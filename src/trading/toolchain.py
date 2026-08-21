"""Tool-mediated agent roles (architecture.tool_mediated).

Key decision roles stop relying on one-shot JSON generation followed by
reject-and-retry validation.  Instead they get two native function-calling
tools so citation mistakes become ordinary tool corrections:

* list_evidence_ids - query the valid evidence catalog before citing, so
  the model never has to guess ID formats;
* submit_analysis - submit the structured verdict; the tool runs the same
  parse/validate/repair pipeline used by the JSON path and returns concrete
  errors (missing mandatory IDs, unknown IDs) that the model can correct in
  place.

The caller (agent_workflow._call_role) keeps its outer retry as a last
resort, so tool-mediated rounds that never submit still fall back safely.
"""

from __future__ import annotations

import copy
import json
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

from src.llm.base import BaseLLM
from src.trading.citations import (
    _parse_json_object,
    repair_citations,
    validate_citations,
)


class ToolMediatedError(ValueError):
    """A bounded tool loop ended without an accepted submission.

    ``diagnostic`` deliberately contains only tool names, argument keys and
    validation outcomes.  It is safe to persist without copying the model's
    complete analysis or the evidence catalog into logs.
    """

    def __init__(self, message: str, diagnostic: Sequence[Mapping[str, Any]]) -> None:
        super().__init__(message)
        self.diagnostic = [dict(item) for item in diagnostic]

_TOOL_SPECS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "list_evidence_ids",
            "description": (
                "列出本轮可引用的证据 ID 目录（可按前缀过滤）。"
                "引用任何证据前必须先核对 ID 是否存在于返回列表，禁止编造 ID。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "prefix": {
                        "type": "string",
                        "description": "可选前缀，例如 AGENT:BULL_RESEARCHER 或 TECHNICAL:",
                    },
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "submit_analysis",
            "description": (
                "提交最终分析结论。工具会立即校验引用：非法 ID、缺失的直接上游引用"
                "会以错误信息返回，必须修正后重新提交；提交成功即视为任务完成。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "payload": {
                        "type": "object",
                        "description": (
                            "分析结论对象，字段严格遵循系统提示中的结构要求"
                            "（summary/thesis、findings、stance、confidence、"
                            "data_gaps、citations、memory_note 或 decisions）。"
                        ),
                    },
                },
                "required": ["payload"],
            },
        },
    },
]


def _tool_specs(portfolio: bool) -> List[Dict[str, Any]]:
    # Deep copy: the portfolio branch mutates the submit tool description,
    # which must not leak into the shared global specs.
    specs = copy.deepcopy(_TOOL_SPECS)
    if portfolio:
        payload = specs[1]["function"]["parameters"]["properties"]["payload"]
        payload["description"] = (
            "组合决策对象：thesis、decisions（decision_id/symbol/action/target_weight/"
            "confidence/reason/evidence_ids）、citations、memory_note。"
            "必须逐一覆盖全部允许标的。"
        )
    return specs


def _execute_tool_call(
    call: Mapping[str, Any],
    *,
    allowed_evidence: Mapping[str, Any],
    allowed_symbols: Sequence[str],
    portfolio: bool,
    require_citations: bool,
    minimum_citations: int,
    required_upstream_prefixes: Sequence[str],
    require_all_upstreams: bool,
    attach_missing_upstream: bool = True,
) -> Tuple[Dict[str, Any], bool, Optional[Dict[str, Any]]]:
    """Execute one tool call.  Returns (tool_result, is_submit, submitted_payload)."""
    function = call.get("function", {}) if isinstance(call, Mapping) else {}
    name = str(function.get("name", "") or "")
    arguments = function.get("arguments", {})
    if not isinstance(arguments, Mapping):
        arguments = {}

    if name == "list_evidence_ids":
        prefix = str(arguments.get("prefix", "") or "").strip()
        matches = [
            evidence_id for evidence_id in sorted(allowed_evidence)
            if not prefix or evidence_id.startswith(prefix)
        ]
        return {"evidence_ids": matches[:200]}, False, None

    if name == "submit_analysis":
        payload = arguments.get("payload") if isinstance(arguments.get("payload"), Mapping) else arguments
        if not isinstance(payload, Mapping) or not payload:
            return {
                "accepted": False,
                "error": "payload 必须是完整的结论对象",
            }, True, None
        normalized: Dict[str, Any] = dict(payload)
        repair_notes: List[str] = []
        try:
            citations = validate_citations(
                normalized,
                allowed_evidence,
                required=require_citations,
                minimum=minimum_citations,
                require_decision_citations=portfolio,
                required_upstream_prefixes=required_upstream_prefixes,
                require_all_upstream_prefixes=require_all_upstreams,
            )
        except ValueError as first_error:
            repaired, repair_notes = repair_citations(
                normalized,
                allowed_evidence,
                required_upstream_prefixes=required_upstream_prefixes,
                require_all_upstream_prefixes=require_all_upstreams,
                attach_missing_upstream=attach_missing_upstream,
            )
            if not repair_notes:
                return {
                    "accepted": False,
                    "error": str(first_error),
                    "hint": "用 list_evidence_ids 核对合法 ID，修正后重新提交。",
                }, True, None
            try:
                citations = validate_citations(
                    repaired,
                    allowed_evidence,
                    required=require_citations,
                    minimum=minimum_citations,
                    require_decision_citations=portfolio,
                    required_upstream_prefixes=required_upstream_prefixes,
                    require_all_upstream_prefixes=require_all_upstreams,
                )
            except ValueError as second_error:
                return {
                    "accepted": False,
                    "error": str(second_error),
                }, True, None
            normalized = repaired
        if portfolio:
            # Lazy import avoids a circular dependency at module load time.
            from src.trading.agent_workflow import validate_portfolio_coverage

            try:
                validate_portfolio_coverage(normalized, allowed_symbols)
            except ValueError as coverage_error:
                return {
                    "accepted": False,
                    "error": str(coverage_error),
                }, True, None
        normalized["citations"] = citations
        if repair_notes:
            normalized["citation_repairs"] = repair_notes
        return {
            "accepted": True,
            "verdict": str(normalized.get("summary", normalized.get("thesis", "")))[:120],
        }, True, normalized

    return {"error": "未知工具: " + name}, False, None


def run_tool_mediated_chat(
    llm: BaseLLM,
    *,
    system: str,
    user: str,
    allowed_evidence: Mapping[str, Any],
    allowed_symbols: Sequence[str],
    portfolio: bool,
    require_citations: bool,
    minimum_citations: int,
    required_upstream_prefixes: Sequence[str],
    require_all_upstreams: bool,
    attach_missing_upstream: bool = True,
    tool_rounds: int,
    chat_kwargs: Mapping[str, Any],
) -> Tuple[Dict[str, Any], List[str]]:
    """Run one tool-mediated completion until a valid submission arrives.

    Returns (payload, repair_notes).  Raises ValueError when the round limit
    is exhausted or the model never produces a parseable answer, so the
    caller's outer retry can take over.
    """
    tools = _tool_specs(portfolio)
    # json_object output mode is meaningless (and can conflict) when the
    # provider is expected to emit tool calls instead of a JSON body.
    chat_kwargs = {key: value for key, value in chat_kwargs.items() if key != "response_format"}
    messages: List[Dict[str, Any]] = [
        {"role": "system", "content": system},
        {"role": "user", "content": user},
    ]
    diagnostic: List[Dict[str, Any]] = []
    catalog_queried = False
    submission_attempted = False
    for _round in range(max(1, tool_rounds)):
        round_kwargs = dict(chat_kwargs)
        force_submit = (
            catalog_queried
            or submission_attempted
            or _round == max(1, tool_rounds) - 1
        )
        if force_submit:
            round_kwargs["tool_choice"] = {
                "type": "function",
                "function": {"name": "submit_analysis"},
            }
        response = llm.chat_tools(messages, tools=tools, **round_kwargs)
        if not isinstance(response, Mapping):
            raise ValueError("LLM 工具调用返回了非对象结果")
        tool_calls = response.get("tool_calls") or []
        round_diagnostic: Dict[str, Any] = {
            "round": _round + 1,
            "forced_submit": force_submit,
            "content_chars": len(str(response.get("content", "") or "")),
            "calls": [],
        }
        diagnostic.append(round_diagnostic)
        if not tool_calls:
            content = str(response.get("content", "") or "").strip()
            if not content:
                raise ValueError("Agent 没有返回 JSON 对象")
            payload = _parse_json_object(content)
            try:
                citations = validate_citations(
                    payload,
                    allowed_evidence,
                    required=require_citations,
                    minimum=minimum_citations,
                    require_decision_citations=portfolio,
                    required_upstream_prefixes=required_upstream_prefixes,
                    require_all_upstream_prefixes=require_all_upstreams,
                )
                return payload, []
            except ValueError as validation_error:
                repaired, repair_notes = repair_citations(
                    payload,
                    allowed_evidence,
                    required_upstream_prefixes=required_upstream_prefixes,
                    require_all_upstream_prefixes=require_all_upstreams,
                    attach_missing_upstream=attach_missing_upstream,
                )
                if not repair_notes:
                    raise validation_error
                citations = validate_citations(
                    repaired,
                    allowed_evidence,
                    required=require_citations,
                    minimum=minimum_citations,
                    require_decision_citations=portfolio,
                    required_upstream_prefixes=required_upstream_prefixes,
                    require_all_upstream_prefixes=require_all_upstreams,
                )
                repaired["citations"] = citations
                return repaired, repair_notes
        # OpenAI-compatible APIs expect function.arguments as a JSON
        # string when an assistant tool call is replayed; the adapter
        # parses it into a dict for local execution.
        replay_calls = []
        for call in tool_calls:
            replay = dict(call)
            function = replay.get("function")
            if isinstance(function, dict) and isinstance(function.get("arguments"), dict):
                replay["function"] = dict(function)
                replay["function"]["arguments"] = json.dumps(
                    function["arguments"], ensure_ascii=False
                )
            replay_calls.append(replay)
        messages.append({
            "role": "assistant",
            "content": response.get("content") or "",
            "tool_calls": replay_calls,
        })
        submitted: Optional[Dict[str, Any]] = None
        repair_notes: List[str] = []
        for call in tool_calls:
            function = call.get("function", {}) if isinstance(call, Mapping) else {}
            name = str(function.get("name", "") or "") if isinstance(function, Mapping) else ""
            arguments = function.get("arguments", {}) if isinstance(function, Mapping) else {}
            result, is_submit, payload = _execute_tool_call(
                call,
                allowed_evidence=allowed_evidence,
                allowed_symbols=allowed_symbols,
                portfolio=portfolio,
                require_citations=require_citations,
                minimum_citations=minimum_citations,
                required_upstream_prefixes=required_upstream_prefixes,
                require_all_upstreams=require_all_upstreams,
                attach_missing_upstream=attach_missing_upstream,
            )
            if name == "list_evidence_ids":
                catalog_queried = True
            if is_submit:
                submission_attempted = True
            result_summary: Dict[str, Any] = {}
            if "accepted" in result:
                result_summary["accepted"] = bool(result.get("accepted"))
            if result.get("error"):
                result_summary["error"] = str(result.get("error"))[:500]
            if isinstance(result.get("evidence_ids"), list):
                result_summary["evidence_count"] = len(result["evidence_ids"])
            round_diagnostic["calls"].append({
                "name": name or "unknown",
                "argument_keys": sorted(str(key) for key in arguments) if isinstance(arguments, Mapping) else [],
                "result": result_summary,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": str(call.get("id", "") or "unknown"),
                "content": json.dumps(result, ensure_ascii=False, default=str)[:6000],
            })
            if is_submit and payload is not None:
                submitted = payload
        if submitted is not None:
            return submitted, list(submitted.pop("citation_repairs", repair_notes))
    last_error = ""
    for item in reversed(diagnostic):
        for call in reversed(item.get("calls", [])):
            error = call.get("result", {}).get("error")
            if error:
                last_error = str(error)
                break
        if last_error:
            break
    detail = f"；末次校验: {last_error}" if last_error else ""
    raise ToolMediatedError(
        "工具调用轮次用尽，未收到有效提交" + detail,
        diagnostic,
    )
