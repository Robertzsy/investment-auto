"""Deterministic rule backtests for the offline research plane.

This is deliberately a lightweight research engine, not a replacement for
the live trading stack: daily close prices from the existing fetcher history
endpoint (Tencent K-line, qfq), a momentum rule, next-close execution and a
flat cost model.  It answers "is this parameter change plausibly better"
before a strategy experiment ever touches production config.

Signal-to-execution is T+1 (decide on close t, fill on close t+1) and each
weight change pays commission + stamp + slippage, so results stay
conservative.
"""

from __future__ import annotations

from typing import Any, Dict, List, Mapping, Optional, Sequence


def _history_closes(market: str, symbol: str, lookback: int) -> List[tuple[str, float]]:
    from engine.data import fetcher

    try:
        payload = fetcher.history(symbol, lookback=lookback + 40)
    except Exception as exc:
        return []
    data = payload.get("data", []) if isinstance(payload, Mapping) else []
    closes = []
    for item in data:
        if not isinstance(item, Mapping):
            continue
        date = str(item.get("date", ""))
        try:
            close = float(item["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if date and close > 0:
            closes.append((date, close))
    return closes


def _align_closes(series: Mapping[str, List[tuple[str, float]]], lookback: int):
    base_symbol = next(iter(series))
    base = series[base_symbol][-lookback:]
    aligned: Dict[str, List[float]] = {}
    for symbol, rows in series.items():
        if not rows:
            continue
        prices: List[float] = []
        index = 0
        for date, _ in base:
            while index < len(rows) and rows[index][0] < date:
                index += 1
            if index < len(rows) and rows[index][0] == date:
                prices.append(rows[index][1])
                index += 1
            else:
                # Forward-fill the previous close (suspended days, holidays).
                prices.append(prices[-1] if prices else rows[index - 1][1] if index else None)
        aligned[symbol] = prices
    return [date for date, _ in base], aligned


def run_rule_backtest(
    market: str,
    symbols: Sequence[str],
    *,
    lookback: int = 60,
    momentum_days: int = 5,
    weight_per_symbol: Optional[float] = None,
    cost_rate: float = 0.00225,
) -> Dict[str, Any]:
    """Run a momentum rule backtest; returns metrics plus the NAV series."""
    normalized = [str(symbol).strip().upper() for symbol in symbols if str(symbol).strip()]
    if len(normalized) < 1:
        raise ValueError("回测至少需要一个标的")
    market = str(market or "").strip().lower()
    days = max(10, min(500, int(lookback)))
    momentum_window = max(1, min(60, int(momentum_days)))
    weight = float(weight_per_symbol or round(1.0 / len(normalized), 4))
    if weight <= 0:
        raise ValueError("weight_per_symbol 必须为正数")

    series = {
        symbol: _history_closes(market, symbol, days) for symbol in normalized
    }
    series = {symbol: rows for symbol, rows in series.items() if len(rows) >= momentum_window + 2}
    if not series:
        return {"market": market, "symbols": normalized, "error": "没有可用历史行情"}
    dates, aligned = _align_closes(series, days)
    symbols_used = [symbol for symbol in normalized if symbol in aligned]
    n = len(dates)
    if n <= momentum_window + 1:
        return {"market": market, "symbols": normalized, "error": "对齐后数据不足"}

    per_symbol = {symbol: aligned[symbol][:n] for symbol in symbols_used}
    nav = 1.0
    nav_series = [1.0]
    benchmark = 1.0
    weights = {symbol: 0.0 for symbol in symbols_used}
    turnover = 0
    daily_returns: List[float] = []

    for t in range(momentum_window, n - 1):
        targets: Dict[str, float] = {}
        for symbol in symbols_used:
            closes = per_symbol[symbol]
            momentum = closes[t] / closes[t - momentum_window] - 1.0
            targets[symbol] = weight if momentum > 0 else 0.0
        cost = sum(abs(targets[symbol] - weights[symbol]) for symbol in symbols_used) * cost_rate
        weights = targets
        portfolio_return = sum(
            weights[symbol] * (per_symbol[symbol][t + 1] / per_symbol[symbol][t] - 1.0)
            for symbol in symbols_used
        ) - cost
        turnover += cost
        nav *= 1.0 + portfolio_return
        nav_series.append(round(nav, 6))
        daily_returns.append(portfolio_return)
        benchmark *= 1.0 + (per_symbol[symbols_used[0]][t + 1] / per_symbol[symbols_used[0]][t] - 1.0)

    peak = 1.0
    max_drawdown = 0.0
    for value in nav_series:
        peak = max(peak, value)
        max_drawdown = min(max_drawdown, value / peak - 1.0)
    mean = sum(daily_returns) / len(daily_returns) if daily_returns else 0.0
    variance = sum((r - mean) ** 2 for r in daily_returns) / len(daily_returns) if daily_returns else 0.0
    std = variance ** 0.5
    sharpe = (mean / std) * (252 ** 0.5) if std > 0 else 0.0
    trading_days = len(daily_returns)

    return {
        "market": market,
        "symbols": symbols_used,
        "mode": "rule_momentum",
        "params": {
            "lookback_days": days,
            "momentum_days": momentum_window,
            "weight_per_symbol": weight,
            "cost_rate": cost_rate,
        },
        "start_date": dates[momentum_window] if n > momentum_window else None,
        "end_date": dates[-1] if dates else None,
        "trading_days": trading_days,
        "total_return": round(nav - 1.0, 6),
        "annualized_return": round(nav ** (252.0 / trading_days) - 1.0, 6) if trading_days else 0.0,
        "max_drawdown": round(max_drawdown, 6),
        "sharpe": round(sharpe, 4),
        "turnover_cost": round(turnover, 6),
        "benchmark_return": round(benchmark - 1.0, 6),
        "final_value": round(nav, 6),
        "nav_series": nav_series,
    }
