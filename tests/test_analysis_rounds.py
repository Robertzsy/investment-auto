"""Engine tests for the async fixed-workflow analysis round service."""
from __future__ import annotations

import threading
import time

import pytest

from engine import analysis_rounds, analysis_runs
from engine.runtime_lock import AtomicClaim


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(analysis_runs, "runtime_dir", lambda: tmp_path / "runtime")
    monkeypatch.setattr(analysis_rounds, "STALE_SECONDS", 60)
    spawned = []
    def fake_start(cycle_id, market, symbols, symbols_source, label, claim):
        spawned.append((cycle_id, market, symbols, symbols_source, label))
        claim.release()
    monkeypatch.setattr(analysis_rounds, "_start_claimed_worker", fake_start)
    # Drain any worker threads left by previous tests before touching _active.
    with analysis_rounds._lock:
        analysis_rounds._active.clear()
    return {"spawned": spawned}


def _payload(**overrides):
    payload = {
        "cycle_id": "20260822-cn-user-0000001",
        "market": "cn",
        "symbols": ["688981"],
        "symbols_source": "user",
        "label": "user-analysis",
    }
    payload.update(overrides)
    return payload


def test_start_creates_run_and_spawns_one_worker(isolate):
    result = analysis_rounds.start(_payload())
    assert result["started"] is True
    assert result["analysis"]["cycle_id"] == "20260822-cn-user-0000001"
    assert result["analysis"]["symbols"] == ["688981"]
    assert result["analysis"]["symbols_source"] == "user"
    assert len(isolate["spawned"]) == 1


def test_duplicate_cycle_id_never_starts_a_second_round(isolate):
    analysis_rounds.start(_payload())
    # Simulate the worker having recorded a checkpoint (keeps the run fresh,
    # so it is NOT stale) and a retry arriving.
    analysis_runs.update({"cycle_id": "20260822-cn-user-0000001", "stage": "base_research", "event": "checkpoint", "result": {"ok": True}})
    result = analysis_rounds.start(_payload())
    assert result["started"] is False
    assert result["duplicate"] is True
    assert len(isolate["spawned"]) == 1


def test_completed_run_is_replayed_not_restarted(isolate):
    analysis_rounds.start(_payload())
    analysis_runs.finish({"cycle_id": "20260822-cn-user-0000001", "decisions": [], "execution": {"fills": []}})
    result = analysis_rounds.start(_payload())
    assert result["started"] is False
    assert result["duplicate"] is True
    assert result["analysis"]["status"] == "completed"
    assert len(isolate["spawned"]) == 1


def test_ready_for_execution_run_is_replayed_not_restarted(isolate):
    analysis_rounds.start(_payload())
    analysis_runs.update({
        "cycle_id": "20260822-cn-user-0000001",
        "stage": "final_decision",
        "event": "execution_ready",
        "decisions": [{"symbol": "688981", "action": "BUY", "target_weight": 0.05, "confidence": 0.9}],
    })
    result = analysis_rounds.start(_payload())
    assert result["started"] is False
    assert result["duplicate"] is True
    assert result["analysis"]["status"] == "ready_for_execution"
    assert len(isolate["spawned"]) == 1


def test_failed_run_retries_with_same_cycle_id(isolate):
    analysis_rounds.start(_payload())
    analysis_runs.finish({"cycle_id": "20260822-cn-user-0000001", "error": "research failed"}, failed=True)
    result = analysis_rounds.start(_payload())
    assert result["started"] is True
    assert result["resumed"] is True
    # Checkpoints survive: the reopened run is running with its history intact.
    assert analysis_runs.get("20260822-cn-user-0000001")["status"] == "running"
    assert len(isolate["spawned"]) == 2


def test_failed_retry_preserves_error_when_previous_owner_holds_lease(isolate):
    analysis_rounds.start(_payload())
    analysis_runs.finish({"cycle_id": "20260822-cn-user-0000001", "error": "specific workflow error"}, failed=True)
    claim = AtomicClaim(analysis_rounds._lease_path("20260822-cn-user-0000001"), stale_seconds=60)
    assert claim.acquire() is True
    try:
        result = analysis_rounds.start(_payload())
        assert result["started"] is False
        assert result["retry_deferred"] is True
        run = analysis_runs.get("20260822-cn-user-0000001")
        assert run["status"] == "failed"
        assert run["error"] == "specific workflow error"
        assert len(isolate["spawned"]) == 1
    finally:
        claim.release()


def test_startup_recovery_resumes_orphaned_running_rounds(isolate):
    # Simulate a crashed worker: create a running run, backdate it, and make
    # sure no live worker is registered (fresh process).
    analysis_runs.start_or_resume({
        "cycle_id": "orphan-1",
        "market": "cn",
        "symbols": ["688981"],
        "symbols_source": "user",
    })
    run = analysis_runs.get("orphan-1")
    run["updated_at"] = "2020-01-01T00:00:00+00:00"
    analysis_runs._write(analysis_runs._path("orphan-1"), run)
    with analysis_rounds._lock:
        analysis_rounds._active.clear()

    analysis_rounds.recover_on_startup()
    assert len(isolate["spawned"]) == 1
    assert isolate["spawned"][0][0] == "orphan-1"


def test_startup_recovery_skips_terminal_and_fresh_runs(isolate):
    # terminal: ready_for_execution
    analysis_runs.start_or_resume({"cycle_id": "ready-1", "market": "cn", "symbols": ["688981"], "symbols_source": "user"})
    analysis_runs.update({"cycle_id": "ready-1", "stage": "final_decision", "event": "execution_ready", "decisions": []})
    # failed stays failed until an explicit retry
    analysis_runs.start_or_resume({"cycle_id": "failed-1", "market": "cn"})
    analysis_runs.finish({"cycle_id": "failed-1", "error": "boom"}, failed=True)
    # fresh running (recently updated) must NOT be resumed
    analysis_runs.start_or_resume({"cycle_id": "fresh-1", "market": "cn"})

    analysis_rounds.recover_on_startup()
    assert len(isolate["spawned"]) == 0


def test_stale_running_run_resumes_from_checkpoints(isolate):
    analysis_rounds.start(_payload())
    # The worker "died": no active thread, and the run record goes stale.
    with analysis_rounds._lock:
        analysis_rounds._active.clear()
    run = analysis_runs.get("20260822-cn-user-0000001")
    run["updated_at"] = "2020-01-01T00:00:00+00:00"
    analysis_runs._write(analysis_runs._path("20260822-cn-user-0000001"), run)
    assert analysis_runs.stale_after(run, seconds=60) is True

    result = analysis_rounds.start(_payload())
    assert result["started"] is True
    assert result["resumed"] is True
    assert len(isolate["spawned"]) == 2


def test_active_worker_blocks_restart_even_when_stale(isolate, monkeypatch):
    # A worker that stays alive while the run record is backdated stale: the
    # live in-process thread must block a restart even though the file looks
    # orphaned.
    monkeypatch.setattr(analysis_rounds, "_worker", lambda *args: time.sleep(3))
    analysis_runs.start_or_resume({"cycle_id": "live-1", "market": "cn"})
    with analysis_rounds._lock:
        thread = threading.Thread(target=analysis_rounds._worker, args=("live-1", "cn", [], "user", "x"), daemon=True)
        analysis_rounds._active["live-1"] = thread
        thread.start()
    stale_live = analysis_runs.get("live-1")
    stale_live["updated_at"] = "2020-01-01T00:00:00+00:00"
    analysis_runs._write(analysis_runs._path("live-1"), stale_live)

    result = analysis_rounds.start(_payload(cycle_id="live-1"))
    assert result["started"] is False
    assert result["running"] is True
    # Clean up the live worker thread before the test ends.
    with analysis_rounds._lock:
        live = analysis_rounds._active.pop("live-1", None)
    if live is not None:
        live.join(timeout=10)


def test_rejects_bad_symbols_and_unsafe_ids():
    with pytest.raises(ValueError, match="symbols 必须是数组"):
        analysis_rounds.start(_payload(symbols="688981"))
    with pytest.raises(ValueError, match="cycle_id"):
        analysis_rounds.start(_payload(cycle_id="../escape"))


def test_web_submissions_are_forced_to_analysis_only(isolate):
    # The HTTP layer passes submit=False before start(); the service itself
    # must never allow a web caller to flip it back on.
    analysis_runs.start_or_resume(
        {"cycle_id": "forced-0", "market": "cn", "submit": analysis_rounds._submit_allowed()}
    )
    run = analysis_runs.get("forced-0")
    assert run["submit"] is False


def test_http_round_endpoint_forces_analysis_only(monkeypatch):
    import json
    import urllib.request

    from engine.api import server as server_module

    captured = {}

    def fake_start(payload):
        captured["payload"] = dict(payload)
        return {"ok": True, "started": True, "analysis": {"cycle_id": payload["cycle_id"], "status": "running"}}

    monkeypatch.setattr("engine.analysis_rounds.start", fake_start)
    httpd = server_module.ThreadingHTTPServer(("127.0.0.1", 0), server_module._Handler)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{httpd.server_address[1]}"
    try:
        req = urllib.request.Request(
            base + "/api/analysis/rounds/start",
            data=json.dumps({
                "cycle_id": "20260822-cn-user-0000001",
                "market": "cn",
                "symbols": ["688981"],
                "symbols_source": "user",
                "submit": True,
            }).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode())
        assert payload["ok"] is True
        assert captured["payload"]["submit"] is False
        assert captured["payload"]["symbols_source"] == "user"
    finally:
        httpd.shutdown()
        httpd.server_close()
