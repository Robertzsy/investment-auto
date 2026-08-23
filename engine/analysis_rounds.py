"""Async fixed-workflow analysis rounds (web-triggered).

The web session's ``investment_analysis_workflow`` tool is a thin launcher: it
posts here and returns immediately with the durable run state.  This module
owns the idempotent start (one stable ``cycle_id`` = at most one round), the
worker threads that execute the round, the resume path for crashed workers,
and the startup recovery scan that re-arms rounds orphaned by a process
restart.

The worker runs the SAME headless DSH round the scheduler uses
(``engine.dsh_bridge``), so web-triggered analysis and autonomous rounds share
one fixed workflow implementation, and every checkpoint lands in
``runtime/analysis_runs`` for the Dashboard and the poller tool.  No HTTP
request — neither the browser's nor the tool's engine call — stays open for
the duration of the analysis.

Concurrency: beside the in-process active registry, each cycle holds a
filesystem lease (``runtime/analysis_runs/<cycle>.lease``, O_EXCL +
staleness via ``engine.runtime_lock.atomic_claim``) so two ENGINE PROCESSES
can never run the same round — the previous ``threading.RLock``-only design
did not provide that.
"""
from __future__ import annotations

import logging
import re
import threading
import time
from typing import Any, Dict, Mapping, Optional

from engine import analysis_runs
from engine.runtime_lock import AtomicClaim

logger = logging.getLogger("investment-auto.analysis-rounds")

_MARKETS = {"cn", "hk", "us", "etf"}
_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$")
_lock = threading.RLock()
_active: Dict[str, threading.Thread] = {}
# Short-TTL content-fingerprint map, used ONLY when the launcher had no
# platform request identity (transport-failure fallback): an immediate retry
# of the same content maps to the same cycle, a later request does not.
_ttl_fingerprints: Dict[str, tuple[str, float]] = {}
TTL_SECONDS = 300

# A running run with no checkpoint for this long and no live in-process
# worker is treated as orphaned; a retry with the same cycle_id resumes it
# from the last durable checkpoint instead of starting a second round.
STALE_SECONDS = 1800
# Startup recovery treats anything older than this as orphaned (a process
# restart killed every worker, so there is nothing else alive to wait for).
STARTUP_STALE_SECONDS = 300
LEASE_STALE_SECONDS = 3600
MAX_SYMBOLS = 40


def _lease_path(cycle_id: str) -> Any:
    from engine.paths import runtime_dir

    return runtime_dir() / "analysis_runs" / f"{_cycle_id(cycle_id)}.lease"


def _cycle_id(value: Any) -> str:
    cycle_id = str(value or "").strip()
    if not _ID_RE.fullmatch(cycle_id):
        raise ValueError("cycle_id 必须是 1-120 位字母、数字、点、下划线或短横线")
    return cycle_id


def _market(value: Any) -> str:
    market = str(value or "").strip().lower()
    if market not in _MARKETS:
        raise ValueError("market 必须是 cn、hk、us 或 etf")
    return market


def _symbols(value: Any) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("symbols 必须是数组")
    normalized: list[str] = []
    for item in value[:MAX_SYMBOLS]:
        symbol = str(item or "").strip()
        if not symbol:
            raise ValueError("symbols 不能包含空值")
        if symbol not in normalized:
            normalized.append(symbol)
    return normalized


def _submit_allowed() -> bool:
    """Web-launched rounds are analysis-only: trading stays behind the user's
    explicit approval on a later submission call."""
    return False


def _worker(
    cycle_id: str,
    market: str,
    symbols: list[str],
    symbols_source: str,
    label: str,
    claim: Optional[AtomicClaim] = None,
) -> None:
    """One round attempt. The filesystem lease is the cross-process guard:
    exactly one engine process may run a given cycle at a time; a second
    claimant exits immediately and leaves the run to its owner."""
    owned_claim = claim or AtomicClaim(_lease_path(cycle_id), stale_seconds=LEASE_STALE_SECONDS)
    if claim is None and not owned_claim.acquire():
        logger.warning("[ANALYSIS-ROUND:%s] lease held elsewhere; skipping duplicate worker", cycle_id)
        with _lock:
            _active.pop(cycle_id, None)
        return
    try:
        from engine import dsh_bridge

        app_dir = dsh_bridge._resolve_app_dir()
        if app_dir is None:
            raise RuntimeError("DSH app dir 不可用，无法启动固定分析流程")
        runner = dsh_bridge.DshBridgeRunner(app_dir=app_dir)
        result = runner.run_analysis_round(
            market,
            symbols=symbols,
            symbols_source=symbols_source,
            label=label,
            submit=False,
            cycle_id=cycle_id,
        )
        run = analysis_runs.get(cycle_id)
        if run is not None and run.get("status") == "running":
            if result.get("status") == "generated":
                # The headless round normally posts /ready or /complete
                # itself; this arm covers a round that produced no terminal
                # post. A concrete workflow failure is already terminal and
                # therefore can no longer be overwritten by this fallback.
                analysis_runs.finish(
                    {"cycle_id": cycle_id, "error": "分析轮次未产生终端状态（headless 会话异常结束）"},
                    failed=True,
                )
            elif result.get("status") == "error":
                analysis_runs.finish(
                    {"cycle_id": cycle_id, "error": result.get("error", "analysis round failed")},
                    failed=True,
                )
    except Exception as exc:  # noqa: BLE001 - the run record carries the failure
        logger.exception("[ANALYSIS-ROUND:%s] worker failed", cycle_id)
        try:
            if analysis_runs.get(cycle_id) is not None:
                analysis_runs.finish({"cycle_id": cycle_id, "error": str(exc)[:2000]}, failed=True)
        except Exception:  # noqa: BLE001
            pass
    finally:
        owned_claim.release()
        with _lock:
            _active.pop(cycle_id, None)


def _start_claimed_worker(
    cycle_id: str,
    market: str,
    symbols: list[str],
    symbols_source: str,
    label: str,
    claim: AtomicClaim,
) -> None:
    with _lock:
        thread = threading.Thread(
            target=_worker,
            args=(cycle_id, market, symbols, symbols_source, label, claim),
            name=f"analysis-round-{cycle_id}",
            daemon=True,
        )
        _active[cycle_id] = thread
        try:
            thread.start()
        except Exception:
            _active.pop(cycle_id, None)
            claim.release()
            raise


def _spawn(cycle_id: str, market: str, symbols: list[str], symbols_source: str, label: str) -> bool:
    claim = AtomicClaim(_lease_path(cycle_id), stale_seconds=LEASE_STALE_SECONDS)
    if not claim.acquire():
        logger.warning("[ANALYSIS-ROUND:%s] lease held elsewhere; skipping duplicate worker", cycle_id)
        return False
    _start_claimed_worker(cycle_id, market, symbols, symbols_source, label, claim)
    return True


def start(payload: Mapping[str, Any]) -> Dict[str, Any]:
    """Idempotently start (or report) one fixed analysis round.

    The same ``cycle_id`` never starts a second round: a completed or
    execution-ready run is replayed, a failed run is retried safely (trading
    is independently idempotent), an active run returns its live state, and
    an orphaned running run (stale + no live worker) is resumed.
    """
    cycle_id = _cycle_id(payload.get("cycle_id"))
    market = _market(payload.get("market"))
    symbols = _symbols(payload.get("symbols"))
    symbols_source = str(payload.get("symbols_source") or "").strip().lower()
    if symbols_source not in {"user", "screening", "autonomous"}:
        symbols_source = "user" if symbols else "autonomous"
    label = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(payload.get("label") or "analysis")).strip("-")[:40] or "analysis"

    with _lock:
        # Transport-failure fallback only: when the launcher could not derive
        # a request identity, an immediate retry with the same content maps
        # to the same cycle id. New user requests never pass this arm because
        # identity-derived ids are unique per user message.
        dedupe_fingerprint = str(payload.get("dedupe_fingerprint") or "").strip()[:64]
        if dedupe_fingerprint:
            existing_cycle = _ttl_fingerprints.get(dedupe_fingerprint)
            if existing_cycle is not None:
                existing_id, stamped_at = existing_cycle
                if time.time() - stamped_at < TTL_SECONDS:
                    previous = analysis_runs.get(existing_id)
                    if previous is not None and previous.get("status") not in {"completed", "ready_for_execution", "failed"}:
                        return {"ok": True, "started": False, "duplicate": True, "running": True, "analysis": previous}
        run = analysis_runs.get(cycle_id)
        if run is not None:
            status = run.get("status")
            if status in {"completed", "ready_for_execution"}:
                return {"ok": True, "started": False, "duplicate": True, "analysis": run}
            if status == "failed":
                # Acquire the cross-process lease BEFORE reopening durable
                # state. If the original owner is still exiting, preserve its
                # concrete failure instead of changing failed -> running and
                # leaving no worker behind.
                claim = AtomicClaim(_lease_path(cycle_id), stale_seconds=LEASE_STALE_SECONDS)
                if not claim.acquire():
                    logger.info("[ANALYSIS-ROUND:%s] retry deferred; cycle lease is still owned", cycle_id)
                    return {
                        "ok": True,
                        "started": False,
                        "duplicate": True,
                        "retry_deferred": True,
                        "analysis": run,
                    }
                original_error = str(run.get("error") or "analysis workflow failed")
                try:
                    run = analysis_runs.start_or_resume(
                        {"cycle_id": cycle_id, "market": market},
                        retry_failed=True,
                    )
                    _start_claimed_worker(
                        cycle_id,
                        market,
                        list(run.get("symbols") or []),
                        str(run.get("symbols_source") or symbols_source),
                        str(run.get("label") or label),
                        claim,
                    )
                except Exception:
                    claim.release()
                    analysis_runs.finish({"cycle_id": cycle_id, "error": original_error}, failed=True)
                    raise
                logger.info("[ANALYSIS-ROUND:%s] retrying failed run", cycle_id)
                return {"ok": True, "started": True, "resumed": True, "analysis": analysis_runs.get(cycle_id) or run}
            active_thread = _active.get(cycle_id)
            if active_thread is not None and active_thread.is_alive():
                return {"ok": True, "started": False, "duplicate": True, "running": True, "analysis": run}
            if not analysis_runs.stale_after(run, seconds=STALE_SECONDS):
                return {"ok": True, "started": False, "duplicate": True, "running": True, "analysis": run}
            # Orphaned round (worker died without a terminal post): resume
            # from the last durable checkpoint.
            resumed = True
        else:
            run = analysis_runs.start_or_resume(
                {
                    "cycle_id": cycle_id,
                    "market": market,
                    "label": label,
                    "symbols": symbols,
                    "symbols_source": symbols_source,
                    "submit": _submit_allowed(),
                }
            )
            resumed = False
        if dedupe_fingerprint:
            _ttl_fingerprints[dedupe_fingerprint] = (cycle_id, time.time())
        spawned = _spawn(cycle_id, market, list(run.get("symbols") or []), str(run.get("symbols_source") or symbols_source), str(run.get("label") or label))
        if not spawned:
            return {"ok": True, "started": False, "duplicate": True, "running": True, "analysis": analysis_runs.get(cycle_id) or run}
    logger.info("[ANALYSIS-ROUND:%s] started (symbols=%s, source=%s, resumed=%s)", cycle_id, symbols or "(screening)", symbols_source, resumed)
    return {"ok": True, "started": True, "resumed": resumed, "analysis": analysis_runs.get(cycle_id) or run}


def recover_on_startup() -> None:
    """Re-arm rounds orphaned by a process restart (serve-process only).

    Called after the API server is already listening, so the resumed headless
    sessions can post their checkpoints back. Running runs older than the
    short startup threshold have no surviving worker (the previous process
    died) and are resumed from their durable checkpoints; failed runs stay
    failed until an explicit retry, and execution-ready/completed runs are
    left alone.
    """
    try:
        rows = analysis_runs.list_runs(limit=100)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[ANALYSIS-ROUNDS] startup recovery scan failed: %s", exc)
        return
    resumed = 0
    for run in rows:
        if run.get("status") != "running":
            continue
        if not analysis_runs.stale_after(run, seconds=STARTUP_STALE_SECONDS):
            continue
        cycle_id = str(run.get("cycle_id", ""))
        if not _ID_RE.fullmatch(cycle_id):
            continue
        with _lock:
            active_thread = _active.get(cycle_id)
        if active_thread is not None and active_thread.is_alive():
            continue
        logger.info("[ANALYSIS-ROUNDS] startup recovery: resuming %s", cycle_id)
        _spawn(
            cycle_id,
            str(run.get("market", "cn")),
            list(run.get("symbols") or []),
            str(run.get("symbols_source") or "autonomous"),
            str(run.get("label") or "analysis"),
        )
        resumed += 1
    if resumed:
        logger.info("[ANALYSIS-ROUNDS] startup recovery resumed %d round(s)", resumed)


def status(cycle_id: str) -> Optional[Dict[str, Any]]:
    return analysis_runs.get(_cycle_id(cycle_id))


def active_rounds() -> list[Dict[str, Any]]:
    with _lock:
        rows = []
        for cycle_id, thread in list(_active.items()):
            if thread.is_alive():
                run = analysis_runs.get(cycle_id)
                if run is not None:
                    rows.append(run)
                else:
                    rows.append({"cycle_id": cycle_id, "status": "running"})
        return rows
