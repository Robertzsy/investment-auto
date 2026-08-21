from __future__ import annotations

import time

import pytest

from engine.investment import command_bus


@pytest.fixture
def isolated_bus(monkeypatch, tmp_path):
    bus = tmp_path / "bus"
    monkeypatch.setattr(command_bus, "BUS_DIR", bus)
    monkeypatch.setattr(command_bus, "INBOX", bus / "inbox")
    monkeypatch.setattr(command_bus, "OUTBOX", bus / "outbox")
    monkeypatch.setattr(command_bus, "PROGRESS", bus / "progress")
    monkeypatch.setattr(command_bus, "ACTIVE", bus / "active")
    monkeypatch.setattr(command_bus, "HEARTBEAT", bus / "worker.json")
    monkeypatch.setenv("INVESTMENT_AGENT_TRANSPORT", "queue")
    monkeypatch.setattr(
        command_bus.InvestmentAgentClient,
        "worker_alive",
        staticmethod(lambda max_age_seconds=5: True),
    )
    monkeypatch.setitem(command_bus.cfg.raw.setdefault("autonomous", {}), "command_idle_timeout_seconds", 0.12)
    monkeypatch.setitem(command_bus.cfg.raw.setdefault("autonomous", {}), "command_heartbeat_seconds", 0.05)
    return bus


def test_running_command_heartbeat_prevents_idle_timeout(monkeypatch, isolated_bus):
    def fake_execute(self, envelope, *, progress_callback=None, write_audit=True):
        assert progress_callback is not None
        progress_callback("研究开始")
        time.sleep(0.45)  # Longer than the configured 0.12s idle window.
        return {
            "ok": True,
            "status": "generated",
            "market": "cn",
        }

    monkeypatch.setattr(
        "engine.investment.service.InvestmentAgentService.execute_envelope",
        fake_execute,
    )
    worker = command_bus.InvestmentCommandWorker(poll_seconds=0.01).start()
    updates = []
    try:
        result = command_bus.InvestmentAgentClient().issue(
            "run_cycle",
            {"market": "cn"},
            timeout=1.5,
            progress_callback=updates.append,
        )
    finally:
        worker.stop()

    assert result["status"] == "generated"
    assert updates == ["研究开始"]


def test_command_without_progress_or_activity_hits_idle_timeout(isolated_bus):
    started = time.monotonic()
    with pytest.raises(TimeoutError, match="无有效进度或任务心跳"):
        command_bus.InvestmentAgentClient().issue("run_cycle", {"market": "cn"}, timeout=1.0)
    assert time.monotonic() - started < 0.8
