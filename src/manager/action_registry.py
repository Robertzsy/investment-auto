from __future__ import annotations

import importlib
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Any, Callable, Dict, List, Mapping, Optional

from src.config import cfg
from src.manager.security_identity import (
    SecurityIdentity,
    identity_matches,
    is_fresh,
    normalize_provider_text,
    snapshot_identity,
)
from src.manager.skill_models import SESSION_SCOPES, SIDE_EFFECT_LEVELS


ActionHandler = Callable[[Mapping[str, Any], Mapping[str, Any]], Any]


@dataclass(frozen=True)
class ActionSpec:
    name: str
    handler: ActionHandler
    description: str
    session_scopes: frozenset[str]
    side_effect_level: str


def _infer_market(symbol: str, fallback: str = "") -> str:
    value = str(symbol or "").strip().upper()
    if fallback in {"cn", "hk", "us", "etf"}:
        return fallback
    if value.startswith("HK") or re.fullmatch(r"0\d{4}", value):
        return "hk"
    if re.fullmatch(r"\d{6}", value):
        return "cn"
    return "us"


def _adapter_error(payload: Any) -> str:
    if not isinstance(payload, Mapping):
        return "空行情结果" if not payload else "行情返回格式无效"
    if payload.get("error"):
        return str(payload.get("error"))
    if payload.get("returncode"):
        return str(payload.get("stderr") or payload.get("stdout") or f"行情适配器退出码 {payload.get('returncode')}")
    return ""


def _search_rows(payload: Any, query: str, market: str) -> List[Dict[str, Any]]:
    if isinstance(payload, list):
        raw = payload
    elif isinstance(payload, Mapping):
        raw = []
        for key in ("results", "data", "items", "securities"):
            if isinstance(payload.get(key), list):
                raw = payload[key]
                break
    else:
        raw = []
    rows: List[Dict[str, Any]] = []
    for item in raw[:12]:
        if not isinstance(item, Mapping):
            continue
        source_symbol = str(
            item.get("symbol") or item.get("code") or item.get("ticker") or item.get("value") or ""
        ).strip()
        if not source_symbol:
            continue
        row = {
            "symbol": source_symbol,
            "name": normalize_provider_text(
                item.get("name") or item.get("description") or item.get("label") or source_symbol
            ),
            "market": str(item.get("market") or _infer_market(source_symbol, market)).lower(),
            "raw": dict(item),
        }
        rows.append(SecurityIdentity.from_search_row(
            row,
            fallback_query=query,
            fallback_market=market,
        ).to_dict() | {"raw": dict(item)})
    normalized = str(query or "").strip()
    if not rows and re.fullmatch(r"(?:HK)?\d{5,6}|[A-Za-z][A-Za-z0-9.\-]{0,14}", normalized, re.I):
        rows.append(SecurityIdentity.from_search_row(
            {"symbol": normalized, "name": normalized, "raw": {}},
            fallback_query=normalized,
            fallback_market=market,
        ).to_dict() | {"raw": {}})
    return rows


def _entities(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> List[Dict[str, Any]]:
    symbols = inputs.get("symbols")
    if isinstance(symbols, str):
        values = [item.strip() for item in re.split(r"[,，\s]+", symbols) if item.strip()]
    elif isinstance(symbols, list):
        values = [str(item).strip() for item in symbols if str(item).strip()]
    else:
        values = []
    if values:
        market = str(inputs.get("market", "")).lower()
        return [
            SecurityIdentity.from_search_row(
                {"symbol": value, "name": value, "raw": {}},
                fallback_query=value,
                fallback_market=market,
            ).to_dict() | {"raw": {}}
            for value in values[:8]
        ]
    state = context.get("state", {})
    outputs = state.get("outputs", {}) if isinstance(state, Mapping) else {}
    for key in ("resolve", "security.resolve"):
        value = outputs.get(key, {}) if isinstance(outputs, Mapping) else {}
        if isinstance(value, Mapping) and isinstance(value.get("securities"), list):
            return [dict(item) for item in value["securities"] if isinstance(item, Mapping)][:8]
    return []


def _security_resolve(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.platform.market_tools import stock_fetcher

    market = str(inputs.get("market", "")).strip().lower()
    queries: List[str] = []
    symbols = inputs.get("symbols")
    if isinstance(symbols, str):
        queries.extend(item for item in re.split(r"[,，、]+", symbols) if item.strip())
    elif isinstance(symbols, list):
        queries.extend(str(item) for item in symbols if str(item).strip())
    query = str(inputs.get("query") or inputs.get("request") or "").strip()
    if not queries and query:
        cleaned = re.sub(
            r"(?:请|帮我|麻烦|现在|今天|一下|看看|分析|研究|估值|行情|股价|股票|证券|基金|新闻|基本面|技术面|情绪|贵不贵|值不值得|怎么样|如何)",
            " ",
            query,
            flags=re.I,
        )
        candidates = [
            item.strip(" ，,。？?！!的")
            for item in re.split(r"(?:和|与|以及|对比|比较|vs\.?|、|，|,)", cleaned, flags=re.I)
            if item.strip(" ，,。？?！!的")
        ]
        queries.extend(candidates or [query])
    results: List[Dict[str, Any]] = []
    raw_results: List[Any] = []
    for value in queries[:8]:
        payload = stock_fetcher("search", value)
        raw_results.append(payload)
        results.extend(_search_rows(payload, value, market))
    unique: Dict[str, Dict[str, Any]] = {}
    for item in results:
        unique.setdefault(str(item["canonical_symbol"]).upper(), item)
    if not unique:
        raise RuntimeError("无法从请求中识别证券；请提供证券名称或代码")
    return {"securities": list(unique.values())[:8], "queries": queries, "raw_results": raw_results}


def _security_snapshot(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.platform.market_tools import stock_fetcher

    securities = _entities(inputs, context)
    if not securities:
        raise RuntimeError("缺少已解析证券，无法获取行情")
    snapshots: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    identity_checks: Dict[str, Any] = {}
    for item in securities:
        symbol = str(item.get("symbol", "")).strip()
        provider_symbol = str(item.get("provider_symbol") or symbol).strip()
        payload = stock_fetcher("snapshot", provider_symbol)
        adapter_error = _adapter_error(payload)
        if not payload or adapter_error:
            errors[symbol] = adapter_error or "空行情结果"
        elif not isinstance(payload, Mapping) or not identity_matches(item, payload):
            actual = snapshot_identity(payload) if isinstance(payload, Mapping) else None
            errors[symbol] = f"行情身份不一致：期望 {provider_symbol}，实际 {actual or '缺失'}"
            identity_checks[symbol] = {
                "passed": False,
                "expected": provider_symbol,
                "actual": actual,
            }
        else:
            snapshots[symbol] = payload
            identity_checks[symbol] = {
                "passed": True,
                "expected": provider_symbol,
                "actual": snapshot_identity(payload),
            }
    if not snapshots:
        raise RuntimeError("所有证券行情获取均失败: " + "; ".join(f"{k}: {v}" for k, v in errors.items()))
    return {
        "securities": securities,
        "snapshots": snapshots,
        "errors": errors,
        "identity_checks": identity_checks,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _security_validate(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    outputs = context.get("state", {}).get("outputs", {})
    resolved = outputs.get("resolve", {}) if isinstance(outputs, Mapping) else {}
    snapshot_result = outputs.get("snapshot", {}) if isinstance(outputs, Mapping) else {}
    securities = resolved.get("securities", []) if isinstance(resolved, Mapping) else []
    snapshots = snapshot_result.get("snapshots", {}) if isinstance(snapshot_result, Mapping) else {}
    checks = snapshot_result.get("identity_checks", {}) if isinstance(snapshot_result, Mapping) else {}
    expected = len(securities)
    received = len(snapshots) if isinstance(snapshots, Mapping) else 0
    identity_consistent = bool(expected) and all(
        isinstance(checks.get(str(item.get("symbol", ""))), Mapping)
        and checks[str(item.get("symbol", ""))].get("passed") is True
        for item in securities
        if isinstance(item, Mapping)
    )
    fresh_symbols = [
        symbol for symbol, payload in snapshots.items()
        if isinstance(payload, Mapping) and is_fresh(payload)
    ] if isinstance(snapshots, Mapping) else []
    freshness_passed = received > 0 and len(fresh_symbols) == received
    coverage_ratio = received / expected if expected else 0.0
    issues: List[str] = []
    if not identity_consistent:
        issues.append("证券身份校验失败")
    if not freshness_passed:
        issues.append("行情时间缺失或已过期")
    if coverage_ratio < 0.8:
        issues.append(f"行情覆盖率不足（{coverage_ratio:.0%}）")
    return {
        "identity_consistent": identity_consistent,
        "freshness_passed": freshness_passed,
        "coverage_ratio": round(coverage_ratio, 4),
        "expected_count": expected,
        "received_count": received,
        "fresh_symbols": fresh_symbols,
        "fact_consistent": identity_consistent,
        "issues": issues,
    }


def _security_research(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.data.research import fetch_research_packet

    securities = _entities(inputs, context)
    packets: Dict[str, Any] = {}
    for item in securities:
        symbol = str(item.get("symbol", "")).strip()
        market = str(item.get("market") or inputs.get("market") or _infer_market(symbol)).lower()
        packets[symbol] = fetch_research_packet(market, symbol)
    return {"packets": packets, "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds")}


def _fmt(value: Any) -> str:
    if value in (None, ""):
        return "数据缺失"
    if isinstance(value, float):
        return f"{value:,.4f}".rstrip("0").rstrip(".")
    return str(value)


def _security_synthesis(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    import json

    from src.llm.registry import resolve_llm

    outputs = context.get("state", {}).get("outputs", {})
    evidence = {
        "resolved": outputs.get("resolve", {}),
        "snapshots": outputs.get("snapshot", {}),
        "research": outputs.get("research", {}),
    }
    prompt = (
        "你是 Investment-Auto 的证券研究分析师。只依据下面提供的可审计证据回答用户原始问题，"
        "不得补造财务数据、新闻、日期、目标价或社交情绪。输出中文 Markdown，包含：核心判断、"
        "估值与质量、趋势与催化、主要风险、数据缺口；多标的请求必须逐项比较。结论必须说明成立条件，"
        "不输出买卖指令，不展示思考过程。\n\n"
        f"用户问题：{inputs.get('request', '')}\n\n"
        "证据：\n"
        + json.dumps(evidence, ensure_ascii=False, default=str)[:42000]
    )
    llm = resolve_llm(role="analyst")
    kwargs: Dict[str, Any] = {"temperature": 0.1, "max_tokens": 2400}
    if getattr(llm, "provider_name", "") == "deepseek":
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    analysis = llm.chat([{"role": "user", "content": prompt}], **kwargs)
    if not analysis or not analysis.strip():
        raise RuntimeError("证券分析模型返回空结果")
    return {
        "analysis": analysis.strip(),
        "generated_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "provider": str(getattr(llm, "provider_name", "")),
    }


def _security_report(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    outputs = context.get("state", {}).get("outputs", {})
    resolved = outputs.get("resolve", {})
    snapshot_result = outputs.get("snapshot", {})
    research_result = outputs.get("research", {})
    synthesis_result = outputs.get("synthesis", {})
    quality_result = outputs.get("quality", {})
    securities = resolved.get("securities", []) if isinstance(resolved, Mapping) else []
    snapshots = snapshot_result.get("snapshots", {}) if isinstance(snapshot_result, Mapping) else {}
    packets = research_result.get("packets", {}) if isinstance(research_result, Mapping) else {}
    sections = ["## 证券研究报告", ""]
    sources: List[str] = []
    entities: List[str] = []
    for item in securities:
        symbol = str(item.get("symbol", ""))
        name = normalize_provider_text(item.get("name") or symbol)
        entities.append(symbol)
        snapshot = snapshots.get(symbol, {}) if isinstance(snapshots, Mapping) else {}
        realtime = snapshot.get("realtime", snapshot) if isinstance(snapshot, Mapping) else {}
        indicators = snapshot.get("indicators", {}) if isinstance(snapshot, Mapping) else {}
        packet = packets.get(symbol, {}) if isinstance(packets, Mapping) else {}
        fundamentals = packet.get("fundamentals", {}) if isinstance(packet, Mapping) else {}
        news = packet.get("news", []) if isinstance(packet, Mapping) else []
        packet_sources = packet.get("sources", []) if isinstance(packet, Mapping) else []
        sources.extend(str(value) for value in packet_sources)
        sections.extend([
            f"### {name}（{symbol}）",
            "",
            f"- 最新价格：{_fmt(realtime.get('price') if isinstance(realtime, Mapping) else None)}",
            f"- 涨跌幅：{_fmt((realtime.get('changePercent') if realtime.get('changePercent') is not None else realtime.get('change_pct')) if isinstance(realtime, Mapping) else None)}",
            f"- 市盈率：{_fmt((fundamentals or {}).get('pe_ttm') or (realtime or {}).get('pe'))}",
            f"- 市净率：{_fmt((fundamentals or {}).get('pb_annual') or (realtime or {}).get('pb'))}",
            f"- ROE：{_fmt((fundamentals or {}).get('roe_ttm'))}",
            f"- 技术数据：{_fmt(indicators if indicators else None)}",
            "",
        ])
        if news:
            sections.append("近期可核验新闻：")
            sections.extend(
                f"- {str(row.get('headline', ''))}（{str(row.get('source', '未知来源'))}）"
                for row in news[:5]
                if isinstance(row, Mapping)
            )
            sections.append("")
        errors = packet.get("errors", {}) if isinstance(packet, Mapping) else {}
        if errors:
            sections.append("数据缺口：" + "；".join(str(value) for value in errors.values()))
            sections.append("")
    if isinstance(synthesis_result, Mapping) and synthesis_result.get("analysis"):
        sections.extend([
            "### 综合分析",
            "",
            str(synthesis_result["analysis"]).strip(),
            "",
        ])
    else:
        sections.extend([
            "### 综合分析",
            "",
            "分析模型不可用，本次保留经过验证的确定性数据摘要，不额外生成主观估值或买卖结论。",
            "",
        ])
    if isinstance(quality_result, Mapping) and quality_result.get("issues"):
        sections.extend([
            "### 数据质量警告",
            "",
            *[f"- {normalize_provider_text(value)}" for value in quality_result.get("issues", [])],
            "",
            "本次结果未达到完整验证标准，系统不会把它标记为已完成。",
            "",
        ])
    sections.extend([
        "### 风险与结论边界",
        "",
        "以上结论只依据当前可获取的行情和研究数据；缺失的财务、新闻或情绪数据不会由模型补造。",
        "本报告用于模拟研究，不构成投资建议或实盘交易指令。",
        "",
        f"数据时间：{snapshot_result.get('fetched_at', '') if isinstance(snapshot_result, Mapping) else ''}",
    ])
    return {
        "user_report": "\n".join(sections).strip(),
        "entities": entities,
        "data_timestamp": snapshot_result.get("fetched_at", "") if isinstance(snapshot_result, Mapping) else "",
        "sources": list(dict.fromkeys(sources + ["stock-fetcher"])),
        "quality": dict(quality_result) if isinstance(quality_result, Mapping) else {},
    }


_CN_BENCHMARKS = (
    {"symbol": "000001", "canonical_symbol": "SH:000001", "provider_symbol": "sh000001", "exchange": "SH", "asset_type": "index", "market": "cn", "name": "上证指数", "required": True},
    {"symbol": "399001", "canonical_symbol": "SZ:399001", "provider_symbol": "sz399001", "exchange": "SZ", "asset_type": "index", "market": "cn", "name": "深证成指", "required": True},
    {"symbol": "399006", "canonical_symbol": "SZ:399006", "provider_symbol": "sz399006", "exchange": "SZ", "asset_type": "index", "market": "cn", "name": "创业板指", "required": True},
    {"symbol": "000300", "canonical_symbol": "SH:000300", "provider_symbol": "sh000300", "exchange": "SH", "asset_type": "index", "market": "cn", "name": "沪深300", "required": False},
)


def _market_benchmarks(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.platform.market_tools import stock_fetcher

    market = str(inputs.get("market", "cn") or "cn").strip().lower()
    if market != "cn":
        raise ValueError("market-overview 当前只支持 A 股市场（market=cn）")
    snapshots: Dict[str, Any] = {}
    checks: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    recoveries: List[Dict[str, Any]] = []
    for identity in _CN_BENCHMARKS:
        payload: Any = stock_fetcher("realtime", identity["provider_symbol"])
        adapter_error = _adapter_error(payload)
        if not isinstance(payload, Mapping) or adapter_error or not identity_matches(identity, payload):
            reason = (
                adapter_error if adapter_error
                else f"身份不一致：{snapshot_identity(payload) if isinstance(payload, Mapping) else '空结果'}"
            )
            recoveries.append({
                "symbol": identity["provider_symbol"],
                "category": "provider_or_identity",
                "attempt": 1,
                "reason": reason,
                "action": "fallback_snapshot",
            })
            fallback = stock_fetcher("snapshot", identity["provider_symbol"])
            if isinstance(fallback, Mapping) and not _adapter_error(fallback) and identity_matches(identity, fallback):
                payload = fallback
        passed = isinstance(payload, Mapping) and not _adapter_error(payload) and identity_matches(identity, payload)
        actual = snapshot_identity(payload) if isinstance(payload, Mapping) else None
        checks[identity["symbol"]] = {
            "passed": passed,
            "expected": identity["provider_symbol"],
            "actual": actual,
        }
        if passed:
            snapshots[identity["symbol"]] = (
                payload if isinstance(payload.get("realtime"), Mapping) else {"realtime": dict(payload)}
            )
        else:
            errors[identity["symbol"]] = _adapter_error(payload) or (
                f"行情身份不一致：期望 {identity['provider_symbol']}，实际 {actual or '缺失'}"
            )
    return {
        "market": "cn",
        "benchmarks": [dict(item) for item in _CN_BENCHMARKS],
        "snapshots": snapshots,
        "identity_checks": checks,
        "errors": errors,
        "recoveries": recoveries[:8],
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _market_breadth(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.platform.market_tools import stock_fetcher

    try:
        payload = stock_fetcher("market-list", "cn", "500")
    except TypeError:  # compatibility with small test doubles and older adapters
        payload = stock_fetcher("market-list", "cn")
    rows = payload.get("data", []) if isinstance(payload, Mapping) else []
    rows = [item for item in rows if isinstance(item, Mapping)]
    advancers = sum(1 for item in rows if float(item.get("change_pct", 0) or 0) > 0)
    decliners = sum(1 for item in rows if float(item.get("change_pct", 0) or 0) < 0)
    unchanged = max(0, len(rows) - advancers - decliners)
    amount = sum(max(0.0, float(item.get("amount", 0) or 0)) for item in rows)
    return {
        "available": bool(rows),
        "sample_size": len(rows),
        "sample_scope": str(payload.get("scope", "bounded")) if isinstance(payload, Mapping) else "bounded",
        "advancers": advancers,
        "decliners": decliners,
        "unchanged": unchanged,
        "advance_decline_ratio": round(advancers / decliners, 4) if decliners else None,
        "sample_turnover": round(amount, 2),
        "turnover_available": amount > 0,
        "source": str(payload.get("source", "")) if isinstance(payload, Mapping) else "",
        "error": (_adapter_error(payload) or ("市场宽度数据源返回空样本" if not rows else "")),
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
    }


def _market_quality(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    outputs = context.get("state", {}).get("outputs", {})
    benchmark_result = outputs.get("benchmarks", {}) if isinstance(outputs, Mapping) else {}
    breadth = outputs.get("breadth", {}) if isinstance(outputs, Mapping) else {}
    snapshots = benchmark_result.get("snapshots", {}) if isinstance(benchmark_result, Mapping) else {}
    checks = benchmark_result.get("identity_checks", {}) if isinstance(benchmark_result, Mapping) else {}
    required = [item for item in _CN_BENCHMARKS if item["required"]]
    required_symbols = [str(item["symbol"]) for item in required]
    required_indexes_covered = all(symbol in snapshots for symbol in required_symbols)
    identity_consistent = required_indexes_covered and all(
        isinstance(checks.get(symbol), Mapping) and checks[symbol].get("passed") is True
        for symbol in required_symbols
    )
    freshness_passed = required_indexes_covered and all(
        isinstance(snapshots.get(symbol), Mapping) and is_fresh(snapshots[symbol])
        for symbol in required_symbols
    )
    breadth_available = bool(isinstance(breadth, Mapping) and breadth.get("available"))
    turnover_available = bool(isinstance(breadth, Mapping) and breadth.get("turnover_available"))
    coverage_ratio = (
        (0.4 if required_indexes_covered else 0.0)
        + (0.25 if breadth_available else 0.0)
        + (0.15 if turnover_available else 0.0)
    )
    issues: List[str] = []
    if not identity_consistent:
        issues.append("核心指数身份校验失败")
    if not freshness_passed:
        issues.append("核心指数时间缺失或已过期")
    if not breadth_available:
        issues.append("市场涨跌宽度不可用")
    if not turnover_available:
        issues.append("成交额样本不可用")
    optional_gaps = ["主力资金流尚未接入可核验数据源", "全市场新闻聚合尚未接入可核验数据源"]
    return {
        "identity_consistent": identity_consistent,
        "freshness_passed": freshness_passed,
        "required_indexes_covered": required_indexes_covered,
        "market_breadth_available": breadth_available,
        "turnover_available": turnover_available,
        "coverage_ratio": round(coverage_ratio, 4),
        "fact_consistent": identity_consistent,
        "issues": issues,
        "optional_gaps": optional_gaps,
    }


def _market_report(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    outputs = context.get("state", {}).get("outputs", {})
    benchmark_result = outputs.get("benchmarks", {}) if isinstance(outputs, Mapping) else {}
    breadth = outputs.get("breadth", {}) if isinstance(outputs, Mapping) else {}
    quality = outputs.get("quality", {}) if isinstance(outputs, Mapping) else {}
    snapshots = benchmark_result.get("snapshots", {}) if isinstance(benchmark_result, Mapping) else {}
    lines = ["## 今日 A 股市场概览", "", "### 核心指数", ""]
    entities: List[str] = []
    for identity in _CN_BENCHMARKS:
        snapshot = snapshots.get(identity["symbol"], {}) if isinstance(snapshots, Mapping) else {}
        realtime = snapshot.get("realtime", {}) if isinstance(snapshot, Mapping) else {}
        if not isinstance(realtime, Mapping) or not realtime:
            if identity["required"]:
                lines.append(f"- {identity['name']}（{identity['provider_symbol']}）：数据缺失")
            continue
        entities.append(identity["canonical_symbol"])
        change_pct = realtime.get("change_pct", realtime.get("changePercent"))
        lines.append(
            f"- {identity['name']}（{identity['provider_symbol']}）：{_fmt(realtime.get('price'))}，"
            f"涨跌 {_fmt(change_pct)}%，成交额 {_fmt(realtime.get('amount'))}"
        )
    lines.extend(["", "### 市场宽度与成交", ""])
    if isinstance(breadth, Mapping) and breadth.get("available"):
        lines.extend([
            f"- 样本：成交额排序前 {int(breadth.get('sample_size', 0) or 0)} 只 A 股",
            f"- 上涨 / 下跌 / 平盘：{breadth.get('advancers', 0)} / {breadth.get('decliners', 0)} / {breadth.get('unchanged', 0)}",
            f"- 涨跌家数比：{_fmt(breadth.get('advance_decline_ratio'))}",
            f"- 样本成交额：{_fmt(breadth.get('sample_turnover'))}（仅样本，不冒充全市场总额）",
        ])
    else:
        lines.append(f"- 市场宽度不可用：{breadth.get('error', '数据源未返回结果') if isinstance(breadth, Mapping) else '数据源未返回结果'}")
    lines.extend(["", "### 数据边界", ""])
    for issue in quality.get("issues", []) if isinstance(quality, Mapping) else []:
        lines.append(f"- 必需质量项：{normalize_provider_text(issue)}")
    for gap in quality.get("optional_gaps", []) if isinstance(quality, Mapping) else []:
        lines.append(f"- {normalize_provider_text(gap)}")
    lines.extend([
        "",
        "本概览只陈述经过标的身份和时间校验的数据，不以个股代替指数，不构成投资建议。",
        "",
        f"数据时间：{benchmark_result.get('fetched_at', '') if isinstance(benchmark_result, Mapping) else ''}",
    ])
    return {
        "user_report": "\n".join(lines).strip(),
        "entities": entities,
        "data_timestamp": benchmark_result.get("fetched_at", "") if isinstance(benchmark_result, Mapping) else "",
        "sources": list(dict.fromkeys(filter(None, ["tencent-market", breadth.get("source", "") if isinstance(breadth, Mapping) else ""]))),
        "quality": dict(quality) if isinstance(quality, Mapping) else {},
    }


def _screening(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    market = str(inputs.get("market", "all")).strip().lower() or "all"
    refresh = bool(inputs.get("refresh", False))
    if market not in {"all", "cn", "hk", "us", "etf"}:
        raise ValueError("market 必须是 cn、hk、us、etf 或 all")
    if refresh:
        if market == "all":
            raise ValueError("刷新选股时必须指定具体市场")
        from src.investment.command_bus import InvestmentAgentClient

        return InvestmentAgentClient().issue(
            "run_screening", {"market": market}, requested_by="skill-runtime", timeout=240
        )
    from src.screening import latest_screening

    markets = cfg.enabled_markets if market == "all" else [market]
    return {
        "market": market,
        "results": {item: latest_screening(item) for item in markets},
        "refreshed": False,
    }


def _screening_report(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    value = context.get("state", {}).get("outputs", {}).get("screen", {})
    return {
        "user_report": "## 选股结果\n\n```json\n"
        + __import__("json").dumps(value, ensure_ascii=False, indent=2, default=str)[:24000]
        + "\n```\n\n结果来自确定性筛选流程，不构成投资建议。"
    }


def _portfolio_snapshot(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.portfolio import account as account_store

    portfolio = account_store.load()
    market = str(inputs.get("market", "")).strip().lower()
    accounts = portfolio.get("accounts", {}) if isinstance(portfolio, Mapping) else {}
    if market:
        accounts = {market: accounts.get(market, {})}
    return {"mode": cfg.trading.get("mode", "paper"), "accounts": accounts}


def _portfolio_report(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    value = context.get("state", {}).get("outputs", {}).get("portfolio", {})
    accounts = value.get("accounts", {}) if isinstance(value, Mapping) else {}
    lines = ["## 组合概览", ""]
    for market, account in accounts.items():
        holdings = account.get("holdings", []) if isinstance(account, Mapping) else []
        total = float(account.get("totalCapital", 0) or 0)
        cash = float(account.get("cash", 0) or 0)
        cash_pct = cash / total * 100 if total > 0 else 0.0
        high_water = float(account.get("highWaterMark", total) or total)
        drawdown_pct = (total / high_water - 1) * 100 if high_water > 0 else 0.0
        risk = cfg.market_config(str(market)).get("risk", {}) if str(market) in {"cn", "hk", "us", "etf"} else {}
        max_position = float(risk.get("single_stock_max_pct", 100) or 100)
        min_cash = float(risk.get("min_cash_reserve_pct", 0) or 0)
        max_drawdown = float(risk.get("max_drawdown_pct", -100) or -100)
        weighted = []
        for holding in holdings:
            shares = float(holding.get("shares", holding.get("quantity", 0)) or 0)
            price = float(
                holding.get("lastPrice", holding.get("last_price", 0))
                or holding.get("costPrice", holding.get("cost", 0))
                or 0
            )
            value = shares * price
            weighted.append((holding, value / total * 100 if total > 0 else 0.0))
        lines.extend([
            f"### {str(market).upper()}",
            f"- 现金：{cash:,.2f}（{cash_pct:.2f}%）",
            f"- 总资产：{total:,.2f}",
            f"- 相对高水位回撤：{drawdown_pct:.2f}%",
            f"- 持仓数量：{len(holdings)}",
        ])
        for holding, weight in sorted(weighted, key=lambda item: item[1], reverse=True)[:30]:
            lines.append(
                f"- {holding.get('name') or holding.get('code')}："
                f"{holding.get('shares', holding.get('quantity', 0))} 股，成本 "
                f"{_fmt(holding.get('costPrice', holding.get('cost')))}，估算权重 {weight:.2f}%"
            )
        flags = []
        if cash_pct < min_cash:
            flags.append(f"现金比例 {cash_pct:.2f}% 低于最低储备 {min_cash:.2f}%")
        if drawdown_pct < max_drawdown:
            flags.append(f"回撤 {drawdown_pct:.2f}% 超过硬限制 {max_drawdown:.2f}%")
        concentrated = [
            f"{holding.get('name') or holding.get('code')} {weight:.2f}%"
            for holding, weight in weighted
            if weight > max_position
        ]
        if concentrated:
            flags.append("单一持仓超限：" + "、".join(concentrated))
        lines.append("- 风险检查：" + ("；".join(flags) if flags else "未发现现金、回撤或单一持仓硬阈值超限"))
        lines.append("")
    lines.append("以上为模拟账户状态快照。")
    return {"user_report": "\n".join(lines).strip()}


def _optimizer(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.investment.command_bus import InvestmentAgentClient

    market = str(inputs.get("market", "")).strip().lower()
    return InvestmentAgentClient().issue(
        "run_optimizer",
        {"market": market, "symbols": inputs.get("symbols") or None},
        requested_by="skill-runtime",
        timeout=240,
    )


def _optimizer_report(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    value = context.get("state", {}).get("outputs", {}).get("optimize", {})
    return {
        "user_report": "## 组合优化结果\n\n```json\n"
        + __import__("json").dumps(value, ensure_ascii=False, indent=2, default=str)[:24000]
        + "\n```\n\n以上为模拟研究结果。"
    }


def _run_cycle(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.investment.command_bus import InvestmentAgentClient
    from src.investment.reporting import format_cycle_result

    market = str(inputs.get("market", "")).strip().lower()
    progress = context.get("progress_callback")
    result = InvestmentAgentClient().issue(
        "run_cycle",
        {"market": market, "label": str(inputs.get("label", "skill"))[:40]},
        requested_by=str(context.get("requested_by", "skill-runtime"))[:80],
        progress_callback=progress if callable(progress) else None,
    )
    return {**result, "user_report": format_cycle_result(result)}


def _run_scheduled_cycle(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.investment.command_bus import InvestmentAgentClient
    from src.investment.reporting import format_cycle_result

    market = str(inputs.get("market", "")).strip().lower()
    progress = context.get("progress_callback")
    result = InvestmentAgentClient().issue(
        "run_scheduled_cycle",
        {
            "market": market,
            "cycle_type": str(inputs.get("cycle_type", "intraday")),
            "label": str(inputs.get("label", "scheduled-skill"))[:40],
            "time": str(inputs.get("time", ""))[:10],
            "catch_up": bool(inputs.get("catch_up", False)),
            "scheduled_at": str(inputs.get("scheduled_at", "")),
        },
        requested_by=str(context.get("requested_by", "skill-scheduler"))[:80],
        progress_callback=progress if callable(progress) else None,
    )
    return {**result, "user_report": format_cycle_result(result)}


def _runtime_status(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.investment.status import runtime_status

    return {"ok": True, **runtime_status()}


def _investment_control(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.investment.command_bus import InvestmentAgentClient

    action = str(inputs.get("action", "status")).strip().lower()
    if action in {"reset", "reset_account", "reset_paper_account"}:
        return InvestmentAgentClient().issue(
            "reset_paper_account",
            {"market": inputs.get("market"), "reason": inputs.get("reason", "用户要求重置模拟账户")},
            requested_by="skill-runtime",
            timeout=60,
        )
    allowed = {"status", "pause", "resume", "kill", "reset_kill", "set_mode", "set_strategy", "reflect"}
    if action not in allowed:
        raise ValueError("不支持的管理动作")
    value = inputs.get("value", "")
    payload = {
        "reason": str(inputs.get("reason", ""))[:500],
        "mode": value,
        "profile": value,
        "market": inputs.get("market") or value,
        "limit": 5,
    }
    return InvestmentAgentClient().issue(action, payload, requested_by="skill-runtime", timeout=60)


def _reset_account(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.investment.command_bus import InvestmentAgentClient

    return InvestmentAgentClient().issue(
        "reset_paper_account",
        {"market": inputs.get("market"), "reason": inputs.get("reason", "用户要求重置模拟账户")},
        requested_by="skill-runtime",
        timeout=60,
    )


def _system_capabilities(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.manager.skill_registry import SkillRegistry

    return {"skills": SkillRegistry().records(), "actions": ActionRegistry(include_dynamic=True).catalog()}


def _system_report(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    value = context.get("state", {}).get("outputs", {}).get("catalog", {})
    skills = value.get("skills", []) if isinstance(value, Mapping) else []
    actions = value.get("actions", []) if isinstance(value, Mapping) else []
    lines = ["## Agent Harness 能力", "", "### Skills"]
    lines.extend(f"- {item.get('name')}: {item.get('description')}" for item in skills)
    lines.extend(["", "### Internal Actions", f"共 {len(actions)} 个受控 Action。"])
    return {"user_report": "\n".join(lines)}


def _incident_inspect(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.manager.incident_repair import IncidentRepairSupervisor

    return IncidentRepairSupervisor().inspect(str(inputs.get("execution_id", "")))


def _incident_replay(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    from src.manager.incident_repair import IncidentRepairSupervisor

    outputs = context.get("state", {}).get("outputs", {})
    incident = outputs.get("incident", {}) if isinstance(outputs, Mapping) else {}
    execution_id = str(inputs.get("execution_id") or incident.get("execution_id") or "")
    diagnosis = incident.get("diagnosis", {}) if isinstance(incident, Mapping) else {}
    if isinstance(diagnosis, Mapping) and diagnosis.get("category") == "external_blocker":
        return {
            "status": "external_blocker",
            "semantic_validation_passed": False,
            "original_execution_id": execution_id,
        }
    return IncidentRepairSupervisor().replay(execution_id)


def _incident_report(inputs: Mapping[str, Any], context: Mapping[str, Any]) -> Dict[str, Any]:
    outputs = context.get("state", {}).get("outputs", {})
    incident = outputs.get("incident", {}) if isinstance(outputs, Mapping) else {}
    replay = outputs.get("replay", {}) if isinstance(outputs, Mapping) else {}
    diagnosis = incident.get("diagnosis", {}) if isinstance(incident, Mapping) else {}
    category = str(diagnosis.get("category", "unknown")) if isinstance(diagnosis, Mapping) else "unknown"
    verified = bool(isinstance(replay, Mapping) and replay.get("semantic_validation_passed"))
    lines = [
        "## 故障诊断与回放",
        "",
        f"- 原执行：{incident.get('execution_id', '') if isinstance(incident, Mapping) else ''}",
        f"- 分类：{category}",
        f"- 是否可由本地代码修复：{'是' if diagnosis.get('repairable') else '否'}",
        f"- 原请求回放：{'通过' if verified else '未通过'}",
    ]
    if category == "external_blocker":
        lines.extend(["", "该问题属于外部账户、网络、权限或供应商状态；系统不会用修改代码伪造修复成功。"])
    elif not verified:
        lines.extend(["", "当前只完成了结构化诊断；只有补丁测试通过且原始请求语义回放通过，才会标记修复完成。"])
    return {
        "user_report": "\n".join(lines),
        "diagnosis_category": category,
        "semantic_validation_passed": verified,
        "sources": ["execution-trace", "semantic-replay"],
    }


class ActionRegistry:
    """Internal, permissioned actions. These are never exposed to the top-level model."""

    def __init__(self, *, include_dynamic: bool = True) -> None:
        self._actions: Dict[str, ActionSpec] = {}
        self._install_core()
        if include_dynamic:
            self._install_dynamic()

    def register(
        self,
        name: str,
        handler: ActionHandler,
        description: str,
        *,
        session_scopes: set[str],
        side_effect_level: str,
    ) -> None:
        if side_effect_level not in SIDE_EFFECT_LEVELS:
            raise ValueError(f"未知副作用等级: {side_effect_level}")
        if not session_scopes or not session_scopes <= SESSION_SCOPES:
            raise ValueError(f"Action 会话范围无效: {session_scopes}")
        self._actions[name] = ActionSpec(
            name=name,
            handler=handler,
            description=description,
            session_scopes=frozenset(session_scopes),
            side_effect_level=side_effect_level,
        )

    def _install_core(self) -> None:
        research = {"investment_research"}
        portfolio = {"portfolio_management"}
        execution = {"investment_execution"}
        self.register("security.resolve", _security_resolve, "解析证券名称或代码", session_scopes=research, side_effect_level="read_only")
        self.register("security.snapshot", _security_snapshot, "获取证券行情快照", session_scopes=research, side_effect_level="read_only")
        self.register("security.research", _security_research, "获取公司新闻与基本面数据", session_scopes=research, side_effect_level="read_only")
        self.register("security.validate", _security_validate, "校验证券身份、时间与证据覆盖率", session_scopes=research, side_effect_level="read_only")
        self.register("security.synthesize", _security_synthesis, "基于可审计证据生成证券综合分析", session_scopes=research, side_effect_level="read_only")
        self.register("report.security", _security_report, "生成有证据边界的证券研究报告", session_scopes=research, side_effect_level="read_only")
        self.register("market.benchmarks", _market_benchmarks, "获取并校验 A 股核心指数", session_scopes=research, side_effect_level="read_only")
        self.register("market.breadth", _market_breadth, "获取 A 股涨跌宽度与成交额样本", session_scopes=research, side_effect_level="read_only")
        self.register("market.validate", _market_quality, "校验市场概览身份、时间和覆盖率", session_scopes=research, side_effect_level="read_only")
        self.register("report.market_overview", _market_report, "生成带数据边界的 A 股市场概览", session_scopes=research, side_effect_level="read_only")
        self.register("market.screen", _screening, "读取或刷新确定性选股结果", session_scopes=research, side_effect_level="read_only")
        self.register("report.screening", _screening_report, "生成选股报告", session_scopes=research, side_effect_level="read_only")
        self.register("portfolio.snapshot", _portfolio_snapshot, "读取模拟组合", session_scopes=portfolio, side_effect_level="read_only")
        self.register("report.portfolio", _portfolio_report, "生成组合概览", session_scopes=portfolio, side_effect_level="read_only")
        self.register("portfolio.optimize", _optimizer, "运行组合优化器", session_scopes=portfolio, side_effect_level="portfolio_write")
        self.register("report.optimizer", _optimizer_report, "生成组合优化报告", session_scopes=portfolio, side_effect_level="read_only")
        self.register("investment.run_cycle", _run_cycle, "运行完整模拟投资周期", session_scopes=execution, side_effect_level="investment_execution")
        self.register("investment.run_scheduled_cycle", _run_scheduled_cycle, "运行结构化定时投资周期", session_scopes=execution, side_effect_level="investment_execution")
        self.register("investment.control", _investment_control, "管理投资运行状态", session_scopes=execution, side_effect_level="investment_execution")
        self.register("account.reset_paper", _reset_account, "重置一个模拟账户", session_scopes=execution, side_effect_level="investment_execution")
        self.register("runtime.status", _runtime_status, "读取投资运行状态", session_scopes=execution | portfolio, side_effect_level="read_only")
        self.register("system.capabilities", _system_capabilities, "读取 Harness Skill 和 Action 目录", session_scopes={"system_admin"}, side_effect_level="read_only")
        self.register("report.system", _system_report, "生成 Harness 能力报告", session_scopes={"system_admin"}, side_effect_level="read_only")
        self.register("incident.inspect", _incident_inspect, "读取最近失败轨迹并形成结构化诊断", session_scopes={"system_admin"}, side_effect_level="read_only")
        self.register("incident.replay", _incident_replay, "以原始请求回放只读 Skill 并做语义验证", session_scopes={"system_admin"}, side_effect_level="system_admin")
        self.register("report.incident", _incident_report, "报告故障分类与语义回放结果", session_scopes={"system_admin"}, side_effect_level="read_only")

    def _install_dynamic(self) -> None:
        try:
            from src.manager.capabilities import CapabilityRegistry

            manifests = CapabilityRegistry().catalog().get("tools", [])
        except Exception:
            manifests = []
        for manifest in manifests:
            try:
                module = str(manifest["module"])
                function = str(manifest["function"])
                target = getattr(importlib.import_module(module), function)
                schema = manifest.get("parameters_schema", {})
                properties = set(schema.get("properties", {})) if isinstance(schema, Mapping) else set()

                def invoke(
                    inputs: Mapping[str, Any],
                    context: Mapping[str, Any],
                    fn=target,
                    allowed=properties,
                ):
                    arguments = {key: value for key, value in inputs.items() if key in allowed}
                    return fn(**arguments)

                scopes = {
                    str(value) for value in manifest.get("session_scopes", ["system_admin"])
                    if str(value) in SESSION_SCOPES
                } or {"system_admin"}
                side_effect = str(manifest.get("side_effect_level", "read_only"))
                if side_effect not in SIDE_EFFECT_LEVELS:
                    side_effect = "read_only"
                self.register(
                    f"custom.{manifest['name']}",
                    invoke,
                    str(manifest.get("description", "自建 Skill Action")),
                    session_scopes=scopes,
                    side_effect_level=side_effect,
                )
            except Exception:
                continue

    def get(self, name: str) -> ActionSpec:
        if name not in self._actions:
            raise KeyError(f"未知 Action: {name}")
        return self._actions[name]

    def execute(
        self,
        name: str,
        inputs: Mapping[str, Any],
        context: Mapping[str, Any],
        *,
        session_scope: str,
    ) -> Any:
        action = self.get(name)
        if session_scope not in action.session_scopes:
            raise PermissionError(f"会话 {session_scope} 无权调用 Action {name}")
        return action.handler(dict(inputs), context)

    def catalog(self) -> List[Dict[str, Any]]:
        return [
            {
                "name": action.name,
                "description": action.description,
                "session_scopes": sorted(action.session_scopes),
                "side_effect_level": action.side_effect_level,
            }
            for action in self._actions.values()
        ]

    @property
    def names(self) -> set[str]:
        return set(self._actions)
