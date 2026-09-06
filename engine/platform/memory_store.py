from __future__ import annotations

import json
import os
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
from engine.paths import runtime_dir
MEMORY_ROOT = runtime_dir() / "memory"
_write_lock = threading.RLock()


def _safe_name(value: str) -> str:
    cleaned = re.sub(r"[^a-z0-9_-]+", "-", str(value).lower()).strip("-")
    if not cleaned:
        raise ValueError("集合名称不能为空")
    return cleaned[:80]


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


class StructuredMemoryStore:
    """Append-only JSON records with an optional best-effort MongoDB index.

    JSON remains the audit source of truth. MongoDB is a query accelerator, so
    an unavailable database never prevents an investment or management task.
    """

    def __init__(self, directory: Path = MEMORY_ROOT) -> None:
        self.directory = directory

    def append(self, collection: str, record: Mapping[str, Any]) -> Dict[str, Any]:
        name = _safe_name(collection)
        payload = dict(record)
        payload.setdefault("record_id", uuid.uuid4().hex)
        payload.setdefault("created_at", _now())
        destination = self.directory / name / f"{payload['record_id']}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        temporary = destination.with_suffix(".json.tmp")
        with _write_lock:
            temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
            temporary.replace(destination)
        self._mongo_insert(name, payload)
        return payload

    def recent(
        self,
        collection: str,
        *,
        limit: int = 10,
        filters: Optional[Mapping[str, Any]] = None,
    ) -> List[Dict[str, Any]]:
        name = _safe_name(collection)
        selected: List[Dict[str, Any]] = []
        paths = sorted((self.directory / name).glob("*.json"), key=lambda item: item.stat().st_mtime, reverse=True)
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            if filters and any(value.get(key) != expected for key, expected in filters.items()):
                continue
            selected.append(value)
            if len(selected) >= max(0, limit):
                break
        return selected

    @staticmethod
    def _mongo_insert(collection: str, payload: Mapping[str, Any]) -> None:
        uri = os.getenv("MONGODB_URI", "").strip()
        if not uri:
            return
        try:
            from pymongo import MongoClient

            client = MongoClient(uri, serverSelectionTimeoutMS=800, connectTimeoutMS=800)
            database = client[os.getenv("MONGODB_DATABASE", "investment_auto")]
            database[collection].update_one(
                {"record_id": payload.get("record_id")},
                {"$set": dict(payload)},
                upsert=True,
            )
            client.close()
        except Exception:
            # The JSON record has already been committed. MongoDB is optional.
            return

