from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

import requests

logger = logging.getLogger("investment-auto.research-data")
ROOT = Path(__file__).resolve().parents[2]
CACHE_DIR = ROOT / "runtime" / "data" / "research"
_write_lock = threading.RLock()


def _cache_path(market: str, symbol: str) -> Path:
    safe_symbol = "".join(char for char in symbol.upper() if char.isalnum() or char in ".-")
    return CACHE_DIR / market.lower() / f"{safe_symbol}.json"


def _load_cache(market: str, symbol: str, ttl_minutes: int) -> Optional[Dict[str, Any]]:
    path = _cache_path(market, symbol)
    try:
        age = time.time() - path.stat().st_mtime
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if age > max(1, ttl_minutes) * 60 or not isinstance(value, dict):
        return None
    return value


def _load_mongo_cache(market: str, symbol: str, ttl_minutes: int) -> Optional[Dict[str, Any]]:
    """Best-effort query accelerator; JSON remains the audit source of truth."""
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        return None
    try:
        from pymongo import MongoClient

        client = MongoClient(uri, serverSelectionTimeoutMS=800, connectTimeoutMS=800)
        database = client[os.getenv("MONGODB_DATABASE", "investment_auto")]
        value = database["research_packets"].find_one(
            {"market": market, "symbol": symbol},
            {"_id": False},
        )
        client.close()
        if not isinstance(value, dict):
            return None
        fetched_at = datetime.fromisoformat(str(value.get("fetched_at", "")))
        now = datetime.now(fetched_at.tzinfo or timezone.utc)
        if now - fetched_at > timedelta(minutes=max(1, ttl_minutes)):
            return None
        return value
    except Exception:
        return None


def _save_cache(market: str, symbol: str, payload: Mapping[str, Any]) -> None:
    path = _cache_path(market, symbol)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(".json.tmp")
    with _write_lock:
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(path)
    _save_mongo_cache(payload)


def _save_mongo_cache(payload: Mapping[str, Any]) -> None:
    uri = os.getenv("MONGODB_URI", "").strip()
    if not uri:
        return
    try:
        from pymongo import ASCENDING, MongoClient

        client = MongoClient(uri, serverSelectionTimeoutMS=800, connectTimeoutMS=800)
        collection = client[os.getenv("MONGODB_DATABASE", "investment_auto")]["research_packets"]
        collection.create_index(
            [("market", ASCENDING), ("symbol", ASCENDING)],
            unique=True,
            name="market_symbol_unique",
        )
        collection.update_one(
            {"market": payload.get("market"), "symbol": payload.get("symbol")},
            {"$set": dict(payload)},
            upsert=True,
        )
        client.close()
    except Exception:
        # JSON has already committed successfully. MongoDB is optional.
        return


def _finnhub_news(symbol: str, key: str, timeout: int) -> list[Dict[str, Any]]:
    today = datetime.now(timezone.utc).date()
    response = requests.get(
        "https://finnhub.io/api/v1/company-news",
        params={
            "symbol": symbol,
            "from": (today - timedelta(days=10)).isoformat(),
            "to": today.isoformat(),
            "token": key,
        },
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    if not isinstance(payload, list):
        raise RuntimeError("Finnhub company-news returned a non-list response")
    rows = []
    for item in payload[:12]:
        if not isinstance(item, Mapping) or not item.get("headline"):
            continue
        published = item.get("datetime")
        if published:
            try:
                published = datetime.fromtimestamp(int(published), timezone.utc).isoformat()
            except (TypeError, ValueError, OSError):
                published = str(published)
        rows.append({
            "headline": str(item.get("headline", ""))[:500],
            "summary": str(item.get("summary", ""))[:1200],
            "source": str(item.get("source", ""))[:200],
            "published_at": published,
            "url": str(item.get("url", ""))[:1000],
        })
    return rows


def _finnhub_metrics(symbol: str, key: str, timeout: int) -> Dict[str, Any]:
    response = requests.get(
        "https://finnhub.io/api/v1/stock/metric",
        params={"symbol": symbol, "metric": "all", "token": key},
        timeout=timeout,
    )
    response.raise_for_status()
    payload = response.json()
    metric = payload.get("metric", {}) if isinstance(payload, Mapping) else {}
    if not isinstance(metric, Mapping):
        return {}
    mapping = {
        "pe_ttm": "peTTM",
        "pb_annual": "pbAnnual",
        "roe_ttm": "roeTTM",
        "revenue_growth_ttm_yoy": "revenueGrowthTTMYoy",
        "eps_growth_ttm_yoy": "epsGrowthTTMYoy",
        "current_ratio_annual": "currentRatioAnnual",
        "debt_to_equity_annual": "totalDebt/totalEquityAnnual",
        "net_margin_ttm": "netProfitMarginTTM",
        "dividend_yield_indicated_annual": "dividendYieldIndicatedAnnual",
        "high_52w": "52WeekHigh",
        "low_52w": "52WeekLow",
    }
    return {
        target: metric.get(source)
        for target, source in mapping.items()
        if metric.get(source) not in (None, "")
    }


def fetch_research_packet(
    market: str,
    symbol: str,
    *,
    ttl_minutes: int = 30,
    timeout: int = 12,
) -> Dict[str, Any]:
    """Fetch auditable per-security research data with safe, explicit gaps.

    Finnhub currently supplies US company news and basic financial metrics when
    FINNHUB_API_KEY is configured. Other markets remain explicit data gaps until
    a licensed regional provider is configured; the Agent must never fabricate.
    """
    normalized_market = str(market).strip().lower()
    normalized_symbol = str(symbol).strip().upper()
    cached = _load_mongo_cache(normalized_market, normalized_symbol, ttl_minutes)
    if cached is None:
        cached = _load_cache(normalized_market, normalized_symbol, ttl_minutes)
    if cached is not None:
        return {**cached, "cached": True}

    packet: Dict[str, Any] = {
        "symbol": normalized_symbol,
        "market": normalized_market,
        "fetched_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "news": [],
        "fundamentals": {},
        "sentiment": {},
        "sources": [],
        "errors": {},
        "cached": False,
    }
    key = os.getenv("FINNHUB_API_KEY", "").strip()
    if normalized_market != "us":
        packet["errors"]["company_research"] = "当前外部公司研究数据源只支持美股"
    elif not key:
        packet["errors"]["company_research"] = "未配置 FINNHUB_API_KEY"
    else:
        try:
            packet["news"] = _finnhub_news(normalized_symbol, key, timeout)
            packet["sources"].append("finnhub:company-news")
        except Exception as exc:
            packet["errors"]["news"] = str(exc)[:500]
            logger.warning("Company news fetch failed for %s: %s", normalized_symbol, exc)
        try:
            packet["fundamentals"] = _finnhub_metrics(normalized_symbol, key, timeout)
            packet["sources"].append("finnhub:basic-financials")
        except Exception as exc:
            packet["errors"]["fundamentals"] = str(exc)[:500]
            logger.warning("Company metrics fetch failed for %s: %s", normalized_symbol, exc)

    _save_cache(normalized_market, normalized_symbol, packet)
    return packet
