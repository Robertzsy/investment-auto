from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

from src.config import cfg


ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "runtime" / "reports"
AUDIT_DIR = ROOT / "runtime" / "trading" / "audit"


def _now(value: Optional[datetime] = None) -> datetime:
    timezone = ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai"))
    if value is None:
        return datetime.now(timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _session_active(market: str, current: datetime, sessions: List[Mapping[str, Any]]) -> bool:
    from src.scheduler import _day_of_week, _expand_days

    if current.weekday() not in _expand_days(_day_of_week(market, current.strftime("%H:%M"))):
        return False
    minute = current.hour * 60 + current.minute
    for session in sessions:
        try:
            start_hour, start_minute = map(int, str(session.get("start", "")).split(":", 1))
            end_hour, end_minute = map(int, str(session.get("end", "")).split(":", 1))
        except (TypeError, ValueError):
            continue
        start, end = start_hour * 60 + start_minute, end_hour * 60 + end_minute
        if (start <= end and start <= minute <= end) or (start > end and (minute >= start or minute <= end)):
            return True
    return False


def _next_time(market: str, values: List[str], current: datetime) -> Optional[str]:
    from src.scheduler import _cron_trigger

    candidates = [
        value for value in (
            _cron_trigger(market, item).get_next_fire_time(None, current) for item in values if item
        ) if value is not None
    ]
    return min(candidates).isoformat(timespec="seconds") if candidates else None


def _latest_report(market: str) -> Optional[Dict[str, Any]]:
    files = sorted(REPORT_DIR.glob(f"*-{market}-*.md"), key=lambda path: path.stat().st_mtime, reverse=True) if REPORT_DIR.exists() else []
    if not files:
        return None
    path = files[0]
    try:
        prefix = path.read_text(encoding="utf-8")[:1000]
    except OSError:
        prefix = ""
    generated = re.search(r"生成时间：([^\s|]+)", prefix)
    return {
        "file": path.name,
        "generated_at": generated.group(1) if generated else datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds"),
    }


def runtime_status(now: Optional[datetime] = None) -> Dict[str, Any]:
    from src.investment.command_bus import InvestmentAgentClient
    from src.investment.mandate import get_mandate
    from src.trading.control import load_state
    from src.trading.controller import autonomous_enabled

    current = _now(now)
    markets = {}
    for market in cfg.enabled_markets:
        market_config = cfg.market_config(market)
        sessions = list(market_config.get("trading", {}).get("session", []) or [])
        intraday = [str(value) for value in cfg.intraday_times(market)]
        close = str(cfg.close_time(market) or "")
        markets[market] = {
            "sessions": sessions,
            "in_session": _session_active(market, current, sessions),
            "next_cycle": _next_time(market, intraday, current),
            "next_close": _next_time(market, [close], current),
            "latest_report": _latest_report(market),
        }
    audits = sorted(AUDIT_DIR.glob("*.json"), key=lambda path: path.stat().st_mtime, reverse=True) if AUDIT_DIR.exists() else []
    latest_audit: Any = None
    if audits:
        try:
            latest_audit = json.loads(audits[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            latest_audit = {"file": audits[0].name, "error": str(exc)}
    enabled = autonomous_enabled()
    return {
        "current_time": current.isoformat(timespec="seconds"),
        "timezone": str(current.tzinfo),
        "utc_offset": current.strftime("%z"),
        "investment_worker_alive": InvestmentAgentClient.worker_alive(),
        # Keep the historic ``enabled`` field for the dashboard while exposing
        # the less ambiguous name to new command-bus clients.
        "enabled": enabled,
        "autonomous_enabled": enabled,
        "operation_mode": str(cfg.autonomous.get("operation_mode", "automatic")),
        "auto_execute": bool(cfg.autonomous.get("auto_execute", False)),
        "control": load_state(),
        "mandate": get_mandate(),
        "markets": markets,
        "latest_audit": latest_audit,
    }
