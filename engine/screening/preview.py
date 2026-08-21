"""Screening preview helper: the command surface behind ``RUN_SCREENING``.

Investment Auto 2.0 keeps the deterministic screening engine and exposes it
as one small, dependency-light entry point used by the CLI, the HTTP command
API, and the DSH tools bridge.
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from engine.config import cfg


def _snapshot_loader(symbols: Sequence[str], limit: int) -> tuple[Dict[str, Any], Dict[str, str]]:
    from engine.data import fetcher

    snapshots: Dict[str, Any] = {}
    errors: Dict[str, str] = {}
    for symbol in symbols[: max(0, limit)]:
        try:
            snapshots[str(symbol)] = fetcher.snapshot(str(symbol))
        except Exception as exc:  # noqa: BLE001 - one failing symbol must not kill the run
            errors[str(symbol)] = str(exc)[:200]
    return snapshots, errors


def run_screening_preview(
    market: str,
    *,
    symbols: Optional[Sequence[str]] = None,
    now: Optional[datetime] = None,
) -> Dict[str, Any]:
    """Run a screening pass and return the dashboard-ready preview."""
    from engine.portfolio import account as account_store
    from engine.screening.engine import run_screening

    normalized = str(market or "").strip().lower()
    if normalized not in {"cn", "hk", "us", "etf"}:
        raise ValueError("market 必须是 cn、hk、us 或 etf")
    if now is None:
        now = datetime.now(ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai")))
    account_data = account_store.account(normalized)
    held = [str(holding.get("code", "")) for holding in account_data.get("holdings", []) if holding.get("code")]
    configured = list(symbols or [])
    fallback = list((cfg.optimizer.get("default_symbols") or {}).get(normalized, []))
    outcome = run_screening(
        normalized,
        held_symbols=held,
        configured_symbols=configured,
        fallback_symbols=fallback,
        settings=dict(cfg.screening),
        autonomous_config=dict(cfg.autonomous),
        snapshot_loader=_snapshot_loader,
        now=now,
    )
    return {
        "market": normalized,
        "selected_symbols": outcome.symbols,
        "snapshots": outcome.snapshots,
        "market_data_errors": outcome.market_data_errors,
        "audit": outcome.audit,
        "generated_at": now.isoformat(timespec="seconds"),
    }
