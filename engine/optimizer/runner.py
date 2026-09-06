from __future__ import annotations

import json
import math
import random
import re
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence
from zoneinfo import ZoneInfo

import numpy as np

from engine.config import cfg
from engine.data import fetcher
from engine.optimizer.engine import (
    _cov_matrix,
    _mean,
    _metrics,
    max_sharpe,
    max_sharpe_cost_aware,
    rebalance_cost,
    risk_parity,
    stress_test,
)
from engine.portfolio import account
from engine.runtime_lock import atomic_claim

ROOT = Path(__file__).resolve().parents[2]
from engine.paths import runtime_dir
OUTPUT_DIR = runtime_dir() / "optimizer"
MIN_OBSERVATIONS = 30
MAX_SYMBOLS = 12
_SUPPORTED_MARKETS = {"cn", "hk", "us", "etf"}

_DEFAULT_SYMBOLS = {
    "cn": ["600519", "000858", "601318", "600030", "510300", "600036", "000333"],
    "hk": ["00700", "09988", "03690", "01299", "00941", "00005", "02800"],
    "us": ["AAPL", "MSFT", "NVDA", "AMZN", "GOOGL", "META", "BRK.B", "SPY", "QQQ"],
    "etf": ["510300", "510050", "159915", "512100"],
}


def _symbol_key(value: Any) -> str:
    original = str(value or "").strip()
    if re.fullmatch(r"(?i:(?:sh|sz|bj)\d{6})", original):
        return original[-6:]
    if re.fullmatch(r"(?i:hk\d{5})", original):
        return original[-5:]
    # Only strip the optional US transport prefix when it is explicitly lower-case;
    # legitimate tickers such as USB, SHOP and HKD must remain unchanged.
    if re.fullmatch(r"us[A-Za-z][A-Za-z0-9.\-]{0,9}", original):
        return original[2:].upper()
    return original.upper()


def _validate_symbol(symbol: str) -> None:
    text = str(symbol).strip()
    if not text or len(text) > 32 or not re.fullmatch(r"[A-Za-z0-9.\-]+", text):
        raise ValueError(f"无效标的代码: {symbol!r}")


def _dedupe(values: Iterable[Any]) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        text = str(value or "").strip()
        if not text:
            continue
        _validate_symbol(text)
        key = _symbol_key(text)
        if key in seen:
            continue
        seen.add(key)
        result.append(text)
    if len(result) > MAX_SYMBOLS:
        raise ValueError(f"单次优化最多支持 {MAX_SYMBOLS} 个标的")
    return result


def _holding_quantity(holding: Dict[str, Any]) -> float:
    return float(holding.get("shares", holding.get("quantity", 0)) or 0)


def _holding_cost(holding: Dict[str, Any]) -> float:
    return float(holding.get("costPrice", holding.get("cost", holding.get("price", 0))) or 0)


def default_symbols(market: str) -> List[str]:
    configured = cfg.optimizer.get("default_symbols", {}).get(market)
    if configured:
        return _dedupe(configured)
    return list(_DEFAULT_SYMBOLS.get(market, _DEFAULT_SYMBOLS["cn"]))


def resolve_universe(market: str, symbols: Optional[Sequence[str]] = None) -> List[str]:
    market = market.lower().strip()
    if market not in _SUPPORTED_MARKETS:
        raise ValueError(f"不支持的市场: {market}")
    if symbols:
        universe = _dedupe(symbols)
    else:
        holdings = account.account(market).get("holdings", [])
        held_symbols = [holding.get("code") for holding in holdings]
        screened_symbols: List[str] = []
        screening_config = cfg.screening
        if screening_config.get("enabled", True) and screening_config.get("use_for_optimizer", True):
            try:
                from engine.screening import latest_screening

                latest = latest_screening(
                    market,
                    max_age_minutes=int(screening_config.get("optimizer_max_age_minutes", 1440)),
                )
                if latest:
                    screened_symbols = list(latest.get("selected_symbols", []))
            except Exception:
                screened_symbols = []
        candidates = [*held_symbols, *(screened_symbols or default_symbols(market))]
        universe = _dedupe(candidates[:MAX_SYMBOLS])
    if len(universe) < 2:
        raise ValueError("组合优化至少需要两个有效标的；请通过 --symbols 或 optimizer.default_symbols 提供标的")
    return universe


def _check_cancel(cancel_event: Optional[threading.Event], deadline: float) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise InterruptedError("优化器已取消")
    if time.monotonic() >= deadline:
        raise TimeoutError("优化器超过总运行时限")


def _load_price_series(
    symbol: str,
    lookback_days: int,
    *,
    cancel_event: Optional[threading.Event],
    deadline: float,
) -> Dict[str, float]:
    _check_cancel(cancel_event, deadline)
    remaining = max(1, int(deadline - time.monotonic()))
    payload = fetcher.history(symbol, lookback=lookback_days, timeout=min(30, remaining))
    if not isinstance(payload, dict):
        raise ValueError(f"{symbol} 历史行情返回格式错误")
    if payload.get("error"):
        raise ValueError(str(payload["error"]))
    rows = payload.get("data") or []
    series: Dict[str, float] = {}
    for row in rows:
        try:
            date = str(row["date"])
            close = float(row["close"])
        except (KeyError, TypeError, ValueError):
            continue
        if close > 0:
            series[date] = close
    if lookback_days > 0 and len(series) > lookback_days + 1:
        keys = sorted(series)[-(lookback_days + 1):]
        series = {key: series[key] for key in keys}
    return series


def _aligned_returns(series_by_symbol: Dict[str, Dict[str, float]]) -> tuple[List[str], List[List[float]]]:
    common_dates: Optional[set[str]] = None
    for series in series_by_symbol.values():
        dates = set(series)
        common_dates = dates if common_dates is None else common_dates & dates
    dates = sorted(common_dates or set())
    if len(dates) < MIN_OBSERVATIONS + 1:
        raise ValueError(f"共同历史数据不足：仅 {len(dates)} 个交易日，至少需要 {MIN_OBSERVATIONS + 1} 个")

    returns: List[List[float]] = []
    for series in series_by_symbol.values():
        prices = [series[date] for date in dates]
        returns.append([prices[index] / prices[index - 1] - 1 for index in range(1, len(prices))])
    return dates, returns


def _current_asset_weights(symbols: Sequence[str], last_prices: Dict[str, float], market: str) -> List[float]:
    acct = account.account(market)
    values_by_key: Dict[str, float] = {}
    all_holdings_value = 0.0
    for holding in acct.get("holdings", []):
        key = _symbol_key(holding.get("code"))
        quantity = _holding_quantity(holding)
        fallback = _holding_cost(holding)
        price = last_prices.get(key, fallback)
        value = max(0.0, quantity * price)
        values_by_key[key] = values_by_key.get(key, 0.0) + value
        all_holdings_value += value
    cash = float(acct.get("cash", 0) or 0)
    total_assets = cash + all_holdings_value
    if total_assets <= 0:
        total_assets = float(acct.get("totalCapital", 0) or 0)
    if total_assets <= 0:
        return [0.0] * len(symbols)
    return [values_by_key.get(_symbol_key(symbol), 0.0) / total_assets for symbol in symbols]


def _black_litterman_posterior(
    mus: List[float],
    cov: List[List[float]],
    prior_weights: List[float],
    *,
    tau: float,
    confidence: float,
    risk_aversion: float,
) -> List[float]:
    sigma = np.asarray(cov, dtype=float)
    n = len(mus)
    weights = np.asarray(prior_weights, dtype=float)
    if weights.sum() <= 0:
        weights = np.full(n, 1.0 / n)
    else:
        weights = weights / weights.sum()
    pi = risk_aversion * sigma.dot(weights)
    p = np.eye(n)
    q = np.asarray(mus, dtype=float)
    tau_sigma = max(tau, 1e-6) * sigma
    view_variance = np.maximum(np.diag(tau_sigma) * (1 - confidence) / max(confidence, 1e-6), 1e-10)
    omega = np.diag(view_variance)
    adjustment = tau_sigma.dot(p.T).dot(np.linalg.pinv(p.dot(tau_sigma).dot(p.T) + omega)).dot(q - p.dot(pi))
    return (pi + adjustment).tolist()


def _net_evaluation(
    weights: List[float],
    metrics: Dict[str, Any],
    current_asset_weights: List[float],
    target_exposure: float,
    rf: float,
    buy_rate: float,
    sell_rate: float,
) -> Dict[str, Any]:
    target_asset_weights = [weight * target_exposure for weight in weights]
    cost = rebalance_cost(current_asset_weights, target_asset_weights, buy_rate, sell_rate)
    gross_return = target_exposure * float(metrics["annual_return"]) + (1 - target_exposure) * rf
    volatility = target_exposure * float(metrics["annual_volatility"])
    net_return = gross_return - cost["cost_ratio"]
    net_sharpe = (net_return - rf) / volatility if volatility > 1e-12 else 0.0
    return {
        "gross_annual_return": round(gross_return, 10),
        "net_annual_return": round(net_return, 10),
        "annual_volatility": round(volatility, 10),
        "net_sharpe": round(net_sharpe, 10),
        "rebalance_cost": {key: round(float(value), 10) for key, value in cost.items()},
    }


def _scheme(
    symbols: Sequence[str],
    weights: List[float],
    metrics: Dict[str, Any],
    returns: List[List[float]],
    *,
    current_asset_weights: List[float],
    target_exposure: float,
    rf: float,
    buy_rate: float,
    sell_rate: float,
    **extra: Any,
) -> Dict[str, Any]:
    portfolio_weights = {symbol: round(weight * target_exposure, 8) for symbol, weight in zip(symbols, weights)}
    portfolio_weights["CASH"] = round(1 - target_exposure, 8)
    scaled_returns = [[value * target_exposure for value in series] for series in returns]
    result: Dict[str, Any] = {
        "weights": {symbol: round(weight, 8) for symbol, weight in zip(symbols, weights)},
        "portfolio_weights": portfolio_weights,
        "metrics": {key: round(float(value), 10) for key, value in metrics.items()},
        "net_metrics": _net_evaluation(weights, metrics, current_asset_weights, target_exposure, rf, buy_rate, sell_rate),
        "stress": {key: round(float(value), 10) for key, value in stress_test(weights, scaled_returns).items()},
    }
    result.update(extra)
    return result


def _run_optimizer_unlocked(
    market: str = "cn",
    symbols: Optional[Sequence[str]] = None,
    *,
    lookback_days: Optional[int] = None,
    samples: Optional[int] = None,
    output_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    cancel_event: Optional[threading.Event] = None,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    if not cfg.optimizer.get("enabled", True):
        raise ValueError("优化器已在配置中禁用")

    market = market.lower().strip()
    universe = resolve_universe(market, symbols)
    lookback = int(lookback_days or cfg.optimizer.get("lookback_days", 120))
    sample_count = int(samples or cfg.optimizer.get("samples", 3000))
    total_timeout = int(timeout_seconds or cfg.optimizer.get("timeout_seconds", 180))
    if lookback < MIN_OBSERVATIONS:
        raise ValueError(f"lookback_days 不能小于 {MIN_OBSERVATIONS}")
    if sample_count < 100:
        raise ValueError("optimizer.samples 不能小于 100")
    deadline = time.monotonic() + max(10, total_timeout)

    series_by_symbol: Dict[str, Dict[str, float]] = {}
    errors: Dict[str, str] = {}
    for symbol in universe:
        try:
            series = _load_price_series(symbol, lookback, cancel_event=cancel_event, deadline=deadline)
            if len(series) < MIN_OBSERVATIONS + 1:
                raise ValueError(f"仅 {len(series)} 个有效交易日")
            series_by_symbol[symbol] = series
        except InterruptedError:
            raise
        except Exception as exc:
            errors[symbol] = str(exc)

    _check_cancel(cancel_event, deadline)
    if len(series_by_symbol) < 2:
        detail = "；".join(f"{symbol}: {error}" for symbol, error in errors.items())
        raise ValueError(f"可用标的不足两个，无法优化。{detail}")

    symbols_used = list(series_by_symbol)
    dates, returns = _aligned_returns(series_by_symbol)
    mus = [_mean(values) for values in returns]
    cov = _cov_matrix(returns)
    rf = float(cfg.optimizer.get("risk_free_rate", 0.02))

    market_cfg = cfg.market_config(market)
    configured_cap = float(market_cfg.get("optimizer", {}).get("risky_sleeve_max_weight", 1.0))
    if not 0 < configured_cap <= 1:
        raise ValueError(f"单标的上限无效: {configured_cap}")
    requested_exposure = float(market_cfg.get("risk", {}).get("target_total_exposure", 100)) / 100
    requested_exposure = min(1.0, max(0.0, requested_exposure))
    target_exposure = min(requested_exposure, configured_cap * len(symbols_used))
    if target_exposure <= 0:
        raise ValueError("目标风险敞口为 0，无法优化")
    relative_cap = min(1.0, configured_cap / target_exposure)

    last_prices = {_symbol_key(symbol): series_by_symbol[symbol][dates[-1]] for symbol in symbols_used}
    current_asset_weights = _current_asset_weights(symbols_used, last_prices, market)

    trading_cfg = market_cfg.get("trading", {})
    commission = float(trading_cfg.get("commission_rate", 0.0) or 0.0)
    slippage = float(trading_cfg.get("slippage", 0.0) or 0.0)
    stamp_tax = float(trading_cfg.get("stamp_tax", 0.0) or 0.0)
    stamp_side = str(trading_cfg.get("stamp_tax_side", "")).lower()
    buy_rate = commission + slippage + (stamp_tax if stamp_side in {"buy", "both"} else 0.0)
    sell_rate = commission + slippage + (stamp_tax if stamp_side in {"sell", "both"} else 0.0)

    state = random.getstate()
    random.seed(20260811)
    try:
        mv_w, mv_m = max_sharpe(mus, cov, rf, relative_cap, sample_count)
        rp_w, risk_contributions = risk_parity(cov, relative_cap)
        rp_m = _metrics(rp_w, mus, cov, rf)

        posterior_mus = _black_litterman_posterior(
            mus,
            cov,
            current_asset_weights,
            tau=float(cfg.optimizer.get("black_litterman_tau", 0.05)),
            confidence=float(cfg.optimizer.get("view_confidence", 0.5)),
            risk_aversion=float(cfg.optimizer.get("risk_aversion", 2.5)),
        )
        bl_w, _ = max_sharpe(posterior_mus, cov, rf, relative_cap, sample_count)
        bl_m = _metrics(bl_w, mus, cov, rf)

        common = {
            "current_asset_weights": current_asset_weights,
            "target_exposure": target_exposure,
            "rf": rf,
            "buy_rate": buy_rate,
            "sell_rate": sell_rate,
        }
        schemes: Dict[str, Any] = {
            "mean_variance": _scheme(symbols_used, mv_w, mv_m, returns, **common),
            "black_litterman": _scheme(
                symbols_used,
                bl_w,
                bl_m,
                returns,
                posterior_daily_returns={symbol: round(value, 10) for symbol, value in zip(symbols_used, posterior_mus)},
                tau=float(cfg.optimizer.get("black_litterman_tau", 0.05)),
                view_confidence=float(cfg.optimizer.get("view_confidence", 0.5)),
                **common,
            ),
            "risk_parity": _scheme(
                symbols_used,
                rp_w,
                rp_m,
                returns,
                risk_contributions={symbol: round(value, 8) for symbol, value in zip(symbols_used, risk_contributions)},
                **common,
            ),
        }

        if cfg.optimizer.get("cost_aware", True):
            ca_w, ca_m, _ = max_sharpe_cost_aware(
                mus,
                cov,
                rf,
                relative_cap,
                current_asset_weights,
                buy_rate,
                sell_rate,
                samples=sample_count,
                target_exposure=target_exposure,
            )
            schemes["cost_adjusted"] = _scheme(symbols_used, ca_w, ca_m, returns, **common)
    finally:
        random.setstate(state)

    _check_cancel(cancel_event, deadline)
    best_name = max(schemes, key=lambda name: schemes[name]["net_metrics"]["net_sharpe"])
    current = now or datetime.now(ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai")))
    destination = output_dir or OUTPUT_DIR
    destination.mkdir(parents=True, exist_ok=True)
    filename = current.strftime("%Y%m%d-%H%M%S-%f") + f"-{uuid.uuid4().hex[:8]}-{market}.json"
    path = destination / filename

    result: Dict[str, Any] = {
        "generated_at": current.isoformat(timespec="seconds"),
        "market": market,
        "symbols": symbols_used,
        "requested_symbols": universe,
        "dropped_symbols": errors,
        "observation_start": dates[0],
        "observation_end": dates[-1],
        "observations": len(returns[0]),
        "lookback_days": lookback,
        "constraints": {
            "single_asset_max_weight": round(configured_cap, 8),
            "requested_exposure": round(requested_exposure, 8),
            "effective_exposure": round(target_exposure, 8),
            "cash_weight": round(1 - target_exposure, 8),
        },
        "current_asset_weights": {symbol: round(weight, 8) for symbol, weight in zip(symbols_used, current_asset_weights)},
        "recommended_scheme": best_name,
        "output_file": str(path),
        **schemes,
    }
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)
    return result


def run_optimizer(
    market: str = "cn",
    symbols: Optional[Sequence[str]] = None,
    *,
    lookback_days: Optional[int] = None,
    samples: Optional[int] = None,
    output_dir: Optional[Path] = None,
    now: Optional[datetime] = None,
    cancel_event: Optional[threading.Event] = None,
    timeout_seconds: Optional[int] = None,
) -> Dict[str, Any]:
    destination = output_dir or OUTPUT_DIR
    lock_path = destination / f".{market.lower().strip()}-optimizer.lock"
    stale = int(timeout_seconds or cfg.optimizer.get("timeout_seconds", 180)) + 120
    with atomic_claim(lock_path, stale_seconds=stale) as claimed:
        if not claimed:
            raise RuntimeError(f"{market.upper()} 优化器已有任务正在运行")
        return _run_optimizer_unlocked(
            market=market,
            symbols=symbols,
            lookback_days=lookback_days,
            samples=samples,
            output_dir=output_dir,
            now=now,
            cancel_event=cancel_event,
            timeout_seconds=timeout_seconds,
        )


def parse_symbols(value: Optional[str]) -> Optional[List[str]]:
    if value is None:
        return None
    symbols = [item.strip() for item in value.replace("，", ",").split(",") if item.strip()]
    return symbols or None


def compact_result(result: Dict[str, Any]) -> Dict[str, Any]:
    schemes = {}
    for name in ("mean_variance", "black_litterman", "risk_parity", "cost_adjusted"):
        if name not in result:
            continue
        item = result[name]
        schemes[name] = {
            "weights": item.get("weights", {}),
            "portfolio_weights": item.get("portfolio_weights", {}),
            "metrics": item.get("metrics", {}),
            "net_metrics": item.get("net_metrics", {}),
            "stress": item.get("stress", {}),
        }
    return {
        "market": result.get("market"),
        "symbols": result.get("symbols"),
        "observations": result.get("observations"),
        "constraints": result.get("constraints", {}),
        "recommended_scheme": result.get("recommended_scheme"),
        "schemes": schemes,
        "dropped_symbols": result.get("dropped_symbols", {}),
        "output_file": result.get("output_file"),
    }
