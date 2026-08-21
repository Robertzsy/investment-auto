from __future__ import annotations

import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.manager.skill_models import SESSION_SCOPES
from src.paths import runtime_dir


SESSION_DIR = runtime_dir() / "manager" / "sessions"
INDEX_FILE = SESSION_DIR / "index.json"
_lock = threading.RLock()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


class SessionStore:
    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = (root or SESSION_DIR).resolve()
        self.index_file = self.root / "index.json"

    def _path(self, scope: str) -> Path:
        if scope not in SESSION_SCOPES:
            raise ValueError(f"未知会话范围: {scope}")
        return self.root / f"{scope}.json"

    def load(self, scope: str) -> Dict[str, Any]:
        path = self._path(scope)
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {
                "scope": scope,
                "created_at": _now(),
                "updated_at": _now(),
                "last_skill": "",
                "last_request": "",
                "entities": [],
                "executions": [],
            }

    def last_scope(self) -> str:
        try:
            value = json.loads(self.index_file.read_text(encoding="utf-8"))
            scope = str(value.get("last_scope", ""))
            return scope if scope in SESSION_SCOPES else ""
        except (OSError, json.JSONDecodeError, AttributeError):
            return ""

    def records(self) -> List[Dict[str, Any]]:
        """Return one bounded, UI-safe snapshot for every domain session."""
        rows: List[Dict[str, Any]] = []
        for scope in sorted(SESSION_SCOPES):
            value = self.load(scope)
            rows.append({
                "scope": scope,
                "created_at": value.get("created_at", ""),
                "updated_at": value.get("updated_at", ""),
                "last_skill": value.get("last_skill", ""),
                "last_request": value.get("last_request", ""),
                "last_status": value.get("last_status", ""),
                "last_summary": value.get("last_summary", ""),
                "entities": list(value.get("entities", []))[:20],
                "execution_count": len(value.get("executions", [])),
                "recent_executions": list(value.get("executions", []))[-5:][::-1],
            })
        return rows

    def record(
        self,
        scope: str,
        *,
        request: str,
        skill: str,
        execution_id: str,
        status: str,
        entities: Optional[list[str]] = None,
        summary: str = "",
    ) -> Dict[str, Any]:
        from src.secret_store import redact_text

        with _lock:
            current = self.load(scope)
            current.update({
                "scope": scope,
                "updated_at": _now(),
                "last_request": redact_text(str(request))[:4000],
                "last_skill": str(skill)[:120],
                "last_status": str(status)[:80],
                "last_summary": redact_text(str(summary))[:4000],
            })
            if entities:
                merged = [redact_text(str(item))[:120] for item in entities if str(item).strip()]
                current["entities"] = list(dict.fromkeys(merged + list(current.get("entities", []))))[:20]
            executions = list(current.get("executions", []))
            executions.append({
                "execution_id": execution_id,
                "skill": skill,
                "status": status,
                "created_at": _now(),
            })
            current["executions"] = executions[-50:]
            _atomic_json(self._path(scope), current)
            _atomic_json(self.index_file, {"last_scope": scope, "updated_at": _now()})
            return current

    def prompt(self, max_chars: int = 12000) -> str:
        rows = []
        for scope in sorted(SESSION_SCOPES):
            value = self.load(scope)
            if not value.get("last_request"):
                continue
            rows.append(
                f"- {scope}: last_skill={value.get('last_skill', '')}; "
                f"entities={value.get('entities', [])}; last_request={value.get('last_request', '')}; "
                f"last_status={value.get('last_status', '')}"
            )
        return "\n".join(rows)[:max_chars]
