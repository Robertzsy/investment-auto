from __future__ import annotations

import json
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional
from zoneinfo import ZoneInfo

from engine.config import cfg
from engine.investment.contracts import CommandEnvelope, InvestmentCommand, normalize_market
from engine.investment.mandate import get_mandate, set_mandate
from engine.investment.reflection import InvestmentReflectionService


ROOT = Path(__file__).resolve().parents[2]
from engine.paths import runtime_dir
COMMAND_DIR = runtime_dir() / "investment" / "commands"
_mode_lock = threading.RLock()


def _now() -> str:
    return datetime.now(ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai"))).isoformat(timespec="seconds")


def _write_command(command: CommandEnvelope, response: Optional[Mapping[str, Any]] = None) -> None:
    COMMAND_DIR.mkdir(parents=True, exist_ok=True)
    path = COMMAND_DIR / f"{command.created_at[:10]}-{command.command_id}.json"
    payload = command.to_dict()
    if response is not None:
        payload["response"] = dict(response)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


def _save_operation_mode(mode: str) -> Dict[str, Any]:
    normalized = str(mode).strip().lower()
    if normalized not in {"manual", "automatic"}:
        raise ValueError("mode 必须是 manual 或 automatic")
    import yaml

    config_path = Path(cfg._path)  # one authoritative configured file
    with _mode_lock:
        config_data = yaml.safe_load(config_path.read_text(encoding="utf-8")) or {}
        autonomous = config_data.setdefault("autonomous", {})
        autonomous.update({"operation_mode": normalized, "enabled": True, "auto_execute": True})
        temporary = config_path.with_suffix(config_path.suffix + ".tmp")
        temporary.write_text(yaml.safe_dump(config_data, allow_unicode=True, sort_keys=False), encoding="utf-8")
        temporary.replace(config_path)
        cfg.reload()
    return {"mode": normalized, "enabled": True, "auto_execute": True}


class InvestmentAgentService:
    """The only application service allowed to operate the investment Agent."""

    def __init__(self, reflection: Optional[InvestmentReflectionService] = None) -> None:
        self.reflection = reflection or InvestmentReflectionService()

    def issue(
        self,
        command: InvestmentCommand | str,
        payload: Optional[Mapping[str, Any]] = None,
        *,
        requested_by: str = "manager",
        progress_callback: Optional[Callable[[str], None]] = None,
    ) -> Dict[str, Any]:
        envelope = CommandEnvelope(
            command_id=uuid.uuid4().hex,
            command=command if isinstance(command, InvestmentCommand) else InvestmentCommand(str(command)),
            payload=dict(payload or {}),
            requested_by=str(requested_by)[:80],
            created_at=_now(),
        )
        _write_command(envelope)
        try:
            response = self.execute_envelope(envelope, progress_callback=progress_callback, write_audit=False)
        except Exception as exc:
            response = {"ok": False, "status": "error", "error": str(exc), "command_id": envelope.command_id}
            _write_command(envelope, response)
            raise
        response.setdefault("command_id", envelope.command_id)
        response.setdefault("command", envelope.command.value)
        _write_command(envelope, response)
        return response

    def execute_envelope(
        self,
        envelope: CommandEnvelope,
        *,
        progress_callback: Optional[Callable[[str], None]] = None,
        write_audit: bool = True,
    ) -> Dict[str, Any]:
        if write_audit:
            _write_command(envelope)
        try:
            response = self._execute(envelope, progress_callback=progress_callback)
        except Exception as exc:
            response = {"ok": False, "status": "error", "error": str(exc), "command_id": envelope.command_id}
            if write_audit:
                _write_command(envelope, response)
            raise
        response.setdefault("command_id", envelope.command_id)
        response.setdefault("command", envelope.command.value)
        if write_audit:
            _write_command(envelope, response)
        return response

    def _execute(
        self,
        command: CommandEnvelope,
        *,
        progress_callback: Optional[Callable[[str], None]],
    ) -> Dict[str, Any]:
        from engine.trading import control

        if command.command == InvestmentCommand.RUN_CYCLE:
            market = normalize_market(command.payload.get("market"))
            label = str(command.payload.get("label", "manager"))[:40]
            from engine.scheduler import run_investment_cycle

            result = run_investment_cycle(market, label=label, progress_callback=progress_callback)
            return {"ok": True, **result, "mandate": get_mandate()}
        if command.command == InvestmentCommand.RUN_SCHEDULED_CYCLE:
            market = normalize_market(command.payload.get("market"))
            cycle_type = str(command.payload.get("cycle_type", "intraday")).strip().lower()
            if cycle_type not in {"intraday", "close"}:
                raise ValueError("cycle_type 必须是 intraday 或 close")
            from engine.scheduler import run_scheduled_cycle

            result = run_scheduled_cycle(
                market,
                cycle_type=cycle_type,
                label=str(command.payload.get("label", "scheduled-skill"))[:40],
                time_str=str(command.payload.get("time", ""))[:10],
                catch_up=bool(command.payload.get("catch_up", False)),
                scheduled_at=str(command.payload.get("scheduled_at", "")),
                progress_callback=progress_callback,
            )
            return {"ok": True, **result, "mandate": get_mandate()}
        if command.command == InvestmentCommand.PAUSE:
            state = control.set_paused(True, reason=str(command.payload.get("reason", "管理 AI 暂停"))[:500], updated_by=command.requested_by)
            return {"ok": True, "control": state}
        if command.command == InvestmentCommand.RESUME:
            state = control.set_paused(False, reason=str(command.payload.get("reason", "管理 AI 恢复"))[:500], updated_by=command.requested_by)
            return {"ok": True, "control": state}
        if command.command == InvestmentCommand.KILL:
            state = control.activate_kill_switch(
                reason=str(command.payload.get("reason", "管理 AI 紧急停止"))[:500],
                updated_by=command.requested_by,
            )
            return {"ok": True, "control": state}
        if command.command == InvestmentCommand.RESET_KILL:
            state = control.reset_kill_switch(reason=str(command.payload.get("reason", "管理 AI 解除紧急停止"))[:500], updated_by=command.requested_by)
            return {"ok": True, "control": state}
        if command.command == InvestmentCommand.SET_MODE:
            return {"ok": True, **_save_operation_mode(str(command.payload.get("mode", "")))}
        if command.command == InvestmentCommand.SET_STRATEGY:
            mandate = set_mandate(str(command.payload.get("profile", "")), selected_by=command.requested_by)
            return {"ok": True, "mandate": mandate}
        if command.command == InvestmentCommand.RUN_SCREENING:
            market = normalize_market(command.payload.get("market"))
            from engine.screening.preview import run_screening_preview

            return {"ok": True, "market": market, "screening": run_screening_preview(market)}
        if command.command == InvestmentCommand.SUBMIT_DECISIONS:
            market = normalize_market(command.payload.get("market"))
            from engine.trading.decision_execution import submit_decisions

            result = submit_decisions(
                market,
                command.payload.get("decisions"),
                label=str(command.payload.get("label", "dsh-manual")),
                note=str(command.payload.get("note", "")),
                requested_by=command.requested_by,
                idempotency_key=str(command.payload.get("idempotency_key", "")),
                progress_callback=progress_callback,
            )
            return {"ok": True, **result}
        if command.command == InvestmentCommand.RUN_OPTIMIZER:
            market = normalize_market(command.payload.get("market"))
            from engine.optimizer.runner import compact_result, parse_symbols, run_optimizer

            symbols = command.payload.get("symbols")
            parsed = parse_symbols(symbols) if isinstance(symbols, str) else symbols
            return {"ok": True, "market": market, "optimizer": compact_result(run_optimizer(market=market, symbols=parsed))}
        if command.command == InvestmentCommand.REFLECT:
            market = str(command.payload.get("market", "")).lower()
            return {"ok": True, "reflections": self.reflection.recent(market, int(command.payload.get("limit", 5)))}
        if command.command == InvestmentCommand.RESET_PAPER_ACCOUNT:
            if str(cfg.trading.get("mode", "paper")).strip().lower() != "paper":
                raise RuntimeError("账户重置只允许 trading.mode=paper；实盘账户不会被修改")
            market = normalize_market(command.payload.get("market"))
            from engine.portfolio import account as account_store

            reset = account_store.reset_market(
                market,
                backup_dir=runtime_dir() / "backups" / "portfolio",
            )
            return {
                "ok": True,
                "status": "reset",
                "reason": str(command.payload.get("reason", "用户要求重置模拟账户"))[:500],
                **reset,
            }
        if command.command == InvestmentCommand.STATUS:
            from engine.investment.status import runtime_status

            return {"ok": True, **runtime_status()}
        raise ValueError(f"不支持的投资命令: {command.command}")
