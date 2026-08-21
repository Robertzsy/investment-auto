from __future__ import annotations

import math
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Dict, Iterable, List, Mapping, Sequence


@dataclass(frozen=True)
class ProposedOrder:
    symbol: str
    side: str
    shares: int
    reference_price: float
    target_weight: float
    confidence: float
    reason: str
    decision_id: str
    protection_stage: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


def _quantity(holding: Mapping[str, Any]) -> int:
    return int(float(holding.get("shares", holding.get("quantity", 0)) or 0))


def _holding_code(holding: Mapping[str, Any]) -> str:
    return str(holding.get("code", "")).strip().upper()


def _lot_size(market_config: Mapping[str, Any]) -> int:
    configured = market_config.get("trading", {}).get("lot_size", 1)
    try:
        return max(1, int(configured))
    except (TypeError, ValueError):
        # HK board lots require instrument metadata that the current data source
        # does not expose. A conservative round lot prevents odd-lot assumptions.
        return 100


def _round_lot(shares: float, lot_size: int) -> int:
    return max(0, int(math.floor(max(0.0, shares) / lot_size)) * lot_size)


def _today_trade_count(account: Mapping[str, Any], now: datetime) -> int:
    prefix = now.date().isoformat()
    return sum(
        1
        for trade in account.get("tradeHistory", [])
        if str(trade.get("date", trade.get("time", ""))).startswith(prefix)
        and str(trade.get("status", "filled")).lower() == "filled"
    )


def _equity(account: Mapping[str, Any], prices: Mapping[str, float]) -> float:
    holdings_value = 0.0
    for holding in account.get("holdings", []):
        code = _holding_code(holding)
        fallback = float(holding.get("lastPrice", holding.get("costPrice", holding.get("cost", 0))) or 0)
        holdings_value += _quantity(holding) * float(prices.get(code, fallback) or fallback)
    return float(account.get("cash", 0) or 0) + holdings_value


def _decision_error(decision: Mapping[str, Any]) -> str | None:
    symbol = str(decision.get("symbol", "")).strip()
    action = str(decision.get("action", "HOLD")).strip().upper()
    if not symbol:
        return "缺少标的代码"
    if action not in {"BUY", "SELL", "HOLD"}:
        return f"不支持的动作: {action}"
    try:
        target = float(decision.get("target_weight", 0))
        confidence = float(decision.get("confidence", 0))
    except (TypeError, ValueError):
        return "target_weight/confidence 必须是数值"
    if not 0 <= target <= 1:
        return "target_weight 必须在 0~1"
    if not 0 <= confidence <= 1:
        return "confidence 必须在 0~1"
    return None


def _protective_decisions(
    account: Mapping[str, Any],
    prices: Mapping[str, float],
    market_config: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    """Create deterministic exits before considering discretionary AI trades."""
    risk = market_config.get("risk", {})
    hard_stop = float(risk.get("hard_stop_pct", -100)) / 100
    trailing_stop = float(risk.get("trailing_stop_pct", -100)) / 100
    take_profit_1 = float(risk.get("take_profit_1_pct", 10_000)) / 100
    take_profit_2 = float(risk.get("take_profit_2_pct", 10_000)) / 100
    take_profit_1_ratio = float(risk.get("take_profit_1_sell_ratio", 0) or 0)
    take_profit_2_ratio = float(risk.get("take_profit_2_sell_ratio", 0) or 0)
    decisions: List[Dict[str, Any]] = []
    for holding in account.get("holdings", []):
        symbol = _holding_code(holding)
        price = float(prices.get(symbol, 0) or 0)
        cost = float(holding.get("costPrice", holding.get("cost", 0)) or 0)
        if not symbol or price <= 0 or cost <= 0 or _quantity(holding) <= 0:
            continue
        high = max(price, float(holding.get("highPrice", cost) or cost))
        pnl = price / cost - 1
        trailing_drawdown = price / high - 1 if high > 0 else 0
        reason = ""
        forced_shares = _quantity(holding)
        protection_stage = ""
        if pnl <= hard_stop:
            reason = f"硬止损触发：收益率 {pnl:.2%} <= {hard_stop:.2%}"
            protection_stage = "hard_stop"
        elif high > cost and trailing_drawdown <= trailing_stop:
            reason = f"移动止损触发：较高点回撤 {trailing_drawdown:.2%} <= {trailing_stop:.2%}"
            protection_stage = "trailing_stop"
        elif pnl >= take_profit_1 and not bool(holding.get("takeProfit1Done")):
            forced_shares = int(_quantity(holding) * take_profit_1_ratio)
            reason = f"第一档止盈触发：收益率 {pnl:.2%} >= {take_profit_1:.2%}"
            protection_stage = "take_profit_1"
        elif pnl >= take_profit_2 and not bool(holding.get("takeProfit2Done")):
            forced_shares = int(_quantity(holding) * take_profit_2_ratio)
            reason = f"第二档止盈触发：收益率 {pnl:.2%} >= {take_profit_2:.2%}"
            protection_stage = "take_profit_2"
        if reason:
            decisions.append({
                "decision_id": f"protective-{symbol}",
                "symbol": symbol,
                "action": "SELL",
                "target_weight": 0,
                "confidence": 1,
                "reason": reason,
                "forced": True,
                "forced_shares": forced_shares,
                "protection_stage": protection_stage,
            })
    return decisions


def build_orders(
    decisions: Sequence[Mapping[str, Any]],
    *,
    account: Mapping[str, Any],
    prices: Mapping[str, float],
    allowed_symbols: Iterable[str],
    market_config: Mapping[str, Any],
    autonomous_config: Mapping[str, Any],
    trading_config: Mapping[str, Any],
    now: datetime,
) -> Dict[str, Any]:
    """Convert AI target weights into bounded paper orders.

    The AI cannot bypass these checks: invalid decisions are rejected and all
    sizing is recomputed from account equity and hard configuration limits.
    """

    allowed = {str(symbol).strip().upper() for symbol in allowed_symbols}
    holdings = {_holding_code(item): item for item in account.get("holdings", [])}
    equity = _equity(account, prices)
    if equity <= 0:
        return {"orders": [], "rejected": [{"reason": "账户权益为 0"}], "equity": equity}

    risk_config = market_config.get("risk", {})
    min_confidence = float(autonomous_config.get("min_confidence", 0.65))
    max_position_weight = min(
        float(risk_config.get("single_stock_max_pct", 100)) / 100,
        float(autonomous_config.get("max_position_pct", 100)) / 100,
    )
    max_order_value = equity * float(autonomous_config.get("max_order_value_pct", 8)) / 100
    max_cycle_turnover = equity * float(autonomous_config.get("max_cycle_turnover_pct", 15)) / 100
    min_cash_reserve = equity * float(risk_config.get("min_cash_reserve_pct", 0)) / 100
    max_total_position = equity * float(autonomous_config.get("max_total_position_pct", 100)) / 100
    max_orders = int(autonomous_config.get("max_orders_per_cycle", 3))
    remaining_daily = max(0, int(trading_config.get("max_daily_trades", 6)) - _today_trade_count(account, now))
    order_limit = min(max_orders, remaining_daily)
    lot_size = _lot_size(market_config)
    cash_available = max(0.0, float(account.get("cash", 0) or 0) - min_cash_reserve)
    current_holdings_value = max(0.0, equity - float(account.get("cash", 0) or 0))
    portfolio_capacity = max(0.0, max_total_position - current_holdings_value)
    cash_available = min(cash_available, portfolio_capacity)
    high_water = max(float(account.get("highWaterMark", 0) or 0), equity)
    drawdown = equity / high_water - 1 if high_water > 0 else 0.0
    max_drawdown = float(risk_config.get("max_drawdown_pct", -100)) / 100

    circuit_breaker = drawdown <= max_drawdown + 1e-12
    circuit_symbols = [
        _holding_code(item)
        for item in account.get("holdings", [])
        if _quantity(item) > 0 and _holding_code(item)
    ]
    circuit_quantities = {
        _holding_code(item): _quantity(item)
        for item in account.get("holdings", [])
        if _quantity(item) > 0 and _holding_code(item)
    }
    protective = _protective_decisions(account, prices, market_config)
    if circuit_breaker:
        protective = []
        for holding in account.get("holdings", []):
            symbol = _holding_code(holding)
            if not symbol or _quantity(holding) <= 0:
                continue
            protective.append({
                "decision_id": f"drawdown-liquidation-{symbol}",
                "symbol": symbol,
                "action": "SELL",
                "target_weight": 0,
                "confidence": 1,
                "reason": f"账户最大回撤熔断：{drawdown:.2%} <= {max_drawdown:.2%}",
                "forced": True,
                "forced_shares": _quantity(holding),
                "protection_stage": "drawdown_liquidation",
            })
    protected_symbols = {item["symbol"] for item in protective}
    effective_decisions = [
        *protective,
        *(item for item in decisions if str(item.get("symbol", "")).strip().upper() not in protected_symbols),
    ]

    orders: List[ProposedOrder] = []
    rejected: List[Dict[str, Any]] = []
    turnover = 0.0

    for index, decision in enumerate(effective_decisions):
        forced = bool(decision.get("forced", False))
        if len(orders) >= order_limit and not forced:
            rejected.append({"decision": dict(decision), "reason": "已达到本轮或当日交易次数上限"})
            continue
        error = _decision_error(decision)
        if error:
            rejected.append({"decision": dict(decision), "reason": error})
            continue

        symbol = str(decision["symbol"]).strip().upper()
        action = str(decision.get("action", "HOLD")).strip().upper()
        confidence = float(decision.get("confidence", 0))
        reason = str(decision.get("reason", ""))[:500]
        decision_id = str(decision.get("decision_id") or f"decision-{index + 1}")[:128]
        if action == "HOLD":
            continue
        if symbol not in allowed:
            rejected.append({"decision": dict(decision), "reason": "标的不在人工配置的允许池中"})
            continue
        if confidence < min_confidence and not forced:
            rejected.append({"decision": dict(decision), "reason": f"置信度低于 {min_confidence:.2f}"})
            continue
        price = float(prices.get(symbol, 0) or 0)
        if not math.isfinite(price) or price <= 0:
            rejected.append({"decision": dict(decision), "reason": "缺少有效实时价格"})
            continue

        holding = holdings.get(symbol, {})
        current_shares = _quantity(holding)
        current_value = current_shares * price
        requested_target = float(decision.get("target_weight", 0))
        target_weight = min(requested_target, max_position_weight)
        target_value = target_weight * equity
        delta = target_value - current_value
        if action == "BUY" and delta <= 0:
            rejected.append({"decision": dict(decision), "reason": "BUY 的目标仓位不高于当前仓位"})
            continue
        if action == "SELL" and delta >= 0:
            rejected.append({"decision": dict(decision), "reason": "SELL 的目标仓位不低于当前仓位"})
            continue
        if action == "BUY" and circuit_breaker:
            rejected.append({"decision": dict(decision), "reason": "账户达到最大回撤阈值，禁止新增风险"})
            continue

        desired_value = abs(delta) if forced and action == "SELL" else min(
            abs(delta), max_order_value, max(0.0, max_cycle_turnover - turnover)
        )
        if action == "BUY":
            desired_value = min(desired_value, cash_available)
            shares = _round_lot(desired_value / price, lot_size)
        else:
            requested_shares = float(decision.get("forced_shares", 0) or 0) if forced else desired_value / price
            shares = min(current_shares, _round_lot(requested_shares, lot_size))
        if shares <= 0:
            rejected.append({"decision": dict(decision), "reason": "风控和手数取整后订单数量为 0"})
            continue

        value = shares * price
        orders.append(ProposedOrder(
            symbol=symbol,
            side=action,
            shares=shares,
            reference_price=price,
            target_weight=target_weight,
            confidence=confidence,
            reason=reason,
            decision_id=decision_id,
            protection_stage=str(decision.get("protection_stage", ""))[:64],
        ))
        turnover += value
        if action == "BUY":
            cash_available = max(0.0, cash_available - value)

    return {
        "orders": [order.to_dict() for order in orders],
        "rejected": rejected,
        "equity": round(equity, 4),
        "high_water_mark": round(high_water, 4),
        "drawdown": round(drawdown, 8),
        "circuit_breaker": circuit_breaker,
        "circuit_liquidation_symbols": circuit_symbols,
        "circuit_liquidation_quantities": circuit_quantities,
        "protective_decisions": protective,
        "turnover_value": round(turnover, 4),
        "remaining_daily_trades": remaining_daily,
    }
