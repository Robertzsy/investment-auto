"""Resumable cycle checkpoints (architecture.checkpoint_cycles).

A trading cycle is a long multi-stage job.  Each completed stage is atomically
archived under runtime/trading/checkpoints/<cycle_id>/stages/, so a timeout or
process restart only re-runs the unfinished stages instead of losing the whole
cycle.  Stage files are independent, which keeps parallel per-symbol workers
race-free; index.json carries cycle-level state including the execution flag.

Execution is deliberately fail-safe: a cycle whose research finished but whose
fills were not confirmed is never replayed.  The next scheduled round picks
the work up; a duplicated fill is worse than a missed one.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
CHECKPOINT_DIR = ROOT / "runtime" / "trading" / "checkpoints"
TIMEZONE = ZoneInfo("Asia/Shanghai")


def sanitize_cycle_id(value: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "cycle").strip()).strip("-")[:80]
    return safe or "cycle"


def sanitize_stage(value: Any) -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or "stage").strip()).strip("-")[:120]
    return safe or "stage"


def _cycle_dir(cycle_id: str) -> Path:
    return CHECKPOINT_DIR / sanitize_cycle_id(cycle_id)


def _index_path(cycle_id: str) -> Path:
    return _cycle_dir(cycle_id) / "index.json"


def _stage_path(cycle_id: str, stage: str) -> Path:
    return _cycle_dir(cycle_id) / "stages" / f"{sanitize_stage(stage)}.json"


def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(
        json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str),
        encoding="utf-8",
    )
    temporary.replace(path)


def _read_json(path: Path) -> Optional[Dict[str, Any]]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, Mapping) else None


def init_checkpoint(
    cycle_id: str,
    market: str,
    symbols: Sequence[str],
    *,
    label: str = "",
    generated_at: str = "",
) -> Dict[str, Any]:
    """Create the cycle index; returns the stored state."""
    payload = {
        "cycle_id": sanitize_cycle_id(cycle_id),
        "market": str(market or "").lower(),
        "symbols": [str(symbol).upper() for symbol in symbols],
        "label": str(label or "")[:60],
        "generated_at": str(generated_at or ""),
        "created_at": _now_iso(),
        "updated_at": _now_iso(),
        "status": "running",
        "execution_completed": False,
        "completed_stages": 0,
    }
    _atomic_write(_index_path(payload["cycle_id"]), payload)
    return payload


def _now_iso() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def load_checkpoint(cycle_id: str) -> Optional[Dict[str, Any]]:
    return _read_json(_index_path(cycle_id))


def save_stage(cycle_id: str, stage: str, payload: Mapping[str, Any]) -> None:
    _atomic_write(_stage_path(cycle_id, stage), {"stage": str(stage), "saved_at": _now_iso(), **dict(payload)})
    update_index(cycle_id, {"updated_at": _now_iso()})


def load_stage(cycle_id: str, stage: str) -> Optional[Dict[str, Any]]:
    return _read_json(_stage_path(cycle_id, stage))


def list_stages(cycle_id: str) -> List[str]:
    directory = _cycle_dir(cycle_id) / "stages"
    if not directory.exists():
        return []
    names = [path.stem for path in directory.glob("*.json") if path.stem != "index"]
    return sorted(names)


def update_index(cycle_id: str, updates: Mapping[str, Any]) -> None:
    current = load_checkpoint(cycle_id) or {}
    current.update(dict(updates))
    current["updated_at"] = _now_iso()
    _atomic_write(_index_path(cycle_id), current)


def mark_completed(cycle_id: str) -> None:
    update_index(cycle_id, {"status": "completed"})


def mark_execution_completed(cycle_id: str) -> None:
    update_index(cycle_id, {"execution_completed": True, "status": "completed"})


def mark_execution_pending(cycle_id: str) -> None:
    update_index(cycle_id, {"execution_pending": True, "execution_completed": False})


def list_incomplete(now: Optional[datetime] = None, stale_minutes: int = 90) -> List[Dict[str, Any]]:
    """Return runnable, non-stale checkpoint indexes ordered by age."""
    current = now or datetime.now(TIMEZONE)
    if current.tzinfo is None:
        current = current.replace(tzinfo=TIMEZONE)
    results: List[Dict[str, Any]] = []
    if not CHECKPOINT_DIR.exists():
        return results
    for path in sorted(CHECKPOINT_DIR.glob("*/index.json"), key=lambda item: item.stat().st_mtime):
        payload = _read_json(path)
        if not payload:
            continue
        if payload.get("status") == "completed":
            continue
        updated = _parse_time(payload.get("updated_at"))
        age = (current - updated).total_seconds() if updated is not None else float("inf")
        if age > max(1, stale_minutes) * 60:
            continue
        results.append(payload)
    return results


def _parse_time(value: Any) -> Optional[datetime]:
    try:
        parsed = datetime.fromisoformat(str(value or ""))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=TIMEZONE)
    return parsed


def discard_checkpoint(cycle_id: str) -> None:
    directory = _cycle_dir(cycle_id)
    if not directory.exists():
        return
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_file():
            path.unlink()
    for path in sorted(directory.rglob("*"), reverse=True):
        if path.is_dir():
            try:
                path.rmdir()
            except OSError:
                pass
    try:
        directory.rmdir()
    except OSError:
        pass
