from __future__ import annotations

import logging
import math
import os
import threading
import time
from datetime import datetime, timedelta
from typing import Any, Dict, Mapping, Optional, Sequence

logger = logging.getLogger("investment-auto.screening.storage")

_LOCK = threading.Lock()
_STORES: Dict[tuple[str, str, int], "MongoScreeningStore"] = {}
_FAILED_UNTIL: Dict[tuple[str, str, int], float] = {}


def _datetime(value: Any) -> Optional[datetime]:
    if isinstance(value, datetime):
        return value
    try:
        return datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _safe(value: Any) -> Any:
    """Convert provider values into BSON-safe, deterministic primitives."""
    if isinstance(value, Mapping):
        return {
            str(key).replace(".", "\uff0e").replace("$", "\uff04"): _safe(item)
            for key, item in value.items()
        }
    if isinstance(value, (list, tuple)):
        return [_safe(item) for item in value]
    if isinstance(value, datetime):
        return value
    if isinstance(value, float) and not math.isfinite(value):
        return None
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "item"):
        try:
            return _safe(value.item())
        except (TypeError, ValueError):
            pass
    return str(value)


def _public(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): _public(item)
            for key, item in value.items()
            if key != "_id"
        }
    if isinstance(value, list):
        return [_public(item) for item in value]
    if isinstance(value, datetime):
        return value.isoformat(timespec="seconds")
    return value


class MongoScreeningStore:
    """MongoDB-backed screening cache and audit repository.

    Imports pymongo lazily so JSON-only deployments keep working even when the
    optional dependency is unavailable.
    """

    def __init__(self, uri: str, database: str, timeout_ms: int = 1500) -> None:
        from pymongo import ASCENDING, DESCENDING, MongoClient

        self._ascending = ASCENDING
        self._descending = DESCENDING
        self.client = MongoClient(
            uri,
            appname="investment-auto",
            connectTimeoutMS=timeout_ms,
            serverSelectionTimeoutMS=timeout_ms,
            socketTimeoutMS=max(timeout_ms, 3000),
            tz_aware=True,
        )
        self.client.admin.command("ping")
        self.db = self.client[database]
        self._ensure_indexes()

    def _ensure_indexes(self) -> None:
        asc = self._ascending
        desc = self._descending
        self.db.securities.create_index(
            [("market", asc), ("symbol", asc)], unique=True, name="market_symbol_unique"
        )
        self.db.securities.create_index(
            [("market", asc), ("discovery_id", asc), ("amount", desc)],
            name="latest_candidates_by_liquidity",
        )
        self.db.market_snapshots.create_index(
            [("market", asc), ("symbol", asc)], unique=True, name="market_symbol_unique"
        )
        self.db.market_snapshots.create_index(
            [("market", asc), ("updated_at", desc)], name="market_snapshot_recency"
        )
        self.db.screening_factors.create_index(
            [("market", asc), ("symbol", asc)], unique=True, name="market_symbol_unique"
        )
        self.db.screening_factors.create_index(
            [("market", asc), ("score", desc)], name="market_factor_score"
        )
        self.db.screening_runs.create_index("run_id", unique=True, name="run_id_unique")
        self.db.screening_runs.create_index(
            [("market", asc), ("generated_at", desc)], name="market_run_recency"
        )
        self.db.screening_meta.create_index(
            [("kind", asc), ("market", asc)], unique=True, name="kind_market_unique"
        )

    def write_discovery(self, market: str, payload: Mapping[str, Any]) -> None:
        from pymongo import ReplaceOne

        generated = _datetime(payload.get("generated_at")) or datetime.now().astimezone()
        discovery_id = f"{market}:{generated.isoformat(timespec='seconds')}"
        operations = []
        for original in payload.get("data", []):
            if not isinstance(original, Mapping):
                continue
            symbol = str(original.get("symbol", original.get("code", ""))).strip().upper()
            if not symbol:
                continue
            document = _safe(original)
            document.update({
                "market": market,
                "symbol": symbol,
                "discovery_id": discovery_id,
                "updated_at": generated,
            })
            operations.append(ReplaceOne(
                {"market": market, "symbol": symbol}, document, upsert=True
            ))
        if operations:
            self.db.securities.bulk_write(operations, ordered=False)
        self.db.screening_meta.replace_one(
            {"kind": "discovery", "market": market},
            {
                "kind": "discovery",
                "market": market,
                "discovery_id": discovery_id,
                "generated_at": generated,
                "source": str(payload.get("source", "market-data-provider")),
                "count": len(operations),
            },
            upsert=True,
        )

    def read_discovery(
        self,
        market: str,
        *,
        now: datetime,
        refresh_minutes: int,
        limit: int,
    ) -> Optional[Dict[str, Any]]:
        meta = self.db.screening_meta.find_one({"kind": "discovery", "market": market})
        if not meta:
            return None
        generated = _datetime(meta.get("generated_at"))
        if generated is None:
            return None
        if generated.tzinfo is None and now.tzinfo is not None:
            generated = generated.replace(tzinfo=now.tzinfo)
        if now - generated > timedelta(minutes=max(1, refresh_minutes)):
            return None
        cursor = self.db.securities.find({
            "market": market,
            "discovery_id": meta.get("discovery_id"),
        }).sort("amount", self._descending).limit(limit)
        rows = []
        for document in cursor:
            for key in ("_id", "market", "discovery_id", "updated_at"):
                document.pop(key, None)
            rows.append(_public(document))
        if not rows:
            return None
        return {
            "generated_at": generated.isoformat(timespec="seconds"),
            "market": market,
            "source": str(meta.get("source", "mongodb")),
            "cached": True,
            "cache_backend": "mongodb",
            "data": rows,
        }

    def write_snapshots(
        self,
        market: str,
        snapshots: Mapping[str, Any],
        generated_at: datetime,
    ) -> None:
        from pymongo import ReplaceOne

        operations = [
            ReplaceOne(
                {"market": market, "symbol": str(symbol).upper()},
                {
                    "market": market,
                    "symbol": str(symbol).upper(),
                    "updated_at": generated_at,
                    "snapshot": _safe(snapshot),
                },
                upsert=True,
            )
            for symbol, snapshot in snapshots.items()
        ]
        if operations:
            self.db.market_snapshots.bulk_write(operations, ordered=False)

    def write_factors(
        self,
        market: str,
        factors: Sequence[Mapping[str, Any]],
        generated_at: datetime,
    ) -> None:
        from pymongo import ReplaceOne

        operations = []
        for factor in factors:
            symbol = str(factor.get("symbol", "")).upper()
            if not symbol:
                continue
            document = _safe(factor)
            document.update({"market": market, "symbol": symbol, "updated_at": generated_at})
            operations.append(ReplaceOne(
                {"market": market, "symbol": symbol}, document, upsert=True
            ))
        if operations:
            self.db.screening_factors.bulk_write(operations, ordered=False)

    def write_run(self, audit: Mapping[str, Any]) -> None:
        market = str(audit.get("market", "")).lower()
        generated = _datetime(audit.get("generated_at")) or datetime.now().astimezone()
        run_id = f"{market}:{generated.isoformat(timespec='seconds')}"
        self.db.screening_runs.replace_one(
            {"run_id": run_id},
            {
                "run_id": run_id,
                "market": market,
                "generated_at": generated,
                "audit": _safe(audit),
            },
            upsert=True,
        )

    def latest_run(self, market: str) -> Optional[Dict[str, Any]]:
        document = self.db.screening_runs.find_one(
            {"market": market}, sort=[("generated_at", self._descending)]
        )
        audit = document.get("audit") if document else None
        return _public(audit) if isinstance(audit, Mapping) else None


def get_screening_store(settings: Mapping[str, Any]) -> Optional[MongoScreeningStore]:
    storage = settings.get("storage", {})
    storage = storage if isinstance(storage, Mapping) else {}
    backend = str(storage.get("backend", "auto")).strip().lower()
    if backend not in {"auto", "mongodb"}:
        return None
    uri_env = str(storage.get("mongodb_uri_env", "MONGODB_URI")).strip() or "MONGODB_URI"
    uri = os.getenv(uri_env, "").strip()
    if not uri:
        if backend == "auto":
            return None
        uri = "mongodb://127.0.0.1:27017"
    database = str(storage.get("mongodb_database", "investment_auto")).strip() or "investment_auto"
    timeout_ms = max(250, min(10000, int(storage.get("connect_timeout_ms", 1500))))
    retry_seconds = max(5, min(3600, int(storage.get("retry_seconds", 60))))
    key = (uri, database, timeout_ms)
    with _LOCK:
        existing = _STORES.get(key)
        if existing is not None:
            return existing
        if _FAILED_UNTIL.get(key, 0) > time.monotonic():
            return None
    try:
        store = MongoScreeningStore(uri, database, timeout_ms)
    except Exception as exc:
        with _LOCK:
            _FAILED_UNTIL[key] = time.monotonic() + retry_seconds
        logger.warning("MongoDB screening store unavailable; using JSON fallback: %s", exc)
        return None
    with _LOCK:
        _STORES[key] = store
        _FAILED_UNTIL.pop(key, None)
    logger.info("MongoDB screening store connected (database=%s)", database)
    return store


def mark_screening_store_failed(
    settings: Mapping[str, Any], store: MongoScreeningStore, exc: Exception
) -> None:
    """Temporarily evict a failed connection so repeated UI reads fail fast."""
    storage = settings.get("storage", {})
    storage = storage if isinstance(storage, Mapping) else {}
    retry_seconds = max(5, min(3600, int(storage.get("retry_seconds", 60))))
    with _LOCK:
        matching = [key for key, value in _STORES.items() if value is store]
        for key in matching:
            _STORES.pop(key, None)
            _FAILED_UNTIL[key] = time.monotonic() + retry_seconds
    try:
        store.client.close()
    except Exception:
        pass
    logger.warning("MongoDB screening store disabled for %s seconds: %s", retry_seconds, exc)
