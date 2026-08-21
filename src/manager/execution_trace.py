from __future__ import annotations

import json
import re
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from src.paths import runtime_dir


TRACE_DIR = runtime_dir() / "manager" / "trajectories"
_lock = threading.RLock()
_EXECUTION_ID = re.compile(r"^[a-f0-9]{32}$")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="milliseconds")


def _safe(value: Any, limit: int = 30000) -> Any:
    try:
        text = json.dumps(value, ensure_ascii=False, default=str)
    except Exception:
        text = json.dumps(repr(value), ensure_ascii=False)
    try:
        from src.secret_store import redact_mapping

        redacted = redact_mapping(json.loads(text))
        text = json.dumps(redacted, ensure_ascii=False, default=str)
    except Exception:
        pass
    if len(text) <= limit:
        return json.loads(text)
    return {"truncated": True, "preview": text[:limit]}


class ExecutionTrace:
    def __init__(
        self,
        *,
        request: str,
        skill: str,
        version: str,
        session_scope: str,
        inputs: Mapping[str, Any],
        root: Optional[Path] = None,
    ) -> None:
        self.execution_id = uuid.uuid4().hex
        self.root = (root or TRACE_DIR).resolve()
        self.payload: Dict[str, Any] = {
            "execution_id": self.execution_id,
            "request": _safe(str(request)[:8000]),
            "skill": skill,
            "skill_version": version,
            "session_scope": session_scope,
            "inputs": _safe(inputs),
            "status": "running",
            "started_at": _now(),
            "updated_at": _now(),
            "steps": [],
        }
        self._commit()

    @property
    def path(self) -> Path:
        return self.root / self.payload["started_at"][:10] / f"{self.execution_id}.json"

    def _commit(self) -> None:
        with _lock:
            self.payload["updated_at"] = _now()
            path = self.path
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(".json.tmp")
            temporary.write_text(
                json.dumps(self.payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
            )
            temporary.replace(path)

    def step(self, value: Mapping[str, Any]) -> None:
        self.payload["steps"].append(_safe(value))
        self._commit()

    def finish(self, *, status: str, validation: Mapping[str, Any], result: Mapping[str, Any]) -> None:
        self.payload.update({
            "status": str(status),
            "validation": _safe(validation),
            "result": _safe(result),
            "finished_at": _now(),
        })
        self._commit()

    @classmethod
    def recent(cls, limit: int = 50, root: Optional[Path] = None) -> list[Dict[str, Any]]:
        """List recent redacted trajectories without exposing filesystem paths."""
        base = (root or TRACE_DIR).resolve()
        bounded = max(1, min(200, int(limit or 50)))
        if not base.exists():
            return []
        paths = sorted(
            base.glob("*/*.json"),
            key=lambda path: path.stat().st_mtime,
            reverse=True,
        )[:bounded]
        rows: list[Dict[str, Any]] = []
        for path in paths:
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if not isinstance(value, dict):
                continue
            steps = value.get("steps", []) if isinstance(value.get("steps"), list) else []
            rows.append({
                "execution_id": value.get("execution_id", path.stem),
                "skill": value.get("skill", ""),
                "skill_version": value.get("skill_version", ""),
                "session_scope": value.get("session_scope", ""),
                "request": value.get("request", ""),
                "status": value.get("status", ""),
                "started_at": value.get("started_at", ""),
                "finished_at": value.get("finished_at", ""),
                "validation": value.get("validation", {}),
                "step_count": len(steps),
                "completed_steps": sum(1 for step in steps if isinstance(step, dict) and step.get("status") == "completed"),
            })
        return rows

    @classmethod
    def load(cls, execution_id: str, root: Optional[Path] = None) -> Dict[str, Any]:
        normalized = str(execution_id or "").strip().lower()
        if not _EXECUTION_ID.fullmatch(normalized):
            raise ValueError("execution_id 无效")
        base = (root or TRACE_DIR).resolve()
        for path in base.glob(f"*/{normalized}.json"):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as exc:
                raise ValueError("执行轨迹文件无效") from exc
            if isinstance(value, dict):
                return value
        raise FileNotFoundError(f"执行轨迹不存在: {normalized}")
