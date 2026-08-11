from __future__ import annotations

import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from src.config import cfg
from src.data import fetcher
from src.llm.registry import resolve_llm
from src.portfolio import account as account_store
from src.runtime_lock import atomic_claim
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
    return _dedupe([*held, *configured], int(config.get("max_universe_size", 10)))


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
) -> Dict[str, Any]:
    current = _now(now)
    market = market.lower().strip()
    config = cfg.autonomous
    base: Dict[str, Any] = {
        "market": market,
        "label": label,
        "generated_at": current.isoformat(timespec="seconds"),
        "mode": "dry_run" if dry_run else "paper",
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
        symbols = _universe(market, account, config)
        snapshots, market_errors = _fetch_snapshots(symbols, int(config.get("market_data_workers", 4)))
        prices = {symbol: _price(snapshot) for symbol, snapshot in snapshots.items()}
        prices = {symbol: price for symbol, price in prices.items() if price > 0}
        minimum_prices = int(config.get("minimum_priced_symbols", 2))
        if len(prices) < minimum_prices:
            audit = {
                **base,
                "status": "blocked",
                "reason": f"有效行情不足 {minimum_prices} 个标的",
                "allowed_symbols": symbols,
                "market_data_errors": market_errors,
            }
            path = _write_audit(audit, current, market, label)
            return {**audit, "audit_file": str(path)}

        compact_snapshots = {
            symbol: {
                "price": prices.get(symbol),
                "realtime": snapshot.get("realtime", {}),
                "indicators": snapshot.get("indicators", {}),
                "history": snapshot.get("history", [])[-10:],
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
            "macro_excerpt": macro_excerpt[:6000],
            "optimizer": dict(optimizer_hint or {}),
            "market_rules": cfg.market_config(market),
            "autonomous_constraints": dict(config),
        }

        roles = [role for role in config.get("committee_roles", list(_ROLE_INSTRUCTIONS)) if role in _ROLE_INSTRUCTIONS]
        committee: List[Dict[str, Any]] = []
        committee_errors: Dict[str, str] = {}
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
                "committee": committee,
                "committee_errors": committee_errors,
                "market_data_errors": market_errors,
            }
            path = _write_audit(audit, current, market, label)
            return {**audit, "audit_file": str(path)}

        try:
            chair = _chair_decision(market, context, committee, config)
            risk = build_orders(
                chair.get("decisions", []),
                account=account,
                prices=prices,
                allowed_symbols=symbols,
                market_config=cfg.market_config(market),
                autonomous_config=config,
                trading_config=cfg.trading,
                now=current,
            )
            should_execute = bool(config.get("auto_execute", True)) and not dry_run
            execution = execute_orders(
                market,
                risk.get("orders", []) if should_execute else [],
                market_config=cfg.market_config(market),
                trading_mode=str(cfg.trading.get("mode", "paper")),
                now=current,
                equity_snapshot=float(risk.get("equity", 0) or 0),
                mark_prices=prices,
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
            audit = {
                **base,
                "status": status,
                "allowed_symbols": symbols,
                "prices": prices,
                "market_data_errors": market_errors,
                "committee": committee,
                "committee_errors": committee_errors,
                "chair": chair,
                "risk": risk,
                "execution": execution,
                "control": control,
            }
        except Exception as exc:
            logger.exception("Autonomous cycle failed for %s", market)
            audit = {
                **base,
                "status": "error",
                "error": str(exc),
                "allowed_symbols": symbols,
                "committee": committee,
                "committee_errors": committee_errors,
                "market_data_errors": market_errors,
            }
        path = _write_audit(audit, current, market, label)
        return {**audit, "audit_file": str(path)}
