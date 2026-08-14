from __future__ import annotations

import copy
import json
import logging
import re
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence

from src.llm.registry import resolve_llm

logger = logging.getLogger("investment-auto.agent-workflow")
ROOT = Path(__file__).resolve().parents[2]
MEMORY_DIR = ROOT / "runtime" / "trading" / "agent_memory"
AGENT_FAILURE_DIR = ROOT / "runtime" / "trading" / "agent_failures"
_failure_lock = threading.RLock()

ROLE_GROUPS: Dict[str, Sequence[str]] = {
    "base": ("technical_analyst", "sentiment_analyst", "news_analyst", "fundamentals_analyst"),
    "research": ("bull_researcher", "bear_researcher"),
    "research_manager": ("research_manager",),
    "trader": ("trader",),
    # This order is intentional.  Unlike the old parallel committee, each
    # opinion is added to the state before the next risk persona is called.
    "risk": ("aggressive_analyst", "conservative_analyst", "neutral_analyst"),
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
    "trader": "逐标的交易员",
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
    "trader": "只针对当前标的，把研究经理裁决转成一个明确的 BUY、HOLD 或 SELL 候选建议；不得替组合或硬风控作决定。",
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
    "trader": ("AGENT:RESEARCH_MANAGER",),
    "aggressive_analyst": ("AGENT:PORTFOLIO_MANAGER:PROPOSAL",),
    "neutral_analyst": ("AGENT:CONSERVATIVE_ANALYST",),
    "conservative_analyst": ("AGENT:AGGRESSIVE_ANALYST",),
    "risk_manager": ("AGENT:AGGRESSIVE_ANALYST", "AGENT:NEUTRAL_ANALYST", "AGENT:CONSERVATIVE_ANALYST"),
    "portfolio_manager": ("AGENT:RISK_MANAGER", "AGENT:PORTFOLIO_MANAGER:PROPOSAL"),
}

GLOBAL_EVIDENCE_IDS = {
    "ACCOUNT:SUMMARY",
    "SCREENING:RUN",
    "MACRO:LATEST",
    "NEWS:MACRO",
    "OPTIMIZER:LATEST",
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
        # Legacy entries were written immediately after an Agent response, before
        # any market outcome was observable.  They are deliberately ignored so
        # plausible-sounding self summaries cannot become investment "facts".
        verified = [
            dict(item)
            for item in entries
            if isinstance(item, Mapping)
            and (item.get("outcome_status") == "evaluated" or item.get("verified") is True)
        ]
        return verified[-max(0, limit):]

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
            "outcome_status": "evaluated",
            "verified": True,
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
        supplied_fundamentals = snapshot.get("fundamentals", {})
        if isinstance(supplied_fundamentals, Mapping):
            fundamentals.update({
                str(key): value for key, value in supplied_fundamentals.items()
                if value not in (None, "")
            })
        fundamentals["data_sources"] = list(snapshot.get("research_data_sources", []))
        catalog[f"FUNDAMENTALS:{symbol}"] = fundamentals
        if not any(fundamentals.get(key) not in (None, "", 0, 0.0) for key in (
            "pe", "pb", "market_cap", "revenue", "net_income", "operating_cash_flow",
        )):
            catalog[f"DATA_GAP:FUNDAMENTALS:{symbol}"] = "缺少可核验的估值、利润、营收和现金流数据。"
        news = snapshot.get("news", [])
        if isinstance(news, list) and news:
            catalog[f"NEWS:{symbol}"] = news[:12]
        else:
            catalog[f"DATA_GAP:COMPANY_NEWS:{symbol}"] = "没有逐标的、带来源与发布时间的公司新闻；不得虚构公司事件。"
        external_sentiment = snapshot.get("sentiment", {})
        if isinstance(external_sentiment, Mapping) and external_sentiment:
            catalog[f"SENTIMENT:{symbol}"].update(external_sentiment)
        research_errors = snapshot.get("research_data_errors", {})
        if isinstance(research_errors, Mapping) and research_errors:
            catalog[f"DATA_GAP:RESEARCH:{symbol}"] = dict(research_errors)
        if symbol in selected:
            catalog[f"SCREENING:{symbol}"] = selected[symbol]
    return catalog


def evidence_for_symbol(evidence: Mapping[str, Any], symbol: str) -> Dict[str, Any]:
    """Create one stock's isolated research state plus shared portfolio facts."""
    normalized = str(symbol).strip().upper()
    suffix = f":{normalized}"
    selected: Dict[str, Any] = {}
    for evidence_id, value in evidence.items():
        if evidence_id == "ACCOUNT:SUMMARY" and isinstance(value, Mapping):
            account = dict(value)
            account["holdings"] = [
                holding for holding in value.get("holdings", [])
                if isinstance(holding, Mapping)
                and str(holding.get("code", "")).strip().upper() == normalized
            ]
            selected[evidence_id] = account
            continue
        if evidence_id == "SCREENING:RUN" and isinstance(value, Mapping):
            selected[evidence_id] = {
                key: item for key, item in value.items()
                if key not in {"selected", "selected_symbols", "allowed_symbols", "scored"}
            }
            continue
        if evidence_id in {"MACRO:LATEST", "NEWS:MACRO"} or evidence_id.startswith("RULES:"):
            selected[evidence_id] = value
            continue
        if evidence_id.endswith(suffix):
            selected[evidence_id] = value
    return selected


def _symbol_context(context: Mapping[str, Any], symbol: str) -> Dict[str, Any]:
    """Bound a research graph to exactly one security."""
    normalized = str(symbol).strip().upper()
    child = dict(context)
    child["allowed_symbols"] = [normalized]
    child["current_symbol"] = normalized
    snapshots = context.get("snapshots", {})
    child["snapshots"] = {
        normalized: snapshots.get(normalized, {})
    } if isinstance(snapshots, Mapping) else {}
    return child


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


def _save_invalid_output(
    *,
    role: str,
    stage: str,
    attempt: int,
    text: str,
    error: Exception,
    generated_at: str,
) -> str:
    """Persist malformed model output so the next failure is diagnosable."""

    safe_role = re.sub(r"[^a-z0-9_-]", "_", role.lower())[:40] or "agent"
    filename = f"{generated_at[:10].replace('-', '')}-{safe_role}-{uuid.uuid4().hex[:10]}.json"
    path = AGENT_FAILURE_DIR / filename
    payload = {
        "generated_at": generated_at,
        "role": role,
        "stage": stage,
        "attempt": attempt,
        "error": str(error),
        "output_chars": len(text),
        "raw_output": text[:24000],
        "truncated_for_diagnostic": len(text) > 24000,
    }
    try:
        with _failure_lock:
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temporary.replace(path)
        return str(path)
    except OSError:
        return ""


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
    require_all_upstream_prefixes: bool = False,
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
    if required and available_prefixes:
        cited_prefixes = {
            prefix for prefix in available_prefixes
            if any(citation.startswith(prefix) for citation in citations)
        }
        if require_all_upstream_prefixes and len(cited_prefixes) != len(available_prefixes):
            missing = [prefix for prefix in available_prefixes if prefix not in cited_prefixes]
            raise ValueError("必须引用全部直接上游 Agent 报告: " + ", ".join(missing))
        if not cited_prefixes:
            raise ValueError("必须引用至少一份直接上游 Agent 报告")
    return citations


def repair_citations(
    payload: Mapping[str, Any],
    allowed_ids: Iterable[str],
    *,
    required_upstream_prefixes: Sequence[str] = (),
    require_all_upstream_prefixes: bool = False,
) -> tuple[Dict[str, Any], List[str]]:
    """Normalize citation IDs and attach missing mandatory upstream citations.

    Most agent failures are citation-format mistakes, not reasoning failures:
    the model references upstream reports by role name, drops the round
    suffix, or forgets to put them in the top-level citations block.  Instead
    of burning a full regeneration, repair the payload when the fix is
    unambiguous:

    * exact catalog ID stays untouched;
    * a bare report ID gains the missing round suffix (or loses a wrong one);
    * a bare role name such as NEWS_ANALYST maps to AGENT:NEWS_ANALYST;
    * mandatory upstream IDs the model forgot are attached to top-level
      citations -- only system-supplied upstream Agent IDs, never raw facts.

    Returns the repaired payload and repair notes.  An unrecoverable payload
    is returned untouched with empty notes so the caller retries as before.
    """
    allowed = {str(item).strip() for item in allowed_ids}
    agent_allowed = sorted(
        candidate for candidate in allowed if str(candidate).startswith("AGENT:")
    )
    repairs: List[str] = []

    def _normalize(raw: str) -> Optional[str]:
        value = raw.strip().upper()
        if not value or value in allowed:
            return None
        # Strip a wrong round suffix: AGENT:BULL_RESEARCHER:R1 when the
        # catalog only carries the bare report ID.
        if ":" in value:
            bare = value.rsplit(":", 1)[0]
            if bare in allowed:
                return bare
        # Attach the missing round suffix when exactly one candidate matches.
        suffixed = sorted(
            candidate for candidate in agent_allowed if candidate.startswith(value + ":")
        )
        if len(suffixed) == 1:
            return suffixed[0]
        if len(suffixed) > 1:
            return None  # ambiguous
        # Model wrote a bare role name (NEWS_ANALYST) instead of an ID.
        prefixed = sorted(
            candidate for candidate in agent_allowed if candidate == "AGENT:" + value
        )
        if len(prefixed) == 1:
            return prefixed[0]
        prefixed_round = sorted(
            candidate for candidate in agent_allowed if candidate.startswith("AGENT:" + value + ":")
        )
        if len(prefixed_round) == 1:
            return prefixed_round[0]
        return None

    def _replace(container: Any, path: str) -> None:
        if not isinstance(container, list):
            return
        for index, item in enumerate(container):
            if not isinstance(item, str):
                continue
            fixed = _normalize(str(item))
            if fixed:
                container[index] = fixed
                repairs.append(path + "[" + str(index) + "]: " + item.strip() + " -> " + fixed)

    repaired = copy.deepcopy(dict(payload))
    if isinstance(repaired.get("citations"), list):
        _replace(repaired["citations"], "citations")
    findings = repaired.get("findings", [])
    if isinstance(findings, list):
        for finding in findings:
            if isinstance(finding, dict):
                _replace(finding.get("evidence_ids"), "findings.evidence_ids")
    decisions = repaired.get("decisions", [])
    if isinstance(decisions, list):
        for decision in decisions:
            if isinstance(decision, dict):
                _replace(decision.get("evidence_ids"), "decisions.evidence_ids")

    available_prefixes = [
        prefix for prefix in required_upstream_prefixes
        if any(evidence_id.startswith(prefix) for evidence_id in allowed)
    ]
    if available_prefixes:
        citations = (
            list(repaired["citations"]) if isinstance(repaired.get("citations"), list) else []
        )
        cited = [str(item) for item in citations]
        cited_prefixes = {
            prefix for prefix in available_prefixes
            if any(citation.startswith(prefix) for citation in cited)
        }
        missing = [prefix for prefix in available_prefixes if prefix not in cited_prefixes]
        if missing and (require_all_upstream_prefixes or not cited_prefixes):
            targets = missing if require_all_upstream_prefixes else missing[:1]
            for prefix in targets:
                exact = sorted(
                    evidence_id for evidence_id in allowed if evidence_id.startswith(prefix)
                )
                if not exact:
                    continue
                repaired["citations"] = [*citations, exact[0]]
                repairs.append("citations: auto_attached " + exact[0])
    return repaired, repairs


def validate_portfolio_coverage(payload: Mapping[str, Any], required_symbols: Sequence[str]) -> None:
    decisions = payload.get("decisions", [])
    if not isinstance(decisions, list):
        raise ValueError("投资组合经理必须输出 decisions 数组")
    expected = {str(symbol).strip().upper() for symbol in required_symbols if str(symbol).strip()}
    actual = {
        str(decision.get("symbol", "")).strip().upper()
        for decision in decisions
        if isinstance(decision, Mapping)
    }
    missing = sorted(expected - actual)
    if missing:
        raise ValueError(f"投资组合决策缺少标的: {', '.join(missing)}")


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


def _upstream_evidence(
    evidence: Mapping[str, Any],
    prefixes: Sequence[str],
    *,
    include_raw: bool = True,
) -> Dict[str, Any]:
    """Expose only declared parents, preventing managers from bypassing them."""
    selected: Dict[str, Any] = {}
    for evidence_id, value in evidence.items():
        if evidence_id.startswith(tuple(prefixes)):
            selected[evidence_id] = value
        elif include_raw and not evidence_id.startswith("AGENT:"):
            selected[evidence_id] = value
    return selected


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


def _architecture_settings() -> Dict[str, Any]:
    """Read the architecture feature-flag block from the global config."""
    from src.config import cfg

    value = cfg.raw.get("architecture", {})
    return dict(value) if isinstance(value, Mapping) else {}


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
    allowed_evidence: Optional[Mapping[str, Any]] = None,
    required_upstream_prefixes: Optional[Sequence[str]] = None,
    require_all_upstreams: bool = False,
    persist_memory: bool = False,
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
    scoped_evidence = dict(allowed_evidence) if allowed_evidence is not None else _role_evidence(role, evidence)
    evidence_text = _evidence_text(scoped_evidence)
    upstream_prefixes = (
        tuple(required_upstream_prefixes)
        if required_upstream_prefixes is not None
        else tuple(ROLE_UPSTREAM_PREFIXES.get(role, ()))
    )
    mandatory_upstream_ids = {
        prefix: [evidence_id for evidence_id in scoped_evidence if evidence_id.startswith(prefix)]
        for prefix in upstream_prefixes
    }
    mandatory_upstream_ids = {
        prefix: ids for prefix, ids in mandatory_upstream_ids.items() if ids
    }
    system = (
        f"你是{ROLE_NAMES[role]}。{ROLE_INSTRUCTIONS[role]}"
        "只允许依据本轮证据目录和明确列出的上游 Agent 报告作判断。"
        "不得引用训练知识、猜测来源或制造事实。输出纯 JSON，不要 Markdown，不调用工具。"
        "必须返回一个语法完整的 JSON 对象并以右花括号结束；字符串内部的双引号必须转义。"
        "每个事实判断都要填写 evidence_ids；引用 ID 必须与目录完全一致。"
    )
    mandate = context.get("investment_mandate", {})
    if isinstance(mandate, Mapping):
        system += (
            f"本轮投资授权书为“{mandate.get('display_name', '中立策略')}”，"
            f"长期目标是：{mandate.get('objective', '')}。"
            f"决策倾向：{mandate.get('prompt', '')}"
            "投资授权书是用户目标，角色记忆和本轮反思都不能改变其风险档位或突破硬限制。"
        )
    user = (
        f"阶段：{stage}\n市场：{market}\n允许交易池：{_json_text(context.get('allowed_symbols', []), 2000)}"
        f"\n角色独立记忆（只能使用自己的历史记忆，记忆不是本轮事实，不能作为引用）：\n{_memory_text(memories, memory_chars)}"
        f"\n跨轮次过程反思（只能改进分析方法，不能作为市场事实或引用，也不能改变授权书风险档位）："
        f"\n{_json_text(context.get('reflection_lessons', []), 5000)}"
        f"\n附加任务：{extra_instruction or '无'}"
        f"\n必须引用的直接上游证据组：{json.dumps(mandatory_upstream_ids, ensure_ascii=False)}"
        "\n以上每个非空组都至少选择一个完整 ID 写入顶层 citations；"
        "若要求逐一引用，则每个组都不能遗漏。不得用原始行情 ID 替代直接上游 Agent ID。"
        f"\n本轮可引用证据目录：{evidence_text}"
        f"\n严格输出结构：{json.dumps(schema, ensure_ascii=False)}"
        "\n输出必须精简：summary/thesis 不超过180字，findings最多6条，每条claim/reason不超过120字，"
        "data_gaps最多5条，memory_note不超过160字；不要复制证据原文，不要添加结构外字段。"
    )
    llm = resolve_llm(role=role)
    chat_kwargs: Dict[str, Any] = {
        "temperature": 0.1,
        "max_tokens": 10000,
        "response_format": {"type": "json_object"},
    }
    # DeepSeek thinking models may spend the entire token budget in
    # reasoning_content and return an empty structured answer. Agent stages
    # need compact JSON, so keep thinking disabled just like report jobs do.
    if getattr(llm, "provider_name", "") == "deepseek":
        chat_kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    last_error: Optional[Exception] = None
    last_diagnostic = ""
    for attempt in range(retries + 1):
        text = ""
        prompt = user
        if attempt:
            prompt += (
                f"\n上次输出无效：{last_error}。这是一次全新重试：请压缩措辞，"
                "从头重新输出完整、闭合且可解析的 JSON，不要延续或解释上次输出。"
                f"顶层 citations 必须包含直接上游完整 ID：{json.dumps(mandatory_upstream_ids, ensure_ascii=False)}。"
            )
        try:
            attempt_kwargs = dict(chat_kwargs)
            if attempt:
                attempt_kwargs["temperature"] = 0
                attempt_kwargs["max_tokens"] = 10000
            text = llm.chat(
                [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
                **attempt_kwargs,
            )
            payload = _parse_json_object(text)
            citation_repairs: List[str] = []
            try:
                citations = validate_citations(
                    payload,
                    scoped_evidence,
                    required=require_citations,
                    minimum=minimum_citations,
                    require_decision_citations=portfolio,
                    required_upstream_prefixes=upstream_prefixes,
                    require_all_upstream_prefixes=require_all_upstreams,
                )
            except ValueError as validation_error:
                # Citation-format mistakes dominate agent failures.  Repair
                # unambiguous ones instead of regenerating the whole answer;
                # an unrecoverable payload is retried as before.
                auto_repair = bool(_architecture_settings().get("citation_auto_repair", True))
                if not auto_repair:
                    raise
                repaired_payload, repair_notes = repair_citations(
                    payload,
                    scoped_evidence,
                    required_upstream_prefixes=upstream_prefixes,
                    require_all_upstream_prefixes=require_all_upstreams,
                )
                if not repair_notes:
                    raise
                citations = validate_citations(
                    repaired_payload,
                    scoped_evidence,
                    required=require_citations,
                    minimum=minimum_citations,
                    require_decision_citations=portfolio,
                    required_upstream_prefixes=upstream_prefixes,
                    require_all_upstream_prefixes=require_all_upstreams,
                )
                payload = repaired_payload
                citation_repairs = repair_notes
            if portfolio:
                validate_portfolio_coverage(payload, context.get("allowed_symbols", []))
            payload.update({"role": role, "role_name": ROLE_NAMES[role], "stage": stage, "citations": citations})
            if citation_repairs:
                payload["citation_repairs"] = citation_repairs
            # Outcome-blind self summaries are not lessons.  Normal workflow
            # calls keep memory pending until a delayed evaluator can attach
            # observed returns; direct callers may explicitly persist it.
            if memory_enabled and persist_memory:
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
            if text:
                last_diagnostic = _save_invalid_output(
                    role=role,
                    stage=stage,
                    attempt=attempt + 1,
                    text=text,
                    error=exc,
                    generated_at=generated_at,
                )
    diagnostic_note = f"；原始输出诊断: {last_diagnostic}" if last_diagnostic else ""
    raise RuntimeError(
        f"{ROLE_NAMES[role]}输出校验失败（已尝试 {retries + 1} 次）: {last_error}{diagnostic_note}"
    )


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
    allowed_evidence: Optional[Mapping[str, Any]] = None,
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
                allowed_evidence=allowed_evidence,
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


def _run_symbol_research(
    symbol: str,
    *,
    context: Mapping[str, Any],
    evidence: Mapping[str, Any],
    settings: Mapping[str, Any],
    memory_store: AgentMemoryStore,
    generated_at: str,
) -> Dict[str, Any]:
    """Run a complete, isolated research graph for one security."""
    child_context = _symbol_context(context, symbol)
    child_evidence = evidence_for_symbol(evidence, symbol)
    errors: Dict[str, str] = {}
    timings: Dict[str, float] = {}

    started = time.monotonic()
    base_reports, stage_errors = _parallel_roles(
        ROLE_GROUPS["base"], stage=f"{symbol}:base_analysis", context=child_context,
        evidence=child_evidence, settings=settings, memory_store=memory_store,
        generated_at=generated_at,
    )
    timings["base_analysis"] = round(time.monotonic() - started, 3)
    errors.update(stage_errors)
    required_base = max(1, int(settings.get("minimum_base_analysts", 4)))
    if len(base_reports) < required_base:
        raise RuntimeError(f"{symbol} 基础分析师成功数不足 {required_base}: {errors}")
    _add_reports(child_evidence, base_reports)

    debate_rounds: List[Dict[str, Any]] = []
    rounds = max(1, min(3, int(settings.get("research_debate_rounds", 1))))
    for round_number in range(1, rounds + 1):
        started = time.monotonic()
        reports: Dict[str, Any] = {}
        for role in ROLE_GROUPS["research"]:
            if not _role_enabled(settings, role):
                raise RuntimeError(f"{symbol} 的 {ROLE_NAMES[role]}不能停用")
            opponent = "bear_researcher" if role == "bull_researcher" else "bull_researcher"
            prior_prefixes = tuple(
                prefix for prefix in (
                    _report_evidence_id(opponent, round_number),
                    _report_evidence_id(opponent, round_number - 1) if round_number > 1 else "",
                ) if prefix
            )
            allowed = _upstream_evidence(
                child_evidence,
                (*ROLE_UPSTREAM_PREFIXES[role], *prior_prefixes),
            )
            report = _call_role(
                role, stage=f"{symbol}:research_debate_{round_number}", context=child_context,
                evidence=child_evidence, allowed_evidence=allowed, settings=settings,
                memory_store=memory_store, generated_at=generated_at,
                required_upstream_prefixes=(prior_prefixes or ROLE_UPSTREAM_PREFIXES[role]),
                extra_instruction=(
                    f"只研究 {symbol}。直接回应当前辩论历史；"
                    "不得把 HOLD 当成多头或空头立场，必须提出本方最强论证。"
                ),
            )
            reports[role] = report
            child_evidence[_report_evidence_id(role, round_number)] = report
        timings[f"research_debate_{round_number}"] = round(time.monotonic() - started, 3)
        debate_rounds.append({"round": round_number, "reports": reports})

    started = time.monotonic()
    manager_prefixes = tuple(
        _report_evidence_id(role, rounds) for role in ROLE_GROUPS["research"]
    )
    manager_allowed = _upstream_evidence(child_evidence, manager_prefixes)
    research_manager = _call_role(
        "research_manager", stage=f"{symbol}:research_judgement", context=child_context,
        evidence=child_evidence, allowed_evidence=manager_allowed, settings=settings,
        memory_store=memory_store, generated_at=generated_at,
        required_upstream_prefixes=manager_prefixes, require_all_upstreams=True,
        extra_instruction=f"只裁决 {symbol}，必须分别评价最新多头和空头论证。",
    )
    timings["research_judgement"] = round(time.monotonic() - started, 3)
    child_evidence[_report_evidence_id("research_manager")] = research_manager

    started = time.monotonic()
    trader_allowed = _upstream_evidence(child_evidence, ("AGENT:RESEARCH_MANAGER",))
    trader = _call_role(
        "trader", stage=f"{symbol}:trade_proposal", context=child_context,
        evidence=child_evidence, allowed_evidence=trader_allowed, settings=settings,
        memory_store=memory_store, generated_at=generated_at,
        required_upstream_prefixes=("AGENT:RESEARCH_MANAGER",),
        extra_instruction=(
            f"只针对 {symbol} 给出 BUY、HOLD 或 SELL 候选建议。"
            "说明入场/退出条件和方向，但不要生成组合目标权重。"
        ),
    )
    timings["trade_proposal"] = round(time.monotonic() - started, 3)
    child_evidence[_report_evidence_id("trader")] = trader

    return {
        "symbol": symbol,
        "status": "completed",
        "evidence_ids": sorted(child_evidence),
        "base_reports": base_reports,
        "research_debate": debate_rounds,
        "research_manager": research_manager,
        "trader": trader,
        "errors": errors,
        "timings_seconds": timings,
    }


def _parallel_symbol_research(
    symbols: Sequence[str],
    **kwargs: Any,
) -> tuple[Dict[str, Dict[str, Any]], Dict[str, str]]:
    workers = max(1, min(len(symbols) or 1, int(kwargs["settings"].get("symbol_workers", 2))))
    reports: Dict[str, Dict[str, Any]] = {}
    errors: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=workers) as executor:
        futures = {
            executor.submit(_run_symbol_research, symbol, **kwargs): symbol
            for symbol in symbols
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                reports[symbol] = future.result()
            except Exception as exc:
                errors[symbol] = str(exc)[:2000]
    return reports, errors


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
    symbols = list(dict.fromkeys(
        str(symbol).strip().upper()
        for symbol in context.get("allowed_symbols", [])
        if str(symbol).strip()
    ))
    if not symbols:
        raise RuntimeError("逐标的研究没有收到任何允许标的")

    started = time.monotonic()
    symbol_research, symbol_errors = _parallel_symbol_research(
        symbols,
        context=context,
        evidence=evidence,
        settings=settings,
        memory_store=store,
        generated_at=generated_at,
    )
    timings["symbol_research"] = round(time.monotonic() - started, 3)
    errors.update({f"symbol:{key}": value for key, value in symbol_errors.items()})
    if symbol_errors:
        raise RuntimeError("逐标的研究未完整完成: " + json.dumps(symbol_errors, ensure_ascii=False))

    # The portfolio layer receives the final trader report for every security,
    # not the entire collection of intermediate prose.
    portfolio_evidence = {
        evidence_id: value for evidence_id, value in evidence.items()
        if evidence_id in GLOBAL_EVIDENCE_IDS
        or evidence_id.startswith("RULES:")
        or evidence_id.startswith("MARKET:")
        or evidence_id.startswith("SCREENING:")
    }
    trader_prefixes: List[str] = []
    for symbol in symbols:
        evidence_id = f"AGENT:TRADER:{symbol}"
        portfolio_evidence[evidence_id] = symbol_research[symbol]["trader"]
        trader_prefixes.append(evidence_id)

    started = time.monotonic()
    proposal = _call_role(
        "portfolio_manager", stage="portfolio_proposal", context=context,
        evidence=portfolio_evidence, allowed_evidence=portfolio_evidence,
        settings=settings, memory_store=store, generated_at=generated_at,
        required_upstream_prefixes=tuple(trader_prefixes), require_all_upstreams=True,
        extra_instruction=(
            "这是风控前组合草案。逐一覆盖全部 allowed_symbols；候选股票决定 BUY/HOLD，"
            "已有持仓决定 BUY/HOLD/SELL。每只股票必须引用它自己的 AGENT:TRADER:代码 报告。"
        ),
        portfolio=True,
    )
    timings["portfolio_proposal"] = round(time.monotonic() - started, 3)
    portfolio_evidence["AGENT:PORTFOLIO_MANAGER:PROPOSAL"] = proposal

    risk_rounds: List[Dict[str, Any]] = []
    risk_round_count = max(1, min(3, int(settings.get("risk_debate_rounds", 1))))
    for round_number in range(1, risk_round_count + 1):
        started = time.monotonic()
        reports: Dict[str, Any] = {}
        previous_id = "AGENT:PORTFOLIO_MANAGER:PROPOSAL"
        for role in ROLE_GROUPS["risk"]:
            if not _role_enabled(settings, role):
                raise RuntimeError(f"{ROLE_NAMES[role]}不能停用，否则风险讨论不完整")
            if reports:
                previous_role = next(reversed(reports))
                previous_id = _report_evidence_id(previous_role, round_number)
            elif round_number > 1:
                previous_id = _report_evidence_id(ROLE_GROUPS["risk"][-1], round_number - 1)
            discussion_prefixes = (
                "AGENT:PORTFOLIO_MANAGER:PROPOSAL",
                *tuple(
                    _report_evidence_id(previous_role, round_number)
                    for previous_role in reports
                ),
            )
            if round_number > 1:
                discussion_prefixes += tuple(
                    _report_evidence_id(previous_role, round_number - 1)
                    for previous_role in ROLE_GROUPS["risk"]
                )
            allowed = _upstream_evidence(portfolio_evidence, discussion_prefixes)
            report = _call_role(
                role, stage=f"risk_debate_{round_number}", context=context,
                evidence=portfolio_evidence, allowed_evidence=allowed, settings=settings,
                memory_store=store, generated_at=generated_at,
                required_upstream_prefixes=(previous_id,),
                extra_instruction=(
                    "评议整个组合草案，直接回应上一位发言者，提出仓位、退出和观望条件。"
                    "不得忽略已有持仓风险。"
                ),
            )
            reports[role] = report
            portfolio_evidence[_report_evidence_id(role, round_number)] = report
        timings[f"risk_debate_{round_number}"] = round(time.monotonic() - started, 3)
        risk_rounds.append({"round": round_number, "reports": reports})

    started = time.monotonic()
    latest_risk_prefixes = tuple(
        _report_evidence_id(role, risk_round_count) for role in ROLE_GROUPS["risk"]
    )
    risk_allowed = _upstream_evidence(portfolio_evidence, latest_risk_prefixes)
    risk_manager = _call_role(
        "risk_manager", stage="risk_judgement", context=context,
        evidence=portfolio_evidence, allowed_evidence=risk_allowed, settings=settings,
        memory_store=store, generated_at=generated_at,
        required_upstream_prefixes=latest_risk_prefixes, require_all_upstreams=True,
        extra_instruction="必须分别裁决激进、保守和中立意见；代码硬风控仍拥有最终否决权。",
    )
    timings["risk_judgement"] = round(time.monotonic() - started, 3)
    portfolio_evidence[_report_evidence_id("risk_manager")] = risk_manager

    started = time.monotonic()
    final_allowed = _upstream_evidence(
        portfolio_evidence,
        ("AGENT:RISK_MANAGER", "AGENT:PORTFOLIO_MANAGER:PROPOSAL"),
    )
    portfolio = _call_role(
        "portfolio_manager", stage="portfolio_decision", context=context,
        evidence=portfolio_evidence, allowed_evidence=final_allowed,
        settings=settings, memory_store=store, generated_at=generated_at,
        required_upstream_prefixes=("AGENT:RISK_MANAGER", "AGENT:PORTFOLIO_MANAGER:PROPOSAL"),
        require_all_upstreams=True,
        extra_instruction=(
            "根据风险裁决修订组合草案，逐一覆盖全部标的。候选股票决定 BUY/HOLD，"
            "已有持仓决定 BUY/HOLD/SELL；每条决策必须引用风险经理或原组合草案。"
        ),
        portfolio=True,
    )
    timings["portfolio_decision"] = round(time.monotonic() - started, 3)

    first_symbol = symbols[0]
    return {
        "workflow": "per_symbol_research_graph_v2",
        "evidence_ids": sorted(portfolio_evidence),
        "symbol_research": symbol_research,
        "symbol_errors": symbol_errors,
        # Compatibility fields keep existing reports/tests readable while the
        # source of truth moves to symbol_research.
        "base_reports": symbol_research[first_symbol]["base_reports"],
        "research_debate": symbol_research[first_symbol]["research_debate"],
        "research_manager": symbol_research[first_symbol]["research_manager"],
        "trader": {symbol: report["trader"] for symbol, report in symbol_research.items()},
        "investment_advice": {},
        "portfolio_proposal": proposal,
        "risk_debate": risk_rounds,
        "risk_manager": risk_manager,
        "portfolio_manager": portfolio,
        "errors": errors,
        "timings_seconds": timings,
        "memory": {
            "enabled": bool(settings.get("memory_enabled", True)),
            "isolation": "market+role+outcome-evaluated",
            "directory": str(store.directory),
            "writes": "delayed_until_outcome",
        },
    }
