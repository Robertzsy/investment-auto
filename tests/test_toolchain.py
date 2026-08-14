from __future__ import annotations

import json

import pytest

from src.trading import toolchain


class _FakeLLM:
    """Scripted chat_tools responder: returns one response per call."""

    provider_name = "deepseek"

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def chat_tools(self, messages, tools, **kwargs):
        self.calls.append({"messages": messages, "kwargs": kwargs})
        if not self._responses:
            raise AssertionError("fake LLM ran out of scripted responses")
        return self._responses.pop(0)


def _submit(citations, *, portfolio=False):
    if portfolio:
        payload = {
            "thesis": "组合判断",
            "decisions": [{
                "decision_id": "p1", "symbol": "600519", "action": "BUY",
                "target_weight": 0.1, "confidence": 0.8, "reason": "test",
                "evidence_ids": citations,
            }],
            "citations": citations,
            "memory_note": "",
        }
    else:
        payload = {
            "summary": "分析结论",
            "findings": [{"claim": "判断", "impact": "neutral", "evidence_ids": citations}],
            "stance": "HOLD",
            "confidence": 0.6,
            "data_gaps": [],
            "citations": citations,
            "memory_note": "",
        }
    return [{
        "id": "call-1",
        "type": "function",
        "function": {"name": "submit_analysis", "arguments": {"payload": payload}},
    }]


def _evidence():
    return {
        "MARKET:600519": {"price": 100},
        "AGENT:TECHNICAL_ANALYST": {"summary": "tech"},
    }


def _kwargs(**extra):
    base = {
        "allowed_evidence": _evidence(),
        "allowed_symbols": ["600519"],
        "portfolio": False,
        "require_citations": True,
        "minimum_citations": 1,
        "required_upstream_prefixes": ("AGENT:TECHNICAL_ANALYST",),
        "require_all_upstreams": False,
        "tool_rounds": 3,
        "chat_kwargs": {"temperature": 0.1, "max_tokens": 4000},
    }
    base.update(extra)
    return base


def test_tool_mediated_chat_queries_catalog_then_submits():
    llm = _FakeLLM([
        {"content": "", "tool_calls": [{
            "id": "c1", "type": "function",
            "function": {"name": "list_evidence_ids", "arguments": {"prefix": "AGENT:"}},
        }]},
        {"content": "", "tool_calls": _submit(["AGENT:TECHNICAL_ANALYST"])},
    ])
    payload, repairs = toolchain.run_tool_mediated_chat(
        llm, system="系统", user="任务", **_kwargs()
    )

    assert payload["summary"] == "分析结论"
    assert payload["citations"] == ["AGENT:TECHNICAL_ANALYST"]
    assert repairs == []
    assert len(llm.calls) == 2
    # The catalog result was fed back to the model as a tool message.
    second_call_messages = llm.calls[1]["messages"]
    tool_messages = [m for m in second_call_messages if m["role"] == "tool"]
    assert tool_messages and "AGENT:TECHNICAL_ANALYST" in tool_messages[0]["content"]


def test_tool_mediated_chat_repairs_submit_without_rejection():
    # A forgotten mandatory upstream cite is auto-repaired inside the
    # submit tool, so the corrected payload is accepted on the first round.
    llm = _FakeLLM([
        {"content": "", "tool_calls": _submit(["MARKET:600519"])},
    ])
    payload, repairs = toolchain.run_tool_mediated_chat(
        llm, system="系统", user="任务", **_kwargs()
    )

    assert payload["citations"] == ["MARKET:600519", "AGENT:TECHNICAL_ANALYST"]
    assert any("auto_attached" in note for note in repairs)
    assert len(llm.calls) == 1


def test_tool_mediated_submit_auto_repairs_forgotten_upstream():
    llm = _FakeLLM([
        {"content": "", "tool_calls": _submit(["MARKET:600519"])},
    ])
    # require_all_upstreams forces the repair path inside the submit tool.
    payload, repairs = toolchain.run_tool_mediated_chat(
        llm, system="系统", user="任务", **_kwargs(require_all_upstreams=True)
    )

    assert "AGENT:TECHNICAL_ANALYST" in payload["citations"]
    assert any("auto_attached" in note for note in repairs)
    assert len(llm.calls) == 1  # repaired in the tool, no second round


def test_tool_mediated_chat_falls_back_to_direct_json():
    llm = _FakeLLM([
        {"content": json.dumps({
            "summary": "直接输出",
            "findings": [{"claim": "判断", "impact": "neutral",
                          "evidence_ids": ["AGENT:TECHNICAL_ANALYST"]}],
            "stance": "HOLD", "confidence": 0.6, "data_gaps": [],
            "citations": ["AGENT:TECHNICAL_ANALYST"], "memory_note": "",
        }, ensure_ascii=False), "tool_calls": None},
    ])
    payload, repairs = toolchain.run_tool_mediated_chat(
        llm, system="系统", user="任务", **_kwargs()
    )

    assert payload["summary"] == "直接输出"
    assert repairs == []


def test_tool_mediated_chat_raises_when_rounds_exhausted():
    llm = _FakeLLM([
        {"content": "", "tool_calls": _submit(["FAKE:1"])},
        {"content": "", "tool_calls": _submit(["FAKE:1"])},
        {"content": "", "tool_calls": _submit(["FAKE:1"])},
    ])
    with pytest.raises(ValueError, match="轮次用尽"):
        toolchain.run_tool_mediated_chat(
            llm, system="系统", user="任务", **_kwargs(tool_rounds=3)
        )
    assert len(llm.calls) == 3


def test_tool_mediated_portfolio_coverage_is_validated():
    llm = _FakeLLM([
        {"content": "", "tool_calls": _submit(["AGENT:RISK_MANAGER"], portfolio=True)},
    ])
    llm._responses[0]["tool_calls"][0]["function"]["arguments"]["payload"]["citations"] = ["AGENT:RISK_MANAGER"]
    # 000001 missing from decisions -> coverage error fed back; then fixed.
    llm._responses.append({"content": "", "tool_calls": [{
        "id": "c2", "type": "function",
        "function": {"name": "submit_analysis", "arguments": {"payload": {
            "thesis": "组合判断",
            "decisions": [
                {"decision_id": "p1", "symbol": "600519", "action": "BUY",
                 "target_weight": 0.1, "confidence": 0.8, "reason": "test",
                 "evidence_ids": ["AGENT:RISK_MANAGER"]},
                {"decision_id": "p2", "symbol": "000001", "action": "HOLD",
                 "target_weight": 0, "confidence": 0.8, "reason": "hold",
                 "evidence_ids": ["AGENT:RISK_MANAGER"]},
            ],
            "citations": ["AGENT:RISK_MANAGER"],
            "memory_note": "",
        }}},
    }]})
    payload, repairs = toolchain.run_tool_mediated_chat(
        llm, system="系统", user="任务",
        **_kwargs(
            portfolio=True,
            allowed_symbols=["600519", "000001"],
            required_upstream_prefixes=("AGENT:RISK_MANAGER",),
            allowed_evidence={
                "MARKET:600519": {"price": 100},
                "AGENT:RISK_MANAGER": {"summary": "risk"},
            },
        )
    )

    assert len(payload["decisions"]) == 2
    assert len(llm.calls) == 2
    rejection_texts = [m["content"] for m in llm.calls[1]["messages"] if m["role"] == "tool"]
    assert any("000001" in text for text in rejection_texts)


def test_call_role_uses_tool_branch_for_enabled_role(monkeypatch, tmp_path):
    from src.trading import agent_workflow

    monkeypatch.setattr(
        agent_workflow, "_architecture_settings",
        lambda: {"tool_mediated": True, "tool_mediated_roles": ["research_manager"], "tool_retries": 2},
    )

    class LLM:
        provider_name = "deepseek"

        def chat(self, messages, **kwargs):
            raise AssertionError("chat() must not run in tool mode")

        def chat_tools(self, messages, tools, **kwargs):
            return {"content": "", "tool_calls": [
                {"id": "c1", "type": "function", "function": {"name": "submit_analysis", "arguments": {"payload": {
                    "summary": "工具模式",
                    "findings": [{"claim": "判断", "impact": "neutral", "evidence_ids": ["AGENT:BULL_RESEARCHER:R1"]}],
                    "stance": "HOLD", "confidence": 0.6, "data_gaps": [],
                    "citations": ["AGENT:BULL_RESEARCHER:R1"], "memory_note": "",
                }}},
            }]}

    monkeypatch.setattr(agent_workflow, "resolve_llm", lambda role: LLM())
    context = {
        "as_of": "2026-08-12T10:00:00+08:00", "market": "cn",
        "allowed_symbols": ["600519"],
        "account": {"cash": 100000, "holdings": []},
        "snapshots": {}, "screening": {"selected": []},
        "macro_excerpt": "", "optimizer": {}, "market_rules": {},
    }
    result = agent_workflow._call_role(
        "research_manager",
        stage="research_judgement",
        context=context,
        evidence={"AGENT:BULL_RESEARCHER:R1": {"summary": "bull"}},
        settings={
            "enabled": True, "require_citations": True, "minimum_citations": 1,
            "minimum_base_analysts": 2, "research_debate_rounds": 1,
            "risk_debate_rounds": 1, "memory_enabled": True,
            "memory_entries_per_role": 3, "memory_chars_per_entry": 200,
            "roles": {}, "json_retries": 1,
        },
        memory_store=agent_workflow.AgentMemoryStore(tmp_path),
        generated_at="2026-08-12T10:00:00+08:00",
        required_upstream_prefixes=("AGENT:BULL_RESEARCHER",),
        require_all_upstreams=True,
    )

    assert result["summary"] == "工具模式"
    assert result["citations"] == ["AGENT:BULL_RESEARCHER:R1"]