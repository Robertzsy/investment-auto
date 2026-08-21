from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Optional
from zoneinfo import ZoneInfo

from engine.config import cfg

ROOT = Path(__file__).resolve().parents[2]
from engine.paths import runtime_dir
CONTROL_FILE = runtime_dir() / "trading" / "control.json"

DEFAULT_STATE: Dict[str, Any] = {
    "paused": False,
    "kill_switch": False,
    "reason": "",
    "updated_at": None,
    "updated_by": "system",
}


def _now() -> str:
    timezone = ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai"))
    return datetime.now(timezone).isoformat(timespec="seconds")


def load_state(path: Optional[Path] = None) -> Dict[str, Any]:
    target = path or CONTROL_FILE
    if not target.exists():
        return dict(DEFAULT_STATE)
    try:
        data = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {**DEFAULT_STATE, "paused": True, "reason": "控制文件损坏，已安全暂停"}
    return {**DEFAULT_STATE, **data}


def save_state(state: Dict[str, Any], path: Optional[Path] = None) -> Dict[str, Any]:
    target = path or CONTROL_FILE
    target.parent.mkdir(parents=True, exist_ok=True)
    normalized = {**DEFAULT_STATE, **state, "updated_at": _now()}
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(normalized, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return normalized


def set_paused(
    paused: bool,
    *,
    reason: str = "",
    updated_by: str = "human",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    state = load_state(path)
    if not paused and state.get("kill_switch"):
        raise RuntimeError("紧急停止仍处于激活状态；请先执行 reset-kill")
    state.update({"paused": bool(paused), "reason": reason, "updated_by": updated_by})
    return save_state(state, path)


def activate_kill_switch(
    *,
    reason: str = "人工紧急停止",
    updated_by: str = "human",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    state = load_state(path)
    state.update({"paused": True, "kill_switch": True, "reason": reason, "updated_by": updated_by})
    return save_state(state, path)


def reset_kill_switch(
    *,
    reason: str = "人工解除紧急停止",
    updated_by: str = "human",
    path: Optional[Path] = None,
) -> Dict[str, Any]:
    state = load_state(path)
    state.update({"paused": True, "kill_switch": False, "reason": reason, "updated_by": updated_by})
    return save_state(state, path)
