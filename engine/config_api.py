"""Whitelisted, atomic configuration updates for the product settings pages.

The DSH conversation plane must be able to edit product settings (strategy
guardrails, schedule, markets, screening, notify) WITHOUT ever touching the
non-negotiable boundaries: `trading.mode` stays `paper`, and the `llm`
section is managed by the DSH settings surface, not here.
"""
from __future__ import annotations

import copy
import re
import threading
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

import yaml

from engine.config import cfg

_lock = threading.RLock()

_MARKETS = {"cn", "hk", "us", "etf"}
_TIME_RE = re.compile(r"^(\d{2}):(\d{2})$")


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("必须是布尔值")
    return value


def _enum(*choices: str) -> Callable[[Any], str]:
    def validate(value: Any) -> str:
        if not isinstance(value, str) or value not in choices:
            raise ValueError("必须是 " + "、".join(choices) + " 之一")
        return value

    return validate


def _integer(minimum: int, maximum: int) -> Callable[[Any], int]:
    def validate(value: Any) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or not minimum <= value <= maximum:
            raise ValueError(f"必须是 {minimum}-{maximum} 的整数")
        return value

    return validate


def _time(value: Any) -> str:
    if not isinstance(value, str):
        raise ValueError("必须是 HH:MM 时间字符串")
    match = _TIME_RE.fullmatch(value.strip())
    if match is None or int(match.group(1)) > 23 or int(match.group(2)) > 59:
        raise ValueError("必须是有效的 HH:MM 时间")
    return value.strip()


def _time_list(value: Any) -> list[str]:
    if not isinstance(value, list) or len(value) > 12:
        raise ValueError("必须是不超过 12 项的 HH:MM 时间列表")
    result = [_time(item) for item in value]
    if len(set(result)) != len(result):
        raise ValueError("时间列表不能重复")
    return result


def _market_list(value: Any) -> list[str]:
    if not isinstance(value, list) or not value:
        raise ValueError("至少启用一个市场")
    result = [str(item).strip().lower() for item in value]
    if any(item not in _MARKETS for item in result) or len(set(result)) != len(result):
        raise ValueError("市场列表只能包含不重复的 cn、hk、us、etf")
    return result


def _channels(value: Any) -> list[str]:
    if not isinstance(value, list) or any(item != "webhook" for item in value) or len(set(value)) != len(value):
        raise ValueError("通知渠道只能包含 webhook")
    return list(value)


# This is deliberately an exact product-settings contract, not a top-level
# section whitelist.  Adding a new control requires adding its path and
# validator here first.
_PATH_VALIDATORS: Dict[str, Callable[[Any], Any]] = {
    "autonomous.operation_mode": _enum("manual", "automatic"),
    "autonomous.enabled": _boolean,
    "schedule.macro_daily_time": _time,
    "markets.enable": _market_list,
    "screening.shortlist_size": _integer(1, 100),
    "screening.refresh_minutes": _integer(5, 1440),
    "notify.enabled": _boolean,
    "notify.channels": _channels,
    **{f"schedule.intraday_rounds.{market}": _time_list for market in _MARKETS},
}


def _get_path(target: Mapping[str, Any], parts: list[str]) -> Optional[Any]:
    node: Any = target
    for part in parts:
        if not isinstance(node, Mapping) or part not in node:
            return None
        node = node[part]
    return node


def _set_path(target: Dict[str, Any], parts: list[str], value: Any) -> None:
    node = target
    for part in parts[:-1]:
        child = node.get(part)
        if not isinstance(child, dict):
            child = {}
            node[part] = child
        node = child
    node[parts[-1]] = value


def apply_config_changes(changes: Any) -> Dict[str, Any]:
    """Apply a set of dot-path changes to config.yaml atomically.

    `changes` is a dict of `"section.key.sub": value` entries. Every path
    must be part of the exact product-settings contract and must not touch
    forbidden boundaries. The write is atomic (temp + replace) and reloads
    the runtime config; a failed validation leaves the file untouched.
    """
    if not isinstance(changes, Mapping) or not changes:
        raise ValueError("changes 必须是非空对象")
    with _lock:
        normalized: Dict[str, Any] = {}
        for raw_path, value in changes.items():
            path = str(raw_path).strip()
            parts = [part for part in path.split(".") if part]
            if not parts:
                raise ValueError(f"无效配置路径: {path}")
            if path == "trading.mode" or path.startswith("trading.mode."):
                raise ValueError(f"禁止修改的配置路径: {path}")
            validator = _PATH_VALIDATORS.get(path)
            if validator is None:
                raise ValueError(f"不允许修改的配置路径: {path}")
            try:
                normalized[path] = validator(value)
            except ValueError as exc:
                raise ValueError(f"配置 {path} {exc}") from exc

        config_path = Path(cfg._path)
        document = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        previous = copy.deepcopy(document)
        for path, value in normalized.items():
            _set_path(document, path.split("."), value)

        temporary = config_path.with_suffix(config_path.suffix + ".tmp")
        temporary.write_text(
            yaml.safe_dump(document, allow_unicode=True, sort_keys=False), encoding="utf-8"
        )
        temporary.replace(config_path)
        try:
            cfg.reload()
        except Exception:
            # Roll the file back to the pre-change document on a bad reload.
            rollback = config_path.with_suffix(config_path.suffix + ".rollback")
            rollback.write_text(
                yaml.safe_dump(previous, allow_unicode=True, sort_keys=False), encoding="utf-8"
            )
            rollback.replace(config_path)
            raise
        return sorted(normalized)
