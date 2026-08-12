from __future__ import annotations

import json
import logging
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from src.llm.registry import resolve_llm

logger = logging.getLogger("investment-auto.agent-workflow")
ROOT = Path(__file__).resolve().parents[2]
MEMORY_DIR = ROOT / "runtime" / "trading" / "agent_memory"

ROLE_GROUPS: Dict[str, Sequence[str]] = {
    "base": ("technical_analyst", "sentiment_analyst", "news_analyst", "fundamentals_analyst"),
    "research": ("bull_researcher", "bear_researcher"),
    "research_manager": ("research_manager",),
    "advice": ("investment_advisor",),
    "risk": ("aggressive_analyst", "neutral_analyst", "conservative_analyst"),
    "risk_manager": ("risk_manager",),
    "portfolio": ("portfolio_manager",),
}

ROLE_NAMES = {
    "technical_analyst": "市场技术分析师",
    "sentiment_analyst": "市场情绪分析师",
    "news_analyst": "新闻事件分析师",
    "fundamentals_analyst": "基本面分析师",
    "bull_researcher": "多头研究员",
    "bear_researcher": "空头研究员",
    "research_manager": "研究经理",
    "investment_advisor": "投资建议分析师",
    "aggressive_analyst": "激进风险分析师",
    "neutral_analyst": "中立风险分析师",
    "conservative_analyst": "保守风险分析师",
    "risk_manager": "风险经理",
    "portfolio_manager": "投资组合经理",
}

ROLE_INSTRUCTIONS = {
    "technical_analyst": "只分析价格、成交量、均线、MACD、RSI、动量与波动；不得用常识补写行情。",
    "sentiment_analyst": "评估可观测的量价、波动、市场拥挤和宏观风险偏好；没有社交媒体数据时必须列为数据缺口。",
    "news_analyst": "区分市场级宏观事件和公司级新闻；未提供公司级新闻时不得虚构事件、日期或影响。",
    "fundamentals_analyst": "只依据输入中的名称、市值、PE、PB及明确财务字段；缺少营收、利润或现金流时必须说明不可判断。",
    "bull_researcher": "从上游证据中提出最强多头论点，同时诚实列出不支持多头的证据。",
    "bear_researcher": "从上游证据中提出最强空头论点，同时诚实列出不支持空头的证据。",
    "research_manager": "裁决多空论证，比较证据质量与时效性，形成研究结论；不能以意见数量代替证据。",
    "investment_advisor": "把研究结论转成可执行但尚未经风控的投资建议，说明入场、退出、仓位方向和观察条件。",
    "aggressive_analyst": "从较高风险承受角度评估机会成本、上行空间和允许承担的风险，但不能突破代码硬限制。",
    "neutral_analyst": "从风险收益平衡角度评估建议，重点检查证据冲突、组合相关性和情景概率。",
    "conservative_analyst": "从资本保护角度评估尾部风险、回撤、流动性、结算和数据质量，可建议全部持有或退出。",
    "risk_manager": "综合激进、中立、保守评议，形成明确的风险裁决和需要收紧的仓位条件。",
    "portfolio_manager": "结合全部研究与风险裁决，给出组合级目标仓位。只能交易允许池，不能绕过硬风控。",
}

ROLE_EVIDENCE_PREFIXES: Dict[str, Sequence[str]] = {
    "technical_analyst": ("MARKET:", "TECHNICAL:", "HISTORY:", "SCREENING:", "DATA_GAP:"),
    "sentiment_analyst": ("MARKET:", "SENTIMENT:", "MACRO:", "SCREENING:", "DATA_GAP:"),
    "news_analyst": ("NEWS:", "MACRO:", "DATA_GAP:COMPANY_NEWS"),
    "fundamentals_analyst": ("FUNDAMENTALS:", "MARKET:", "SCREENING:", "DATA_GAP:FUNDAMENTALS"),
}

ROLE_UPSTREAM_PREFIXES: Dict[str, Sequence[str]] = {
    "bull_researcher": ("AGENT:TECHNICAL_ANALYST", "AGENT:SENTIMENT_ANALYST", "AGENT:NEWS_ANALYST", "AGENT:FUNDAMENTALS_ANALYST"),
    "bear_researcher": ("AGENT:TECHNICAL_ANALYST", "AGENT:SENTIMENT_ANALYST", "AGENT:NEWS_ANALYST", "AGENT:FUNDAMENTALS_ANALYST"),
    "research_manager": ("AGENT:BULL_RESEARCHER", "AGENT:BEAR_RESEARCHER"),
    "investment_advisor": ("AGENT:RESEARCH_MANAGER",),
    "aggressive_analyst": ("AGENT:INVESTMENT_ADVISOR",),
    "neutral_analyst": ("AGENT:INVESTMENT_ADVISOR",),
    "conservative_analyst": ("AGENT:INVESTMENT_ADVISOR",),
    "risk_manager": ("AGENT:AGGRESSIVE_ANALYST", "AGENT:NEUTRAL_ANALYST", "AGENT:CONSERVATIVE_ANALYST"),
    "portfolio_manager": ("AGENT:RISK_MANAGER", "AGENT:RESEARCH_MANAGER", "AGENT:INVESTMENT_ADVISOR"),
}


def _clean_role(value: str) -> str:
    return re.sub(r"[^a-z0-9_-]+", "-", value.lower()).strip("-")[:64]


class AgentMemoryStore:
    """Bounded, role-isolated memory stored as auditable JSON files."""

    def __init__(self, directory: Path = MEMORY_DIR) -> None:
        self.directory = directory

    def _path(self, market: str, role: str) -> Path:
        return self.directory / _clean_role(market) / f"{_clean_role(role)}.json"

    def load(self, market: str, role: str, limit: int) -> List[Dict[str, Any]]:
        try:
            payload = json.loads(self._path(market, role).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return []
        entries = payload.get("entries", []) if isinstance(payload, Mapping) else []
        return [dict(item) for item in entries[-max(0, limit):] if isinstance(item, Mapping)]

    def append(
        self,
        market: str,
        role: str,
        *,
        generated_at: str,
        situation: str,
        memory_note: str,
        citations: Sequence[str],
        limit: int,
        max_chars: int,
    ) -> None:
        if not memory_note.strip() or limit <= 0:
            return
        path = self._path(market, role)
        entries = self.load(market, role, max(0, limit - 1))
        entries.append({
            "generated_at": generated_at,
            "situation": situation[:max_chars],
            "memory_note": memory_note.strip()[:max_chars],
            "citations": list(dict.fromkeys(str(item) for item in citations))[:20],
        })
        payload = {"market": market, "role": role, "entries": entries[-limit:]}
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)


def _json_text(value: Any, limit: int) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), default=str)[:limit]


def build_evidence_catalog(context: Mapping[str, Any]) -> Dict[str, Any]:
    """Assign stable IDs to every fact an agent is allowed to cite."""
    market = str(context.get("market", "market")).upper()
    catalog: Dict[str, Any] = {
        "ACCOUNT:SUMMARY": context.get("account", {}),
        f"RULES:{market}": context.get("market_rules", {}),
        "SCREENING:RUN": context.get("screening", {}),
    }
    if context.get("macro_excerpt"):
        catalog["MACRO:LATEST"] = str(context.get("macro_excerpt"))[:6000]
        catalog["NEWS:MACRO"] = str(context.get("macro_excerpt"))[:6000]
    catalog["DATA_GAP:COMPANY_NEWS"] = "本轮没有接入逐标的、带来源与发布时间的公司新闻；不得虚构公司事件。"
    if context.get("optimizer"):
        catalog["OPTIMIZER:LATEST"] = context.get("optimizer")
    selected = {
        str(item.get("symbol", "")).upper(): item
        for item in context.get("screening", {}).get("selected", [])
        if isinstance(item, Mapping) and item.get("symbol")
    }
    for raw_symbol, snapshot in context.get("snapshots", {}).items():
        if not isinstance(snapshot, Mapping):
            continue
        symbol = str(raw_symbol).upper()
        catalog[f"MARKET:{symbol}"] = snapshot.get("realtime", {"price": snapshot.get("price")})
        catalog[f"TECHNICAL:{symbol}"] = snapshot.get("indicators", {})
        catalog[f"HISTORY:{symbol}"] = snapshot.get("history", [])[-10:]
        realtime = snapshot.get("realtime", {}) if isinstance(snapshot.get("realtime"), Mapping) else {}
        indicators = snapshot.get("indicators", {}) if isinstance(snapshot.get("indicators"), Mapping) else {}
        selected_row = selected.get(symbol, {})
        catalog[f"SENTIMENT:{symbol}"] = {
            "change_pct": realtime.get("change_pct", selected_row.get("change_pct")),
            "amount": realtime.get("amount", selected_row.get("amount")),
            "volume": indicators.get("volume"),
            "volatility": indicators.get("volatility"),
        }
        fundamentals = {
            "name": realtime.get("name", selected_row.get("name")),
            "pe": realtime.get("pe", selected_row.get("pe")),
            "pb": realtime.get("pb", selected_row.get("pb")),
            "market_cap": realtime.get("market_cap", selected_row.get("market_cap")),
            "sector": selected_row.get("sector"),
            "industry": selected_row.get("industry"),
        }
        catalog[f"FUNDAMENTALS:{symbol}"] = fundamentals
        if not any(fundamentals.get(key) not in (None, "", 0, 0.0) for key in ("pe", "pb", "market_cap")):
            catalog[f"DATA_GAP:FUNDAMENTALS:{symbol}"] = "缺少可核验的估值、利润、营收和现金流数据。"
        if symbol in selected:
            catalog[f"SCREENING:{symbol}"] = selected[symbol]
    return catalog


def _parse_json_object(text: str) -> Dict[str, Any]:
    cleaned = str(text or "").strip()
    fence = chr(96) * 3
    if cleaned.startswith(fence):
        cleaned = re.sub(r"^[A-Za-z]*\s*", "", cleaned[len(fence):], count=1)
        if cleaned.endswith(fence):
            cleaned = cleaned[:-len(fence)].rstrip()
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("Agent 没有返回 JSON 对象")
        payload = json.loads(cleaned[start:end + 1])
    if not isinstance(payload, dict):
        raise ValueError("Agent 输出必须是 JSON 对象")
    return payload


def _citation_ids(payload: Mapping[str, Any]) -> List[str]:
    values: List[Any] = list(payload.get("citations", [])) if isinstance(payload.get("citations"), list) else []
    findings = payload.get("findings", [])
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, Mapping) and isinstance(finding.get("evidence_ids"), list):
                values.extend(finding["evidence_ids"])
    decisions = payload.get("decisions", [])
    if isinstance(decisions, list):
        for decision in decisions:
            if isinstance(decision, Mapping) and isinstance(decision.get("evidence_ids"), list):
                values.extend(decision["evidence_ids"])
    return list(dict.fromkeys(str(item).strip() for item in values if str(item).strip()))


def validate_citations(
    payload: Mapping[str, Any],
    allowed_ids: Iterable[str],
    *,
    required: bool,
    minimum: int,
    require_decision_citations: bool = False,
    required_upstream_prefixes: Sequence[str] = (),
) -> List[str]:
    allowed = set(allowed_ids)
    citations = _citation_ids(payload)
    unknown = [item for item in citations if item not in allowed]
    if unknown:
        raise ValueError("引用了不存在的证据 ID: " + ", ".join(unknown[:8]))
    findings = payload.get("findings", [])
    if required and not require_decision_citations and (not isinstance(findings, list) or not findings):
        raise ValueError("必须输出至少一条带引用的 finding")
    if required and isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, Mapping) and str(finding.get("claim", "")).strip():
                refs = finding.get("evidence_ids", [])
                if not isinstance(refs, list) or not refs:
                    raise ValueError("每条 finding 必须包含 evidence_ids")
    if required and require_decision_citations:
        decisions = payload.get("decisions", [])
        for decision in decisions if isinstance(decisions, list) else []:
            if isinstance(decision, Mapping):
                refs = decision.get("evidence_ids", [])
                if not isinstance(refs, list) or not refs:
                    raise ValueError("每条组合决策必须包含 evidence_ids")
    if required and len(citations) < max(1, minimum):
        raise ValueError(f"有效引用不足 {max(1, minimum)} 条")
    available_prefixes = [
        prefix for prefix in required_upstream_prefixes
        if any(evidence_id.startswith(prefix) for evidence_id in allowed)
    ]
    if required and available_prefixes and not any(
        citation.startswith(tuple(available_prefixes)) for citation in citations
    ):
        raise ValueError("必须引用至少一份直接上游 Agent 报告")
    return citations


def _report_evidence_id(role: str, round_number: Optional[int] = None) -> str:
    suffix = f":R{round_number}" if round_number is not None else ""
    return f"AGENT:{role.upper()}{suffix}"


def _role_enabled(settings: Mapping[str, Any], role: str) -> bool:
    roles = settings.get("roles", {})
    return bool(roles.get(role, True)) if isinstance(roles, Mapping) else True


def _role_evidence(role: str, evidence: Mapping[str, Any]) -> Dict[str, Any]:
    prefixes = ROLE_EVIDENCE_PREFIXES.get(role)
    if not prefixes:
        return dict(evidence)
    scoped = {
        evidence_id: value
        for evidence_id, value in evidence.items()
        if evidence_id.startswith(tuple(prefixes))
    }
    return scoped or {"DATA_GAP:ROLE_INPUT": f"{ROLE_NAMES[role]}没有获得可用领域数据。"}


def _memory_text(entries: Sequence[Mapping[str, Any]], max_chars: int) -> str:
    if not entries:
        return "无"
    rows = [
        f"- {item.get('generated_at', '')}: {str(item.get('memory_note', ''))[:max_chars]}"
        for item in entries
    ]
    return "\n".join(rows)[: max_chars * max(1, len(entries))]


def _evidence_text(evidence: Mapping[str, Any], max_chars: int = 42000) -> str:
    # Put upstream Agent reports first for manager stages, then raw facts. Each
    # item is bounded so a long history series cannot hide later evidence.
    ordered = sorted(evidence.items(), key=lambda item: (not item[0].startswith("AGENT:"), item[0]))
    rows = [
        {"id": evidence_id, "data": _json_text(value, 2600)}
        for evidence_id, value in ordered
    ]
    return json.dumps(rows, ensure_ascii=False, separators=(",", ":"))[:max_chars]


def _call_role(
    role: str,
    *,
    stage: str,
    context: Mapping[str, Any],
    evidence: Mapping[str, Any],
    settings: Mapping[str, Any],
    memory_store: AgentMemoryStore,
    generated_at: str,
    extra_instruction: str = "",
    portfolio: bool = False,
) -> Dict[str, Any]:
    require_citations = bool(settings.get("require_citations", True))
    minimum_citations = int(settings.get("minimum_citations", 1))
    memory_enabled = bool(settings.get("memory_enabled", True))
    memory_limit = max(1, min(50, int(settings.get("memory_entries_per_role", 6))))
    memory_chars = max(200, min(6000, int(settings.get("memory_chars_per_entry", 1200))))
    retries = max(0, min(2, int(settings.get("json_retries", 1))))
    market = str(context.get("market", "market"))
    memories = memory_store.load(market, role, memory_limit) if memory_enabled else []
    if portfolio:
        schema: Dict[str, Any] = {
            "thesis": "组合级结论",
            "decisions": [{
                "decision_id": "唯一短标识",
                "symbol": "allowed_symbols 中的代码",
                "action": "BUY|SELL|HOLD",
                "target_weight": "0到1",
                "confidence": "0到1",
                "reason": "决策依据",
                "evidence_ids": ["证据ID"],
            }],
            "citations": ["证据ID"],
            "memory_note": "留给该角色未来轮次的简短教训",
        }
    else:
        schema = {
            "summary": "简短结论",
            "findings": [{"claim": "可核验判断", "impact": "positive|negative|neutral", "evidence_ids": ["证据ID"]}],
            "stance": "BUY|SELL|HOLD|MIXED",
            "confidence": "0到1",
            "data_gaps": ["缺失数据"],
            "citations": ["证据ID"],
            "memory_note": "留给该角色未来轮次的简短教训",
        }
    scoped_evidence = _role_evidence(role, evidence)
    evidence_text = _evidence_text(scoped_evidence)
    system = (
        f"你是{ROLE_NAMES[role]}。{ROLE_INSTRUCTIONS[role]}"
        "只允许依据本轮证据目录和明确列出的上游 Agent 报告作判断。"
        "不得引用训练知识、猜测来源或制造事实。输出纯 JSON，不要 Markdown，不调用工具。"
        "每个事实判断都要填写 evidence_ids；引用 ID 必须与目录完全一致。"
    )
    user = (
        f"阶段：{stage}\n市场：{market}\n允许交易池：{_json_text(context.get('allowed_symbols', []), 2000)}"
        f"\n角色独立记忆（只能使用自己的历史记忆，记忆不是本轮事实，不能作为引用）：\n{_memory_text(memories, memory_chars)}"
        f"\n附加任务：{extra_instruction or '无'}"
        f"\n本轮可引用证据目录：{evidence_text}"
        f"\n严格输出结构：{json.dumps(schema, ensure_ascii=False)}"
    )
    llm = resolve_llm(role=role)
    chat_kwargs: Dict[str, Any] = {
        "temperature": 0.1,
        "max_tokens": 2600 if portfolio else 2000,
    }
    # DeepSeek thinking models may spend the entire token budget in
    # reasoning_content and return an empty structured answer. Agent stages
    # need compact JSON, so keep thinking disabled just like report jobs do.
    if getattr(llm, "provider_name", "") == "deepseek":
        chat_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    last_error: Optional[Exception] = None
    for attempt in range(retries + 1):
        prompt = user
        if attempt:
            prompt += f"\n上次输出无效：{last_error}。请重新输出完整、闭合且可解析的 JSON。"
        try:
            text = llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                **chat_kwargs,
            )
            payload = _parse_json_object(text)
            citations = validate_citations(
                payload,
                scoped_evidence,
                required=require_citations,
                minimum=minimum_citations,
                require_decision_citations=portfolio,
                required_upstream_prefixes=ROLE_UPSTREAM_PREFIXES.get(role, ()),
            )
            payload.update({"role": role, "role_name": ROLE_NAMES[role], "stage": stage, "citations": citations})
            if memory_enabled:
                memory_store.append(
                    market,
                    role,
                    generated_at=generated_at,
                    situation=f"{market} {stage} {payload.get('summary', payload.get('thesis', ''))}",
                    memory_note=str(payload.get("memory_note", "")),
                    citations=citations,
                    limit=memory_limit,
                    max_chars=memory_chars,
                )
            return payload
        except Exception as exc:
            last_error = exc
    raise RuntimeError(f"{ROLE_NAMES[role]}输出校验失败: {last_error}")


def _parallel_roles(
    roles: Sequence[str],
    *,
    stage: str,
    context: Mapping[str, Any],
    evidence: Mapping[str, Any],
    settings: Mapping[str, Any],
    memory_store: AgentMemoryStore,
    generated_at: str,
    extra_instruction: str = "",
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    active = [role for role in roles if _role_enabled(settings, role)]
    reports: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}
    workers = max(1, min(len(active) or 1, int(settings.get("max_parallel_workers", 4))))
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(
                _call_role,
                role,
                stage=stage,
                context=context,
                evidence=evidence,
                settings=settings,
                memory_store=memory_store,
                generated_at=generated_at,
                extra_instruction=extra_instruction,
            ): role
            for role in active
        }
        for future in as_completed(futures):
            role = futures[future]
            try:
                reports[role] = future.result()
            except Exception as exc:
                errors[role] = str(exc)[:1200]
    return reports, errors


def _add_reports(evidence: Dict[str, Any], reports: Mapping[str, Any], round_number: Optional[int] = None) -> None:
    for role, report in reports.items():
        evidence[_report_evidence_id(role, round_number)] = report


def run_analysis_workflow(
    context: Mapping[str, Any],
    config: Mapping[str, Any],
    *,
    memory_store: Optional[AgentMemoryStore] = None,
) -> Dict[str, Any]:
    settings = config.get("agent_workflow", {})
    if not isinstance(settings, Mapping) or not bool(settings.get("enabled", True)):
        raise ValueError("分阶段 Agent 工作流未启用")
    generated_at = str(context.get("as_of") or datetime.now().astimezone().isoformat(timespec="seconds"))
    store = memory_store or AgentMemoryStore()
    evidence = build_evidence_catalog(context)
    errors: Dict[str, str] = {}
    timings: Dict[str, float] = {}

    started = time.monotonic()
    base_reports, stage_errors = _parallel_roles(
        ROLE_GROUPS["base"], stage="base_analysis", context=context, evidence=evidence,
        settings=settings, memory_store=store, generated_at=generated_at,
    )
    timings["base_analysis"] = round(time.monotonic() - started, 3)
    errors.update(stage_errors)
    required_base = max(1, int(settings.get("minimum_base_analysts", 2)))
    if len(base_reports) < required_base:
        raise RuntimeError(f"基础分析师成功数不足 {required_base}: {errors}")
    _add_reports(evidence, base_reports)

    debate_rounds: List[Dict[str, Any]] = []
    for round_number in range(1, max(1, min(3, int(settings.get("research_debate_rounds", 1)))) + 1):
        started = time.monotonic()
        reports, stage_errors = _parallel_roles(
            ROLE_GROUPS["research"], stage=f"research_debate_{round_number}", context=context,
            evidence=evidence, settings=settings, memory_store=store, generated_at=generated_at,
            extra_instruction="直接回应基础分析及此前辩论，提出可被对方反驳的核心论点。",
        )
        timings[f"research_debate_{round_number}"] = round(time.monotonic() - started, 3)
        errors.update({f"{key}:r{round_number}": value for key, value in stage_errors.items()})
        debate_rounds.append({"round": round_number, "reports": reports})
        _add_reports(evidence, reports, round_number)

    started = time.monotonic()
    research_manager = _call_role(
        "research_manager", stage="research_judgement", context=context, evidence=evidence,
        settings=settings, memory_store=store, generated_at=generated_at,
        extra_instruction="总结多空最强论点，裁决证据质量并给出明确研究结论。",
    ) if _role_enabled(settings, "research_manager") else {}
    timings["research_judgement"] = round(time.monotonic() - started, 3)
    if research_manager:
        evidence[_report_evidence_id("research_manager")] = research_manager

    started = time.monotonic()
    investment_advice = _call_role(
        "investment_advisor", stage="investment_advice", context=context, evidence=evidence,
        settings=settings, memory_store=store, generated_at=generated_at,
        extra_instruction="基于研究经理裁决形成候选级投资建议，不要直接假设订单会通过硬风控。",
    ) if _role_enabled(settings, "investment_advisor") else research_manager
    timings["investment_advice"] = round(time.monotonic() - started, 3)
    if investment_advice:
        evidence[_report_evidence_id("investment_advisor")] = investment_advice

    risk_rounds: List[Dict[str, Any]] = []
    for round_number in range(1, max(1, min(3, int(settings.get("risk_debate_rounds", 1)))) + 1):
        started = time.monotonic()
        reports, stage_errors = _parallel_roles(
            ROLE_GROUPS["risk"], stage=f"risk_debate_{round_number}", context=context,
            evidence=evidence, settings=settings, memory_store=store, generated_at=generated_at,
            extra_instruction="评议投资建议并提出仓位、退出或观望条件；直接回应此前风险意见。",
        )
        timings[f"risk_debate_{round_number}"] = round(time.monotonic() - started, 3)
        errors.update({f"{key}:r{round_number}": value for key, value in stage_errors.items()})
        risk_rounds.append({"round": round_number, "reports": reports})
        _add_reports(evidence, reports, round_number)

    started = time.monotonic()
    risk_manager = _call_role(
        "risk_manager", stage="risk_judgement", context=context, evidence=evidence,
        settings=settings, memory_store=store, generated_at=generated_at,
        extra_instruction="综合三种风险偏好，形成明确风险裁决；代码硬风控仍拥有最终否决权。",
    ) if _role_enabled(settings, "risk_manager") else {}
    timings["risk_judgement"] = round(time.monotonic() - started, 3)
    if risk_manager:
        evidence[_report_evidence_id("risk_manager")] = risk_manager

    if not _role_enabled(settings, "portfolio_manager"):
        raise RuntimeError("投资组合经理不能停用，否则无法生成结构化目标仓位")
    started = time.monotonic()
    portfolio = _call_role(
        "portfolio_manager", stage="portfolio_decision", context=context, evidence=evidence,
        settings=settings, memory_store=store, generated_at=generated_at,
        extra_instruction="输出组合级 BUY/SELL/HOLD 与目标仓位。每条决策必须引用证据，不得输出允许池外标的。",
        portfolio=True,
    )
    timings["portfolio_decision"] = round(time.monotonic() - started, 3)

    return {
        "workflow": "tradingagents_staged_v1",
        "evidence_ids": sorted(evidence),
        "base_reports": base_reports,
        "research_debate": debate_rounds,
        "research_manager": research_manager,
        "investment_advice": investment_advice,
        "risk_debate": risk_rounds,
        "risk_manager": risk_manager,
        "portfolio_manager": portfolio,
        "errors": errors,
        "timings_seconds": timings,
        "memory": {
            "enabled": bool(settings.get("memory_enabled", True)),
            "isolation": "market+role",
            "directory": str(store.directory),
        },
    }
