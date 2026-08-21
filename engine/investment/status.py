from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

from engine.config import cfg


ROOT = Path(__file__).resolve().parents[2]
from engine.paths import runtime_dir
REPORT_DIR = runtime_dir() / "reports"
AUDIT_DIR = runtime_dir() / "trading" / "audit"


def _now(value: Optional[datetime] = None) -> datetime:
    timezone = ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai"))
    if value is None:
        return datetime.now(timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _session_active(market: str, current: datetime, sessions: List[Mapping[str, Any]]) -> bool:
    from engine.scheduler import _day_of_week, _expand_days

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
    from engine.scheduler import _cron_trigger

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


def cycle_evidence(market: str, date: str = "", label: str = "") -> Dict[str, Any]:
    """Return report and audit evidence for one scheduled/manual investment cycle."""
    normalized = str(market or "").strip().lower()
    if normalized not in {"cn", "hk", "us", "etf"}:
        raise ValueError("market 必须是 cn、hk、us 或 etf")
    requested_date = re.sub(r"[^0-9]", "", str(date or "")) or _now().strftime("%Y%m%d")
    if len(requested_date) != 8:
        raise ValueError("date 必须是 YYYY-MM-DD 或 YYYYMMDD")
    requested_label = re.sub(r"[^A-Za-z0-9_-]", "", str(label or "").strip())
    report_pattern = f"{requested_date}-{normalized}-{requested_label}.md" if requested_label else f"{requested_date}-{normalized}-*.md"
    reports = sorted(REPORT_DIR.glob(report_pattern), key=lambda path: path.stat().st_mtime, reverse=True) if REPORT_DIR.exists() else []
    audit_pattern = f"{requested_date}*-{normalized}-{requested_label}.json" if requested_label else f"{requested_date}*-{normalized}-*.json"
    audits = sorted(AUDIT_DIR.glob(audit_pattern), key=lambda path: path.stat().st_mtime, reverse=True) if AUDIT_DIR.exists() else []
    audit: Optional[Dict[str, Any]] = None
    audit_file = None
    if audits:
        audit_file = str(audits[0])
        try:
            audit = json.loads(audits[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            audit = {"status": "unreadable", "error": str(exc)}
    evidence_ref = audit.get("evidence_ref") if isinstance(audit, Mapping) else None
    return {
        "market": normalized,
        "date": requested_date,
        "label": requested_label or None,
        "triggered": bool(reports or audits),
        "report": str(reports[0]) if reports else None,
        "report_generated_at": datetime.fromtimestamp(reports[0].stat().st_mtime).astimezone().isoformat(timespec="seconds") if reports else None,
        "audit_file": audit_file,
        "investment_status": audit.get("status") if isinstance(audit, Mapping) else None,
        "error": audit.get("error") if isinstance(audit, Mapping) else None,
        "fills": len((audit.get("execution") or {}).get("fills", [])) if isinstance(audit, Mapping) else 0,
        "evidence_ref": evidence_ref,
    }


def _autonomous_enabled() -> bool:
    enabled = bool(cfg.autonomous.get("enabled", False))
    paper = str(cfg.trading.get("mode", "paper")).strip().lower() == "paper"
    return enabled and paper


def runtime_status(now: Optional[datetime] = None) -> Dict[str, Any]:
    from engine.investment.command_bus import InvestmentAgentClient
    from engine.investment.mandate import get_mandate
    from engine.trading.control import load_state

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
            payload = json.loads(audits[0].read_text(encoding="utf-8"))
            # The desktop polls this snapshot every five seconds.  Returning
            # the full evidence graph made each status response hundreds of
            # kilobytes and duplicated it into command audits.  Detailed
            # evidence remains available through cycle_evidence().
            latest_audit = {
                "file": audits[0].name,
                **{
                    key: payload.get(key)
                    for key in ("generated_at", "market", "label", "status", "reason", "error", "evidence_ref")
                    if payload.get(key) is not None
                },
            }
        except (OSError, json.JSONDecodeError) as exc:
            latest_audit = {"file": audits[0].name, "error": str(exc)}
    enabled = _autonomous_enabled()
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
