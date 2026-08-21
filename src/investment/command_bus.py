from __future__ import annotations

import json
import logging
import os
import threading
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional
from zoneinfo import ZoneInfo

from src.config import cfg
from src.investment.contracts import CommandEnvelope, InvestmentCommand


ROOT = Path(__file__).resolve().parents[2]
from src.paths import runtime_dir
BUS_DIR = runtime_dir() / "investment" / "bus"
INBOX = BUS_DIR / "inbox"
OUTBOX = BUS_DIR / "outbox"
PROGRESS = BUS_DIR / "progress"
ACTIVE = BUS_DIR / "active"
HEARTBEAT = BUS_DIR / "worker.json"
logger = logging.getLogger(__name__)


def _now() -> str:
    return datetime.now(ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai"))).isoformat(timespec="seconds")


def _atomic_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(dict(value), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


class InvestmentAgentClient:
    """Management-plane client. Queue transport keeps investment work out of chat."""

    def issue(
        self,
        command: InvestmentCommand | str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        requested_by: str = "manager",
        progress_callback: Optional[Callable[[str], None]] = None,
        timeout: Optional[float] = None,
    ) -> Dict[str, Any]:
        transport = os.getenv("INVESTMENT_AGENT_TRANSPORT", "queue").strip().lower()
        if transport != "queue":
            from src.investment.service import InvestmentAgentService

            return InvestmentAgentService().issue(
                command, payload, requested_by=requested_by, progress_callback=progress_callback,
            )
        normalized = command if isinstance(command, InvestmentCommand) else InvestmentCommand(str(command))
        envelope = CommandEnvelope(
            command_id=uuid.uuid4().hex,
            command=normalized,
            payload=dict(payload or {}),
            requested_by=str(requested_by)[:80],
            created_at=_now(),
        )
        if not self.worker_alive() and normalized == InvestmentCommand.STATUS:
            # The management plane must still be able to explain that the
            # worker is offline.  This is a read-only shared-state snapshot;
            # every mutating or analytical command continues to require the
            # standalone investment process.
            from src.investment.status import runtime_status

            return {
                "ok": True,
                "status": "offline",
                "command": normalized.value,
                **runtime_status(),
            }
        if not self.worker_alive():
            raise RuntimeError("投资 Agent 独立进程未运行，请启动 python -m src.main run")
        _atomic_json(INBOX / f"{envelope.command_id}.json", envelope.to_dict())
        if timeout is None:
            configured = cfg.autonomous.get(
                "command_hard_timeout_seconds",
                1800
                if normalized in {InvestmentCommand.RUN_CYCLE, InvestmentCommand.RUN_SCHEDULED_CYCLE}
                else 480,
            )
            hard_timeout = max(1.0, float(configured))
        else:
            hard_timeout = max(1.0, float(timeout))
        idle_timeout = max(0.1, float(cfg.autonomous.get("command_idle_timeout_seconds", 300)))
        deadline = time.monotonic() + hard_timeout
        last_activity = time.monotonic()
        active_token = ""
        delivered_progress = 0
        response_path = OUTBOX / f"{envelope.command_id}.json"
        progress_path = PROGRESS / f"{envelope.command_id}.json"
        active_path = ACTIVE / f"{envelope.command_id}.json"
        while time.monotonic() < deadline:
            if progress_path.exists():
                try:
                    values = json.loads(progress_path.read_text(encoding="utf-8"))
                    if isinstance(values, list) and len(values) > delivered_progress:
                        if progress_callback:
                            for value in values[delivered_progress:]:
                                progress_callback(str(value))
                        delivered_progress = len(values)
                        last_activity = time.monotonic()
                except (OSError, json.JSONDecodeError, TypeError):
                    pass
            if active_path.exists():
                try:
                    active = json.loads(active_path.read_text(encoding="utf-8"))
                    token = (
                        f"{active.get('updated_at', '')}:{active.get('progress_seq', '')}:"
                        f"{active.get('heartbeat_seq', '')}"
                    )
                    if token and token != active_token:
                        active_token = token
                        last_activity = time.monotonic()
                except (OSError, json.JSONDecodeError, TypeError, AttributeError):
                    pass
            if response_path.exists():
                result = json.loads(response_path.read_text(encoding="utf-8"))
                try:
                    response_path.unlink()
                    if progress_path.exists():
                        progress_path.unlink()
                except OSError:
                    pass
                if not result.get("ok", False) and result.get("error"):
                    raise RuntimeError(str(result["error"]))
                delivery = result.get("chat_delivery")
                if isinstance(delivery, Mapping) and delivery.get("event_id"):
                    from src.manager.report_inbox import acknowledge_event

                    acknowledge_event(str(delivery["event_id"]))
                return result
            if time.monotonic() - last_activity >= idle_timeout:
                raise TimeoutError(
                    f"投资 Agent 命令无有效进度或任务心跳，已等待 {int(idle_timeout)} 秒: {normalized.value}"
                )
            time.sleep(0.1)
        raise TimeoutError(f"投资 Agent 命令达到 {int(hard_timeout)} 秒硬上限: {normalized.value}")

    @staticmethod
    def worker_alive(max_age_seconds: float = 5.0) -> bool:
        try:
            payload = json.loads(HEARTBEAT.read_text(encoding="utf-8"))
            stamp = datetime.fromisoformat(str(payload["updated_at"]))
            now = datetime.now(stamp.tzinfo) if stamp.tzinfo else datetime.now()
            return (now - stamp).total_seconds() <= max_age_seconds
        except (OSError, ValueError, KeyError, json.JSONDecodeError):
            return False


class InvestmentCommandWorker:
    def __init__(self, poll_seconds: float = 0.2) -> None:
        self.poll_seconds = max(0.05, poll_seconds)
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self.heartbeat_thread: Optional[threading.Thread] = None

    def start(self) -> "InvestmentCommandWorker":
        if self.thread and self.thread.is_alive():
            return self
        self.stop_event.clear()
        self.heartbeat_thread = threading.Thread(
            target=self._heartbeat_loop,
            name="investment-command-heartbeat",
            daemon=True,
        )
        self.heartbeat_thread.start()
        self.thread = threading.Thread(target=self.run, name="investment-command-worker", daemon=True)
        self.thread.start()
        return self

    def stop(self) -> None:
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=2)
        if self.heartbeat_thread:
            self.heartbeat_thread.join(timeout=2)

    def _heartbeat_loop(self) -> None:
        while not self.stop_event.is_set():
            try:
                _atomic_json(HEARTBEAT, {"pid": os.getpid(), "updated_at": _now(), "status": "running"})
            except Exception:
                logger.exception("Investment Agent heartbeat write failed")
            self.stop_event.wait(1.0)

    def run(self) -> None:
        from src.investment.service import InvestmentAgentService

        for path in (INBOX, OUTBOX, PROGRESS, ACTIVE):
            path.mkdir(parents=True, exist_ok=True)
        service = InvestmentAgentService()
        while not self.stop_event.is_set():
            try:
                jobs = sorted(INBOX.glob("*.json"), key=lambda item: item.stat().st_mtime)
                if not jobs:
                    self.stop_event.wait(self.poll_seconds)
                    continue
                source = jobs[0]
                processing = source.with_suffix(".processing")
                try:
                    source.replace(processing)
                except OSError:
                    continue
                command_id = processing.stem
                progress_path = PROGRESS / f"{command_id}.json"
                active_path = ACTIVE / f"{command_id}.json"
                progress_values: list[str] = []
                activity_lock = threading.Lock()
                activity_stop = threading.Event()
                activity_state: Dict[str, Any] = {
                    "command_id": command_id,
                    "status": "running",
                    "progress_seq": 0,
                    "heartbeat_seq": 0,
                    "last_progress": "",
                }

                def write_activity() -> None:
                    with activity_lock:
                        activity_state["heartbeat_seq"] = int(activity_state["heartbeat_seq"]) + 1
                        activity_state["updated_at"] = _now()
                        _atomic_json(active_path, activity_state)

                def activity_loop() -> None:
                    interval = max(0.05, float(cfg.autonomous.get("command_heartbeat_seconds", 10)))
                    while not activity_stop.wait(interval):
                        try:
                            write_activity()
                        except Exception:
                            logger.exception("Investment command activity heartbeat failed")

                def progress(value: str) -> None:
                    progress_values.append(str(value)[:1000])
                    progress_path.write_text(json.dumps(progress_values, ensure_ascii=False), encoding="utf-8")
                    with activity_lock:
                        activity_state["progress_seq"] = len(progress_values)
                        activity_state["heartbeat_seq"] = int(activity_state["heartbeat_seq"]) + 1
                        activity_state["last_progress"] = progress_values[-1]
                        activity_state["updated_at"] = _now()
                        _atomic_json(active_path, activity_state)

                write_activity()
                activity_thread = threading.Thread(
                    target=activity_loop,
                    name=f"investment-command-activity-{command_id[:8]}",
                    daemon=True,
                )
                activity_thread.start()
                try:
                    envelope = CommandEnvelope.from_mapping(json.loads(processing.read_text(encoding="utf-8")))
                    result = service.execute_envelope(envelope, progress_callback=progress)
                except Exception as exc:
                    result = {"ok": False, "status": "error", "error": str(exc), "command_id": command_id}
                finally:
                    activity_stop.set()
                    activity_thread.join(timeout=1)
                _atomic_json(OUTBOX / f"{command_id}.json", result)
                try:
                    active_path.unlink(missing_ok=True)
                except OSError:
                    pass
                try:
                    processing.unlink()
                except OSError:
                    pass
            except Exception:
                logger.exception("Investment command worker recovered from an unexpected loop error")
                self.stop_event.wait(self.poll_seconds)
