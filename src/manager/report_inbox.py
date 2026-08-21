from __future__ import annotations

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional


ROOT = Path(__file__).resolve().parents[2]
from src.paths import runtime_dir
INBOX_DIR = runtime_dir() / "manager" / "report_inbox"


def publish_cycle_report(
    result: Mapping[str, Any],
    *,
    title: str,
    report_content: str,
    inbox_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Atomically publish a completed scheduled cycle for the chat process."""
    directory = inbox_dir or INBOX_DIR
    directory.mkdir(parents=True, exist_ok=True)
    event_id = uuid.uuid4().hex
    from src.investment.reporting import format_cycle_result

    payload = {
        "event_id": event_id,
        "type": "investment_cycle_report",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "market": result.get("market"),
        "label": result.get("label"),
        "status": result.get("status"),
        "execution_status": (result.get("autonomous") or {}).get("status")
        if isinstance(result.get("autonomous"), Mapping) else None,
        "report": result.get("report"),
        "title": title,
        "content": format_cycle_result(result) + "\n\n---\n\n" + report_content.strip(),
    }
    target = directory / f"{event_id}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return {"status": "queued_for_chat", "event_id": event_id}


def publish_skill_report(
    result: Mapping[str, Any],
    *,
    title: str,
    report_content: str,
    inbox_dir: Optional[Path] = None,
) -> Dict[str, Any]:
    """Publish a generic completed Skill execution without cycle-specific formatting."""
    directory = inbox_dir or INBOX_DIR
    directory.mkdir(parents=True, exist_ok=True)
    event_id = uuid.uuid4().hex
    payload = {
        "event_id": event_id,
        "type": "skill_execution_report",
        "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "skill": result.get("skill"),
        "execution_id": result.get("execution_id"),
        "status": result.get("status"),
        "title": title,
        "content": report_content.strip(),
    }
    target = directory / f"{event_id}.json"
    temporary = target.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(target)
    return {"status": "queued_for_chat", "event_id": event_id}


def pending_events(inbox_dir: Optional[Path] = None) -> list[Dict[str, Any]]:
    directory = inbox_dir or INBOX_DIR
    result = []
    for path in sorted(directory.glob("*.json"), key=lambda item: item.stat().st_mtime) if directory.exists() else []:
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["_path"] = str(path)
            result.append(payload)
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return result


def acknowledge_event(event_id: str, inbox_dir: Optional[Path] = None) -> bool:
    """Remove one queued report after a live chat client received its result."""
    normalized = str(event_id or "").strip()
    if not normalized or any(character not in "0123456789abcdef" for character in normalized.lower()):
        return False
    path = (inbox_dir or INBOX_DIR) / f"{normalized}.json"
    try:
        existed = path.is_file()
        path.unlink(missing_ok=True)
        return existed
    except OSError:
        return False
