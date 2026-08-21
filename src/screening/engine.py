from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, Mapping, Optional, Sequence

from src.data import fetcher
from src.screening.storage import (
    MongoScreeningStore,
    get_screening_store,
    mark_screening_store_failed,
)

ROOT = Path(__file__).resolve().parents[2]
from src.paths import runtime_dir
SCREENING_DIR = runtime_dir() / "screener"
logger = logging.getLogger("investment-auto.screening")

SnapshotLoader = Callable[[Sequence[str], int], tuple[Dict[str, Any], Dict[str, str]]]


@dataclass
class ScreeningOutcome:
    symbols: List[str]
    snapshots: Dict[str, Any]
    market_data_errors: Dict[str, str]
    audit: Dict[str, Any]


def _number(value: Any, default: float = 0.0) -> float:
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if math.isfinite(result) else default


def _symbol(value: Any) -> str:
    original = str(value or "").strip()
    lower = original.lower()
    if re.fullmatch(r"(?:sh|sz|bj)\d{6}", lower):
        original = original[-6:]
    elif re.fullmatch(r"hk\d{5}", lower):
        original = original[-5:]
    elif re.fullmatch(r"us[A-Za-z][A-Za-z0-9.\-]{0,15}", original):
        original = original[2:]
    normalized = original.upper()
    return normalized if re.fullmatch(r"[A-Z0-9.\-]{1,32}", normalized) else ""


def _dedupe(values: Iterable[Any], limit: Optional[int] = None) -> List[str]:
    result: List[str] = []
    seen = set()
    for value in values:
        normalized = _symbol(value)
        if not normalized or normalized in seen:
            continue
        seen.add(normalized)
        result.append(normalized)
        if limit is not None and len(result) >= limit:
            break
    return result


def _market_setting(settings: Mapping[str, Any], key: str, market: str, default: float) -> float:
    value = settings.get(key, default)
    if isinstance(value, Mapping):
        value = value.get(market, default)
    return _number(value, default)


def _write_json(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    return parsed


def _discovery_cache(
    market: str,
    now: datetime,
    refresh_minutes: int,
    *,
    allow_stale: bool = False,
) -> Optional[Dict[str, Any]]:
    path = SCREENING_DIR / f"discovery-{market}.json"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
        generated = _parse_time(payload.get("generated_at"))
        if generated is None:
            return None
        if generated.tzinfo is None and now.tzinfo is not None:
            generated = generated.replace(tzinfo=now.tzinfo)
        if not allow_stale and now - generated > timedelta(minutes=max(1, refresh_minutes)):
            return None
        if not isinstance(payload.get("data"), list):
            return None
        payload["cached"] = True
        if allow_stale:
            payload["stale"] = True
        return payload
    except (OSError, json.JSONDecodeError, TypeError):
        return None


def _discover(
    market: str,
    settings: Mapping[str, Any],
    now: datetime,
    store: Optional[MongoScreeningStore] = None,
) -> Dict[str, Any]:
    requested_limit = int(settings.get("discovery_limit", 0))
    limit = max(10, min(50_000, requested_limit)) if requested_limit > 0 else 0
    refresh_minutes = max(1, int(settings.get("refresh_minutes", 30)))
    if store is not None:
        try:
            cached = store.read_discovery(
                market,
                now=now,
                refresh_minutes=refresh_minutes,
                limit=limit,
            )
            if cached is not None:
                return cached
        except Exception as exc:
            mark_screening_store_failed(settings, store, exc)
            logger.warning("MongoDB discovery read failed; trying JSON/provider: %s", exc)
            store = None
    cached = _discovery_cache(market, now, refresh_minutes)
    if cached is not None:
        cached["cache_backend"] = "json"
        if store is not None:
            try:
                store.write_discovery(market, cached)
                cached["cache_backend"] = "mongodb+json"
            except Exception as exc:
                mark_screening_store_failed(settings, store, exc)
                logger.warning("MongoDB discovery hydration failed; using JSON: %s", exc)
                store = None
        return cached
    try:
        payload = fetcher.market_list(
            market,
            limit=limit,
            timeout=max(15, int(settings.get("discovery_timeout_seconds", 50))),
        )
    except Exception as provider_error:
        stale = _discovery_cache(market, now, refresh_minutes, allow_stale=True)
        if stale is not None:
            stale["cache_backend"] = "json-stale"
            stale["provider_error"] = str(provider_error)[:1000]
            logger.warning("Full-market provider failed; using stale JSON universe: %s", provider_error)
            return stale
        if store is not None:
            try:
                stale = store.read_discovery(
                    market,
                    now=now,
                    refresh_minutes=60 * 24 * 365 * 10,
                    limit=limit,
                )
                if stale is not None:
                    stale["cache_backend"] = "mongodb-stale"
                    stale["stale"] = True
                    stale["provider_error"] = str(provider_error)[:1000]
                    logger.warning("Full-market provider failed; using stale MongoDB universe: %s", provider_error)
                    return stale
            except Exception as stale_error:
                logger.warning("Stale MongoDB universe also failed: %s", stale_error)
        raise
    result = {
        "generated_at": now.isoformat(timespec="seconds"),
        "market": market,
        "source": payload.get("source", "market-data-provider"),
        "cached": False,
        "cache_backend": "provider",
        "scope": payload.get("scope", "bounded" if limit else "full-market"),
        "total_count": int(payload.get("total_count", len(payload.get("data", []))) or 0),
        "data": payload.get("data", [])[:limit] if limit else payload.get("data", []),
    }
    _write_json(SCREENING_DIR / f"discovery-{market}.json", result)
    if store is not None:
        try:
            store.write_discovery(market, result)
            result["cache_backend"] = "mongodb+json"
        except Exception as exc:
            mark_screening_store_failed(settings, store, exc)
            logger.warning("MongoDB discovery write failed; JSON copy is intact: %s", exc)
    return result


def _filter_candidates(
    rows: Sequence[Mapping[str, Any]],
    market: str,
    settings: Mapping[str, Any],
) -> tuple[List[Dict[str, Any]], Dict[str, int]]:
    min_price = _market_setting(settings, "min_price", market, 0)
    min_amount = _market_setting(settings, "min_amount", market, 0)
    min_market_cap = _market_setting(settings, "min_market_cap", market, 0)
    max_pe = _number(settings.get("max_pe", 80), 80)
    max_pb = _number(settings.get("max_pb", 20), 20)
    max_abs_change = _number(settings.get("max_abs_change_pct", 15), 15)
    patterns = [str(value).upper() for value in settings.get("exclude_name_patterns", [])]
    market_patterns = settings.get("exclude_patterns_by_market", {})
    if isinstance(market_patterns, Mapping):
        patterns.extend(str(value).upper() for value in market_patterns.get(market, []))

    accepted: List[Dict[str, Any]] = []
    rejected: Dict[str, int] = {}

    def excluded_name(name: str) -> bool:
        for pattern in patterns:
            if not pattern:
                continue
            if pattern in {"ST", "*ST"}:
                if market == "cn" and re.search(r"^(?:\*?ST)", name, flags=re.IGNORECASE):
                    return True
            elif re.fullmatch(r"[A-Z ]+", pattern):
                if re.search(rf"\b{re.escape(pattern)}S?\b", name, flags=re.IGNORECASE):
                    return True
            elif pattern in name:
                return True
        return False

    def reject(reason: str) -> None:
        rejected[reason] = rejected.get(reason, 0) + 1

    for original in rows:
        row = dict(original)
        symbol = _symbol(row.get("symbol", row.get("code")))
        name = str(row.get("name", "")).strip()
        upper_name = name.upper()
        price = _number(row.get("price"))
        amount = _number(row.get("amount"))
        market_cap = _number(row.get("market_cap"))
        pe = _number(row.get("pe"))
        pb = _number(row.get("pb"))
        change_pct = _number(row.get("change_pct"))
        if not symbol:
            reject("invalid_symbol")
        elif market == "us" and not re.fullmatch(r"[A-Z]{1,6}(?:\.[A-Z])?", symbol):
            reject("non_common_symbol")
        elif excluded_name(upper_name):
            reject("excluded_name")
        elif price < min_price:
            reject("price")
        elif amount < min_amount:
            reject("liquidity")
        elif min_market_cap > 0 and market_cap < min_market_cap:
            reject("market_cap")
        elif max_pe > 0 and pe > max_pe:
            reject("pe")
        elif max_pb > 0 and pb > max_pb:
            reject("pb")
        elif max_abs_change > 0 and abs(change_pct) > max_abs_change:
            reject("daily_change")
        else:
            row.update({
                "symbol": symbol,
                "name": name,
                "price": price,
                "amount": amount,
                "market_cap": market_cap,
                "pe": pe,
                "pb": pb,
                "change_pct": change_pct,
            })
            accepted.append(row)
    return accepted, rejected


def _cheap_score(row: Mapping[str, Any]) -> float:
    amount = max(0.0, _number(row.get("amount")))
    market_cap = max(0.0, _number(row.get("market_cap")))
    change = _number(row.get("change_pct"))
    pe = _number(row.get("pe"))
    valuation = 0.5 if pe <= 0 else max(0.0, 1.0 - max(0.0, pe - 15) / 100)
    momentum = max(0.0, 1.0 - abs(change - 1.5) / 15)
    return math.log1p(amount) + 0.12 * math.log1p(market_cap) + valuation + momentum


def _clamp(value: float, lower: float = 0.0, upper: float = 100.0) -> float:
    return max(lower, min(upper, value))


def _nested(mapping: Mapping[str, Any], *keys: str) -> Any:
    value: Any = mapping
    for key in keys:
        if not isinstance(value, Mapping):
            return None
        value = value.get(key)
    return value


def _percentile(values: Sequence[float], value: float) -> float:
    clean = sorted(item for item in values if math.isfinite(item))
    if len(clean) <= 1:
        return 50.0
    below = sum(1 for item in clean if item < value)
    equal = sum(1 for item in clean if item == value)
    return _clamp(100.0 * (below + equal / 2) / len(clean))


def _valuation_score(pe: float, pb: float) -> float:
    if pe <= 0:
        pe_score = 50.0
    elif pe < 5:
        pe_score = 55.0
    elif pe <= 20:
        pe_score = 90.0
    elif pe <= 35:
        pe_score = 72.0
    elif pe <= 60:
        pe_score = 45.0
    else:
        pe_score = 20.0
    if pb <= 0:
        return pe_score
    if pb <= 1.5:
        pb_score = 90.0
    elif pb <= 4:
        pb_score = 72.0
    elif pb <= 8:
        pb_score = 45.0
    else:
        pb_score = 20.0
    return (pe_score + pb_score) / 2


def _factor_rows(
    candidates: Sequence[Mapping[str, Any]],
    snapshots: Mapping[str, Any],
    settings: Mapping[str, Any],
) -> List[Dict[str, Any]]:
    amounts = [max(0.0, _number(row.get("amount"))) for row in candidates]
    weights = {
        "momentum": 0.28,
        "trend": 0.22,
        "liquidity": 0.20,
        "valuation": 0.12,
        "volume": 0.10,
        "low_volatility": 0.08,
        **(dict(settings.get("weights", {})) if isinstance(settings.get("weights"), Mapping) else {}),
    }
    weights = {key: max(0.0, _number(value)) for key, value in weights.items()}
    weight_sum = sum(weights.values()) or 1.0
    results: List[Dict[str, Any]] = []
    for row in candidates:
        symbol = _symbol(row.get("symbol"))
        snapshot = snapshots.get(symbol)
        if not isinstance(snapshot, Mapping):
            continue
        realtime = snapshot.get("realtime", {})
        indicators = snapshot.get("indicators", {})
        if not isinstance(realtime, Mapping) or not isinstance(indicators, Mapping) or indicators.get("error"):
            continue
        price = _number(realtime.get("price", row.get("price")))
        if price <= 0:
            continue
        d5 = _number(_nested(indicators, "change", "d5"))
        d20 = _number(_nested(indicators, "change", "d20"))
        momentum = (_clamp(50 + d5 * 5) + _clamp(50 + d20 * 2.5)) / 2

        ma5 = _number(_nested(indicators, "mas", "ma5"))
        ma10 = _number(_nested(indicators, "mas", "ma10"))
        ma20 = _number(_nested(indicators, "mas", "ma20"))
        ma60 = _number(_nested(indicators, "mas", "ma60"))
        macd_bar = _number(_nested(indicators, "macd", "bar"))
        rsi14 = _number(_nested(indicators, "rsi", "rsi14"), 50)
        trend = 20.0
        trend += 25 if ma20 > 0 and price > ma20 else 0
        trend += 25 if ma5 > ma10 > ma20 > 0 else 0
        trend += 15 if ma60 > 0 and price > ma60 else 0
        trend += 15 if macd_bar > 0 else 0
        trend += 10 if 45 <= rsi14 <= 70 else 0
        trend = _clamp(trend)

        amount = max(_number(row.get("amount")), _number(realtime.get("amount")))
        liquidity = _percentile(amounts, max(0.0, amount))
        pe = _number(row.get("pe", realtime.get("pe")))
        pb = _number(row.get("pb"))
        valuation = _valuation_score(pe, pb)
        volume_ratio = _number(_nested(indicators, "volume", "ratio"), 1)
        if volume_ratio < 0.5:
            volume_score = 25.0
        elif volume_ratio < 1:
            volume_score = 50.0
        elif volume_ratio <= 2:
            volume_score = 85.0
        elif volume_ratio <= 3:
            volume_score = 70.0
        else:
            volume_score = 45.0
        volatility = max(0.0, _number(indicators.get("volatility"), 4))
        low_volatility = _clamp(100 - volatility * 12)
        factors = {
            "momentum": momentum,
            "trend": trend,
            "liquidity": liquidity,
            "valuation": valuation,
            "volume": volume_score,
            "low_volatility": low_volatility,
        }
        score = sum(factors[key] * weights.get(key, 0) for key in factors) / weight_sum
        reasons = [f"20日动量 {d20:+.1f}%", f"成交额 {amount / 100_000_000:.1f}亿"]
        if ma5 > ma10 > ma20 > 0:
            reasons.append("均线多头")
        if volume_ratio >= 1:
            reasons.append(f"量比 {volume_ratio:.2f}")
        if pe > 0:
            reasons.append(f"PE {pe:.1f}")
        results.append({
            "symbol": symbol,
            "name": str(row.get("name", realtime.get("name", ""))),
            "score": round(score, 2),
            "price": round(price, 6),
            "change_pct": round(_number(row.get("change_pct", realtime.get("change_pct"))), 3),
            "amount": round(amount, 2),
            "market_cap": round(_number(row.get("market_cap")), 2),
            "sector": str(row.get("sector", "")),
            "industry": str(row.get("industry", "")),
            "factors": {key: round(value, 2) for key, value in factors.items()},
            "evidence": reasons,
        })
    return sorted(results, key=lambda item: (-item["score"], item["symbol"]))


def _latest_path(market: str) -> Path:
    return SCREENING_DIR / f"latest-{market}.json"


def latest_screening(market: str, *, max_age_minutes: Optional[int] = None, now: Optional[datetime] = None) -> Optional[Dict[str, Any]]:
    normalized = str(market or "").strip().lower()
    payload = None
    settings: Mapping[str, Any] = {}
    store: Optional[MongoScreeningStore] = None
    try:
        from src.config import cfg

        settings = cfg.screening
        store = get_screening_store(settings)
        if store is not None:
            payload = store.latest_run(normalized)
    except Exception as exc:
        if store is not None:
            mark_screening_store_failed(settings, store, exc)
        logger.warning("MongoDB latest-screening read failed; trying JSON: %s", exc)
    if payload is None:
        try:
            payload = json.loads(_latest_path(normalized).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None
    if max_age_minutes is not None:
        generated = _parse_time(payload.get("generated_at"))
        current = now or datetime.now().astimezone()
        if generated is None:
            return None
        if generated.tzinfo is None and current.tzinfo is not None:
            generated = generated.replace(tzinfo=current.tzinfo)
        if current - generated > timedelta(minutes=max(1, max_age_minutes)):
            return None
    return payload


def run_screening(
    market: str,
    *,
    held_symbols: Sequence[Any],
    configured_symbols: Sequence[Any],
    fallback_symbols: Sequence[Any],
    settings: Mapping[str, Any],
    autonomous_config: Mapping[str, Any],
    snapshot_loader: SnapshotLoader,
    now: datetime,
) -> ScreeningOutcome:
    market = str(market).strip().lower()
    held = _dedupe(held_symbols)
    configured = _dedupe(configured_symbols)
    fallback = _dedupe(fallback_symbols)
    enabled = bool(settings.get("enabled", True))
    max_universe = max(len(held), int(autonomous_config.get("max_universe_size", 10)))
    snapshot_limit = max(1, min(60, int(settings.get("snapshot_limit", 24))))
    shortlist_size = max(1, min(max_universe, int(settings.get("shortlist_size", 8))))
    workers = max(1, int(autonomous_config.get("market_data_workers", 4)))
    store = get_screening_store(settings)
    discovery_error = ""
    discovered_count = 0
    rejected: Dict[str, int] = {}

    if not enabled:
        source = "disabled"
        status = "disabled"
        rows = [{"symbol": item, "name": ""} for item in (configured or fallback)]
    elif configured:
        source = "configured-hard-pool"
        status = "configured_pool"
        rows = [{"symbol": item, "name": ""} for item in configured]
    else:
        try:
            discovery = _discover(market, settings, now, store)
            source = str(discovery.get("source", "market-data-provider"))
            status = "screened"
            discovered_count = len(discovery.get("data", []))
            rows, rejected = _filter_candidates(discovery.get("data", []), market, settings)
            if not rows:
                raise RuntimeError("all discovered candidates were filtered out")
        except Exception as exc:
            source = "optimizer-default-fallback"
            status = "fallback"
            discovery_error = str(exc)[:1000]
            rows = [{"symbol": item, "name": ""} for item in fallback]

    ranked = sorted(rows, key=_cheap_score, reverse=True)
    preselected = ranked[:snapshot_limit]
    requested = _dedupe([*held, *(row.get("symbol") for row in preselected)])
    snapshots, errors = snapshot_loader(requested, workers)
    scored = _factor_rows(preselected, snapshots, settings) if enabled else []
    mongo_snapshots_written = False
    if store is not None:
        try:
            store.write_snapshots(market, snapshots, now)
            store.write_factors(market, scored, now)
            mongo_snapshots_written = True
        except Exception as exc:
            mark_screening_store_failed(settings, store, exc)
            logger.warning("MongoDB screening snapshot/factor write failed: %s", exc)
            store = None
    selected = scored[:shortlist_size]
    selected_symbols = [item["symbol"] for item in selected]
    if not selected_symbols:
        selected_symbols = [symbol for symbol in requested if symbol not in held][:shortlist_size]
        selected = [
            {
                "symbol": symbol,
                "name": "",
                "score": None,
                "price": _number(_nested(snapshots.get(symbol, {}), "realtime", "price")),
                "factors": {},
                "evidence": ["量化因子不足，使用安全回退候选"],
            }
            for symbol in selected_symbols
            if symbol in snapshots
        ]
    symbols = _dedupe([*held, *selected_symbols], max_universe)
    selected_snapshots = {symbol: snapshots[symbol] for symbol in symbols if symbol in snapshots}
    selected_errors = {symbol: error for symbol, error in errors.items() if symbol in symbols or symbol in requested}
    audit: Dict[str, Any] = {
        "generated_at": now.isoformat(timespec="seconds"),
        "market": market,
        "enabled": enabled,
        "status": status,
        "source": source,
        "configured_hard_pool": bool(configured),
        "discovered_count": discovered_count or len(rows),
        "candidate_count": len(rows),
        "prefetched_count": len(preselected),
        "scored_count": len(scored),
        "shortlist_size": shortlist_size,
        "selected_symbols": selected_symbols,
        "allowed_symbols": symbols,
        "selected": selected,
        "rejected": rejected,
        "discovery_error": discovery_error or None,
        "market_data_errors": selected_errors,
        "storage_backend": "mongodb+json" if mongo_snapshots_written else "json",
    }
    _write_json(_latest_path(market), audit)
    if store is not None:
        try:
            store.write_run(audit)
        except Exception as exc:
            mark_screening_store_failed(settings, store, exc)
            logger.warning("MongoDB screening run write failed; JSON audit is intact: %s", exc)
    return ScreeningOutcome(
        symbols=symbols,
        snapshots=selected_snapshots,
        market_data_errors=selected_errors,
        audit=audit,
    )
