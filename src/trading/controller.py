from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from src.config import cfg
from src.data import fetcher
from src.llm.registry import resolve_llm
from src.portfolio import account as account_store
from src.runtime_lock import atomic_claim
from src.screening import ScreeningOutcome, run_screening
from src.trading.broker import execute_orders
from src.trading.control import activate_kill_switch, load_state
from src.trading.risk import build_orders

logger = logging.getLogger("investment-auto.autonomous")
ROOT = Path(__file__).resolve().parents[2]
AUDIT_DIR = ROOT / "runtime" / "trading" / "audit"
CYCLE_LOCK_DIR = ROOT / "runtime" / "trading" / "locks"

_ROLE_INSTRUCTIONS = {
    "analyst": "你是技术面分析员。只依据给定行情、K线与指标评估趋势、波动和关键价位，不得虚构数据。",
    "researcher": "你是市场研究员。结合宏观摘要与给定标的资料，分析催化剂、相关性和数据缺口，不得补写未知基本面。",
    "quant_analyst": "你是量化分析员。审阅组合权重、收益风险指标和账户暴露，识别样本内偏差和集中度风险。",
    "risk_chairman": "你是独立风险负责人。优先识别回撤、流动性、仓位、结算和数据质量风险，可以建议全部 HOLD。",
}


def _now(value: Optional[datetime] = None) -> datetime:
    timezone = ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai"))
    if value is None:
        return datetime.now(timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _env_bool(name: str, default: bool) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def autonomous_enabled(config: Optional[Mapping[str, Any]] = None) -> bool:
    settings = config or cfg.autonomous
    return _env_bool("AUTONOMOUS_TRADING_ENABLED", bool(settings.get("enabled", False)))


def _dedupe(values: Iterable[Any], limit: int) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        symbol = str(value or "").strip().upper()
        if not symbol or symbol in seen or not re.fullmatch(r"[A-Z0-9.\-]{1,32}", symbol):
            continue
        seen.add(symbol)
        result.append(symbol)
        if len(result) >= limit:
            break
    return result


def _universe(market: str, account: Mapping[str, Any], config: Mapping[str, Any]) -> List[str]:
    from src.optimizer.runner import default_symbols

    configured = config.get("universe", {}).get(market) or default_symbols(market)
    held = [item.get("code") for item in account.get("holdings", [])]
    limit = max(len(_dedupe(held, 10_000)), int(config.get("max_universe_size", 10)))
    return _dedupe([*held, *configured], limit)


def _screening_outcome(
    market: str,
    account: Mapping[str, Any],
    config: Mapping[str, Any],
    current: datetime,
) -> ScreeningOutcome:
    from src.optimizer.runner import default_symbols

    held = [item.get("code") for item in account.get("holdings", [])]
    configured = config.get("universe", {}).get(market) or []
    return run_screening(
        market,
        held_symbols=held,
        configured_symbols=configured,
        fallback_symbols=default_symbols(market),
        settings=cfg.screening,
        autonomous_config=config,
        snapshot_loader=_fetch_snapshots,
        now=current,
    )


def run_screening_preview(market: str = "cn", *, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Run the non-trading screening stage for UI/Agent inspection."""

    current = _now(now)
    normalized = str(market or "").strip().lower()
    if normalized not in {"cn", "hk", "us", "etf"}:
        raise ValueError(f"不支持的市场: {market}")
    lock_path = CYCLE_LOCK_DIR / f"{normalized}.lock"
    with atomic_claim(lock_path, stale_seconds=int(cfg.autonomous.get("cycle_timeout_seconds", 300)) + 60) as claimed:
        if not claimed:
            from src.screening import latest_screening

            return {
                "market": normalized,
                "generated_at": current.isoformat(timespec="seconds"),
                "status": "in_progress",
                "latest": latest_screening(normalized),
            }
        outcome = _screening_outcome(
            normalized,
            account_store.account(normalized),
            cfg.autonomous,
            current,
        )
        return outcome.audit


def _price(snapshot: Mapping[str, Any]) -> float:
    realtime = snapshot.get("realtime", snapshot)
    if not isinstance(realtime, Mapping):
        return 0.0
    try:
        return float(realtime.get("price", realtime.get("last", realtime.get("close", 0))) or 0)
    except (TypeError, ValueError):
        return 0.0


def _account_for_agents(account: Mapping[str, Any]) -> Dict[str, Any]:
    """Minimize portfolio data sent to external LLM providers."""
    holdings = []
    for item in account.get("holdings", []):
        holdings.append({
            "code": item.get("code"),
            "name": item.get("name", ""),
            "quantity": item.get("shares", item.get("quantity", 0)),
            "cost": item.get("costPrice", item.get("cost", 0)),
            "last_price": item.get("lastPrice"),
            "high_price": item.get("highPrice"),
            "take_profit_1_done": bool(item.get("takeProfit1Done")),
            "take_profit_2_done": bool(item.get("takeProfit2Done")),
        })
    return {
        "total_capital": account.get("totalCapital", 0),
        "cash": account.get("cash", 0),
        "high_water_mark": account.get("highWaterMark"),
        "holdings": holdings,
        "trade_count": len(account.get("tradeHistory", [])),
    }


def _fetch_snapshots(symbols: Sequence[str], workers: int) -> tuple[Dict[str, Any], Dict[str, str]]:
    snapshots: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(symbols) or 1))) as executor:
        futures = {executor.submit(fetcher.snapshot, symbol): symbol for symbol in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                payload = future.result()
                if not isinstance(payload, dict) or payload.get("error"):
                    raise ValueError(str(payload.get("error", "行情返回格式错误")))
                snapshots[symbol] = payload
            except Exception as exc:
                errors[symbol] = str(exc)[:500]
    return snapshots, errors


def _enrich_research_packets(
    market: str,
    snapshots: Dict[str, Any],
    *,
    workers: int,
    settings: Mapping[str, Any],
) -> Dict[str, str]:
    """Attach auditable company news/fundamentals before any Agent is called."""
    from src.data.research import fetch_research_packet

    errors: Dict[str, str] = {}
    ttl = max(5, int(settings.get("research_data_ttl_minutes", 30)))
    timeout = max(3, min(30, int(settings.get("research_data_timeout_seconds", 12))))
    with ThreadPoolExecutor(max_workers=max(1, min(workers, len(snapshots) or 1))) as executor:
        futures = {
            executor.submit(
                fetch_research_packet,
                market,
                symbol,
                ttl_minutes=ttl,
                timeout=timeout,
            ): symbol
            for symbol in snapshots
        }
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                packet = future.result()
                snapshot = snapshots.get(symbol)
                if not isinstance(snapshot, dict):
                    continue
                for key in ("news", "fundamentals", "sentiment"):
                    value = packet.get(key)
                    if value:
                        snapshot[key] = value
                if packet.get("errors"):
                    snapshot["research_data_errors"] = packet["errors"]
                snapshot["research_data_sources"] = packet.get("sources", [])
            except Exception as exc:
                errors[symbol] = str(exc)[:1000]
    return errors


def _agent_prompt(role: str, context: Mapping[str, Any]) -> List[Dict[str, str]]:
    schema = {
        "summary": "简短结论",
        "signals": [{"symbol": "代码", "score": "-1到1", "confidence": "0到1", "reason": "证据"}],
        "risks": ["风险"],
        "data_gaps": ["缺失数据"],
    }
    return [
        {"role": "system", "content": _ROLE_INSTRUCTIONS.get(role, _ROLE_INSTRUCTIONS["analyst"])},
        {"role": "user", "content": (
            "你是自主模拟交易委员会的一名独立成员。输出纯 JSON，不要 Markdown，不要调用工具。"
            f"\n输出结构示例：{json.dumps(schema, ensure_ascii=False)}"
            f"\n输入数据：{json.dumps(context, ensure_ascii=False)[:24000]}"
        )},
    ]


def _run_committee_member(role: str, context: Mapping[str, Any]) -> Dict[str, Any]:
    llm = resolve_llm(role=role)
    text = llm.chat(_agent_prompt(role, context), temperature=0.15, max_tokens=1800)
    if not text or not text.strip():
        raise RuntimeError(f"{role} returned an empty response")
    return {"role": role, "response": text.strip()[:12000]}


def _parse_json_object(text: str) -> Dict[str, Any]:
    cleaned = text.strip()
    fence = chr(96) * 3
    if cleaned.startswith(fence):
        cleaned = re.sub(r"^[A-Za-z]*\s*", "", cleaned[len(fence):], count=1)
        if cleaned.endswith(fence):
            cleaned = cleaned[:-len(fence)].rstrip()
    try:
        value = json.loads(cleaned)
    except json.JSONDecodeError:
        start, end = cleaned.find("{"), cleaned.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("交易主席没有返回 JSON 对象")
        value = json.loads(cleaned[start:end + 1])
    if not isinstance(value, dict):
        raise ValueError("交易主席输出必须是 JSON 对象")
    return value


def _normalize_decisions(payload: Mapping[str, Any], limit: int) -> List[Dict[str, Any]]:
    raw = payload.get("decisions", [])
    if not isinstance(raw, list):
        raise ValueError("decisions 必须是数组")
    decisions: List[Dict[str, Any]] = []
    for index, item in enumerate(raw[:limit]):
        if not isinstance(item, dict):
            continue
        normalized = dict(item)
        normalized["symbol"] = str(item.get("symbol", "")).strip().upper()
        normalized["action"] = str(item.get("action", "HOLD")).strip().upper()
        normalized["decision_id"] = str(item.get("decision_id") or f"ai-{index + 1}")[:128]
        decisions.append(normalized)
    return decisions


def _chair_decision(
    market: str,
    context: Mapping[str, Any],
    committee: Sequence[Mapping[str, Any]],
    config: Mapping[str, Any],
) -> Dict[str, Any]:
    schema = {
        "thesis": "本轮组合级判断",
        "decisions": [{
            "decision_id": "唯一短标识",
            "symbol": "必须来自 allowed_symbols",
            "action": "BUY|SELL|HOLD",
            "target_weight": "占账户权益比例，0到1",
            "confidence": "0到1",
            "reason": "引用输入证据",
        }],
    }
    messages = [
        {"role": "system", "content": (
            "你是自主模拟交易委员会主席。你只制定目标仓位，不负责绕过风控，也不能假设未知数据。"
            "委员会意见冲突、数据不足或风险过高时应输出 HOLD。不得输出允许池以外的标的。"
        )},
        {"role": "user", "content": (
            f"市场：{market}\n允许池：{json.dumps(context.get('allowed_symbols', []), ensure_ascii=False)}"
            f"\n账户、行情与约束：{json.dumps(context, ensure_ascii=False)[:22000]}"
            f"\n独立委员意见：{json.dumps(list(committee), ensure_ascii=False)[:22000]}"
            f"\n只输出纯 JSON：{json.dumps(schema, ensure_ascii=False)}"
        )},
    ]
    llm = resolve_llm(role="trader")
    text = llm.chat(messages, temperature=0.1, max_tokens=2200)
    payload = _parse_json_object(text)
    payload["decisions"] = _normalize_decisions(payload, int(config.get("max_decisions", 10)))
    return payload


def _staged_workflow_enabled(config: Mapping[str, Any]) -> bool:
    settings = config.get("agent_workflow", {})
    return isinstance(settings, Mapping) and bool(settings.get("enabled", False))


def _write_audit(audit: Mapping[str, Any], now: datetime, market: str, label: str) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    safe_label = re.sub(r"[^A-Za-z0-9_-]+", "-", label)[:40] or "cycle"
    filename = now.strftime("%Y%m%d-%H%M%S-%f") + f"-{market}-{safe_label}.json"
    path = AUDIT_DIR / filename
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(audit, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return path


def run_autonomous_cycle(
    market: str = "cn",
    *,
    label: str = "manual",
    now: Optional[datetime] = None,
    macro_excerpt: str = "",
    optimizer_hint: Optional[Mapping[str, Any]] = None,
    catch_up: bool = False,
    dry_run: bool = False,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    def progress(message: str) -> None:
        if progress_callback is not None:
            try:
                progress_callback(message)
            except Exception:
                logger.debug("Progress callback failed", exc_info=True)

    current = _now(now)
    market = market.lower().strip()
    from src.investment.mandate import effective_configs, get_mandate

    mandate = get_mandate()
    market_config = cfg.market_config(market)
    config, trading_config, market_config = effective_configs(
        cfg.autonomous,
        cfg.trading,
        market_config,
        mandate,
    )
    base: Dict[str, Any] = {
        "market": market,
        "label": label,
        "generated_at": current.isoformat(timespec="seconds"),
        "mode": "dry_run" if dry_run else "paper",
        "mandate": {
            "profile": mandate.get("profile"),
            "display_name": mandate.get("display_name"),
            "objective": mandate.get("objective"),
            "risk_policy_version": mandate.get("risk_policy_version"),
            "prompt_version": mandate.get("prompt_version"),
            "version": mandate.get("version"),
        },
    }
    if not autonomous_enabled(config):
        return {**base, "status": "disabled"}
    if str(cfg.trading.get("mode", "paper")).lower() != "paper":
        return {**base, "status": "blocked", "reason": "自主模块只支持模拟交易"}
    control = load_state()
    if control.get("paused") or control.get("kill_switch"):
        return {**base, "status": "paused", "control": control}
    if catch_up and not bool(config.get("trade_on_catch_up", False)):
        return {**base, "status": "skipped", "reason": "补跑任务禁止执行历史时点交易"}

    lock_path = CYCLE_LOCK_DIR / f"{market}.lock"
    with atomic_claim(lock_path, stale_seconds=int(config.get("cycle_timeout_seconds", 240)) + 60) as claimed:
        if not claimed:
            return {**base, "status": "in_progress"}

        account = account_store.account(market)
        progress("正在从全市场筛选优质候选，并合并已有持仓…")
        try:
            screening_outcome = _screening_outcome(market, account, config, current)
            symbols = screening_outcome.symbols
            snapshots = screening_outcome.snapshots
            market_errors = screening_outcome.market_data_errors
            screening_audit = screening_outcome.audit
        except Exception as exc:
            logger.exception("Stock screening failed for %s; using the legacy universe", market)
            symbols = _universe(market, account, config)
            snapshots, market_errors = _fetch_snapshots(symbols, int(config.get("market_data_workers", 4)))
            screening_audit = {
                "generated_at": current.isoformat(timespec="seconds"),
                "market": market,
                "status": "error_fallback",
                "source": "legacy-universe",
                "selected_symbols": symbols,
                "allowed_symbols": symbols,
                "error": str(exc)[:1000],
            }
        prices = {symbol: _price(snapshot) for symbol, snapshot in snapshots.items()}
        prices = {symbol: price for symbol, price in prices.items() if price > 0}
        selected_names = {
            str(item.get("symbol", "")).upper(): str(item.get("name", "")).strip()
            for item in screening_audit.get("selected", [])
            if isinstance(item, Mapping) and item.get("symbol") and item.get("name")
        }
        security_names = {
            symbol: str(snapshot.get("realtime", {}).get("name", "")).strip() or selected_names.get(symbol, "")
            for symbol, snapshot in snapshots.items()
            if isinstance(snapshot, Mapping)
        }
        security_names.update({symbol: name for symbol, name in selected_names.items() if name})
        minimum_prices = int(config.get("minimum_priced_symbols", 2))
        if len(prices) < minimum_prices:
            audit = {
                **base,
                "status": "blocked",
                "reason": f"有效行情不足 {minimum_prices} 个标的",
                "allowed_symbols": symbols,
                "screening": screening_audit,
                "market_data_errors": market_errors,
            }
            path = _write_audit(audit, current, market, label)
            return {**audit, "audit_file": str(path)}

        progress("正在为每只候选预取公司新闻、基本面和可用情绪数据…")
        research_data_errors = _enrich_research_packets(
            market,
            snapshots,
            workers=int(config.get("market_data_workers", 4)),
            settings=config.get("agent_workflow", {}),
        )

        compact_snapshots = {
            symbol: {
                "price": prices.get(symbol),
                "realtime": snapshot.get("realtime", {}),
                "indicators": snapshot.get("indicators", {}),
                "history": snapshot.get("history", [])[-60:],
                "fundamentals": snapshot.get("fundamentals", {}),
                "news": snapshot.get("news", []),
                "sentiment": snapshot.get("sentiment", {}),
                "research_data_sources": snapshot.get("research_data_sources", []),
                "research_data_errors": snapshot.get("research_data_errors", {}),
            }
            for symbol, snapshot in snapshots.items()
            if symbol in prices
        }
        context: Dict[str, Any] = {
            "as_of": current.isoformat(timespec="seconds"),
            "market": market,
            "allowed_symbols": symbols,
            "account": _account_for_agents(account),
            "snapshots": compact_snapshots,
            "market_data_errors": market_errors,
            "research_data_errors": research_data_errors,
            "macro_excerpt": macro_excerpt[:6000],
            "optimizer": dict(optimizer_hint or {}),
            "screening": screening_audit,
            "market_rules": market_config,
            "autonomous_constraints": dict(config),
            "investment_mandate": dict(mandate),
        }
        try:
            from src.investment.reflection import InvestmentReflectionService

            context["reflection_lessons"] = InvestmentReflectionService().recent(market, 5)
        except Exception:
            logger.debug("Could not load previous investment reflections", exc_info=True)
            context["reflection_lessons"] = []

        roles = [role for role in config.get("committee_roles", list(_ROLE_INSTRUCTIONS)) if role in _ROLE_INSTRUCTIONS]
        committee: List[Dict[str, Any]] = []
        committee_errors: Dict[str, str] = {}
        staged_workflow: Dict[str, Any] = {}
        progress(f"已获得 {len(symbols)} 个候选/持仓标的，正在运行多 Agent 研究链…")
        if not _staged_workflow_enabled(config):
            with ThreadPoolExecutor(max_workers=max(1, min(len(roles), int(config.get("agent_workers", 4))))) as executor:
                futures = {executor.submit(_run_committee_member, role, context): role for role in roles}
                for future in as_completed(futures):
                    role = futures[future]
                    try:
                        committee.append(future.result())
                    except Exception as exc:
                        committee_errors[role] = str(exc)[:1000]

            minimum_agents = int(config.get("minimum_agent_responses", 2))
            if len(committee) < minimum_agents:
                audit = {
                    **base,
                    "status": "blocked",
                    "reason": f"独立 Agent 成功数不足 {minimum_agents}",
                    "allowed_symbols": symbols,
                    "screening": screening_audit,
                    "committee": committee,
                    "committee_errors": committee_errors,
                    "market_data_errors": market_errors,
                }
                path = _write_audit(audit, current, market, label)
                return {**audit, "audit_file": str(path)}

        try:
            if _staged_workflow_enabled(config):
                from src.trading.agent_workflow import run_analysis_workflow

                staged_workflow = run_analysis_workflow(context, config)
                chair = dict(staged_workflow.get("portfolio_manager", {}))
                chair["decisions"] = _normalize_decisions(
                    chair,
                    max(int(config.get("max_decisions", 10)), len(symbols)),
                )
            else:
                chair = _chair_decision(market, context, committee, config)
            progress("Agent 已完成逐标的买入/观望/卖出判断，正在执行硬风控…")
            risk = build_orders(
                chair.get("decisions", []),
                account=account,
                prices=prices,
                allowed_symbols=symbols,
                market_config=market_config,
                autonomous_config=config,
                trading_config=trading_config,
                now=current,
            )
            should_execute = bool(config.get("auto_execute", True)) and not dry_run
            progress("硬风控完成，正在提交允许的模拟订单…" if should_execute else "硬风控完成，本轮仅生成决策…")
            execution = execute_orders(
                market,
                risk.get("orders", []) if should_execute else [],
                market_config=market_config,
                trading_mode=str(cfg.trading.get("mode", "paper")),
                now=current,
                equity_snapshot=float(risk.get("equity", 0) or 0),
                mark_prices=prices,
                security_names=security_names,
            ) if should_execute else {"fills": [], "rejected": [], "dry_run": True}
            required_liquidations = risk.get("circuit_liquidation_quantities", {})
            filled_quantities: Dict[str, int] = {}
            for fill in execution.get("fills", []):
                if str(fill.get("action", "")).upper() != "SELL":
                    continue
                symbol = str(fill.get("code", "")).upper()
                filled_quantities[symbol] = filled_quantities.get(symbol, 0) + int(fill.get("shares", 0) or 0)
            liquidation_complete = all(
                filled_quantities.get(str(symbol).upper(), 0) >= int(quantity)
                for symbol, quantity in required_liquidations.items()
            )
            if (
                should_execute
                and risk.get("circuit_breaker")
                and not execution.get("rejected")
                and liquidation_complete
            ):
                control = activate_kill_switch(
                    reason=f"{market.upper()} 账户达到最大回撤阈值",
                    updated_by="risk_engine",
                )
            status = "executed" if execution.get("fills") else "no_trade"
            progress("模拟执行完成，正在写入审计和最终报告…")
            audit = {
                **base,
                "status": status,
                "account_before": _account_for_agents(account),
                "allowed_symbols": symbols,
                "screening": screening_audit,
                "prices": prices,
                "market_data_errors": market_errors,
                "research_data_errors": research_data_errors,
                "committee": committee,
                "committee_errors": committee_errors,
                "agent_workflow": staged_workflow,
                "evidence_ref": staged_workflow.get("evidence_ref", "") if isinstance(staged_workflow, Mapping) else "",
                "chair": chair,
                "risk": risk,
                "execution": execution,
                "control": control,
            }
        except Exception as exc:
            logger.exception("Autonomous cycle failed for %s", market)
            # Fail closed for every discretionary decision, while still letting
            # deterministic hard stops / trailing stops / drawdown liquidation
            # protect an existing paper portfolio when the LLM graph fails.
            try:
                emergency_risk = build_orders(
                    [],
                    account=account,
                    prices=prices,
                    allowed_symbols=symbols,
                    market_config=market_config,
                    autonomous_config=config,
                    trading_config=trading_config,
                    now=current,
                )
                should_execute = bool(config.get("auto_execute", True)) and not dry_run
                emergency_orders = emergency_risk.get("orders", [])
                emergency_execution = execute_orders(
                    market,
                    emergency_orders if should_execute else [],
                    market_config=market_config,
                    trading_mode=str(cfg.trading.get("mode", "paper")),
                    now=current,
                    equity_snapshot=float(emergency_risk.get("equity", 0) or 0),
                    mark_prices=prices,
                    security_names=security_names,
                ) if should_execute else {"fills": [], "rejected": [], "dry_run": True}
                emergency_status = "protective_executed" if emergency_execution.get("fills") else "error"
                audit = {
                    **base,
                    "status": emergency_status,
                    "error": str(exc),
                    "degraded_mode": "deterministic_protective_exits_only",
                    "allowed_symbols": symbols,
                    "screening": screening_audit,
                    "committee": committee,
                    "committee_errors": committee_errors,
                    "agent_workflow": staged_workflow,
                "evidence_ref": staged_workflow.get("evidence_ref", "") if isinstance(staged_workflow, Mapping) else "",
                    "market_data_errors": market_errors,
                    "research_data_errors": research_data_errors,
                    "chair": {"decisions": []},
                    "risk": emergency_risk,
                    "execution": emergency_execution,
                    "control": control,
                }
            except Exception as protective_exc:
                logger.exception("Protective fallback also failed for %s", market)
                audit = {
                    **base,
                    "status": "error",
                    "error": str(exc),
                    "protective_fallback_error": str(protective_exc),
                    "allowed_symbols": symbols,
                    "screening": screening_audit,
                    "committee": committee,
                    "committee_errors": committee_errors,
                    "agent_workflow": staged_workflow,
                "evidence_ref": staged_workflow.get("evidence_ref", "") if isinstance(staged_workflow, Mapping) else "",
                    "market_data_errors": market_errors,
                    "research_data_errors": research_data_errors,
                }
        path = _write_audit(audit, current, market, label)
        return {**audit, "audit_file": str(path)}
