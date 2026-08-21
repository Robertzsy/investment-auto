"""Engine tests for the DSH headless bridge runner (P3)."""
from __future__ import annotations

import json
import subprocess
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine import dsh_bridge

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(dsh_bridge, "AUDIT_DIR", tmp_path / "audit")
    (tmp_path / "audit").mkdir()
    return tmp_path


def _runner(tmp_path):
    return dsh_bridge.DshBridgeRunner(app_dir=tmp_path / "app")


def _context(**overrides):
    context = {
        "label": "auto-101500",
        "time_str": "10:15",
        "scheduled_at": NOW,
        "catch_up": False,
        "now": NOW,
        "macro_excerpt": "",
        "account": {},
        "progress_callback": None,
    }
    context.update(overrides)
    return context


def test_runner_spawns_headless_profile_with_task(monkeypatch, tmp_path):
    (tmp_path / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib").mkdir(parents=True)
    (tmp_path / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js").write_text("", encoding="utf-8")
    captured = {}

    def fake_run(command, **kwargs):
        captured["command"] = command
        captured["timeout"] = kwargs.get("timeout")
        captured["env"] = kwargs.get("env") or {}
        return subprocess.CompletedProcess(command, 0, stdout="轮次总结".encode("utf-8"), stderr=b"")

    monkeypatch.setattr(dsh_bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(dsh_bridge, "_latest_audit_since", lambda *args: None)
    monkeypatch.setattr(dsh_bridge.shutil, "which", lambda name: "node.exe")

    result = _runner(tmp_path)("cn", "intraday", _context())

    assert captured["command"][0] == "node.exe"
    assert "--profile" in captured["command"]
    assert captured["command"][captured["command"].index("--profile") + 1] == "investment"
    task = captured["command"][-1]
    assert "CN" in task and "盘中轮次" in task and "auto-101500" in task
    assert "investment_submit_decisions" in task
    assert captured["env"].get("DSH_TELEMETRY_DISABLED") == "1"
    assert result["status"] == "generated"
    assert result["report_text"] == "轮次总结"
    assert result["execution"] == {"fills": [], "rejected": []}
    assert result["warnings"]


def test_runner_folds_engine_audit_into_result(monkeypatch, tmp_path):
    (tmp_path / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib").mkdir(parents=True)
    (tmp_path / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js").write_text("", encoding="utf-8")
    audit_payload = {
        "command": "submit_decisions",
        "market": "cn",
        "label": "auto-101500",
        "decisions": [{"symbol": "600519", "action": "BUY", "confidence": 0.8}],
        "execution": {"fills": [{"code": "600519", "shares": 100}]},
        "risk": {"orders": []},
        "mandate": {"profile": "neutral"},
    }
    audit_path = tmp_path / "audit" / "20260812-100001-cn-auto-101500.json"
    audit_path.write_text(json.dumps(audit_payload, ensure_ascii=False), encoding="utf-8")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 0, stdout="ok".encode("utf-8"), stderr=b"")

    monkeypatch.setattr(dsh_bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(dsh_bridge.shutil, "which", lambda name: "node.exe")
    result = _runner(tmp_path)("cn", "intraday", _context())

    assert result["decisions"][0]["symbol"] == "600519"
    assert result["execution"]["fills"][0]["code"] == "600519"
    assert result["mandate"]["profile"] == "neutral"
    assert result["audit_file"] == str(audit_path)


def test_runner_reports_process_failure(monkeypatch, tmp_path):
    (tmp_path / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib").mkdir(parents=True)
    (tmp_path / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js").write_text("", encoding="utf-8")

    def fake_run(command, **kwargs):
        return subprocess.CompletedProcess(command, 1, stdout=b"", stderr="fatal error".encode("utf-8"))

    monkeypatch.setattr(dsh_bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(dsh_bridge.shutil, "which", lambda name: "node.exe")
    result = _runner(tmp_path)("cn", "intraday", _context())

    assert result["status"] == "error"
    assert "rc=1" in result["error"]
    assert "fatal error" in result["stderr_tail"]


def test_runner_reports_timeout(monkeypatch, tmp_path):
    (tmp_path / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib").mkdir(parents=True)
    (tmp_path / "app" / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js").write_text("", encoding="utf-8")

    def fake_run(command, **kwargs):
        raise subprocess.TimeoutExpired(command, 30)

    monkeypatch.setattr(dsh_bridge.subprocess, "run", fake_run)
    monkeypatch.setattr(dsh_bridge.shutil, "which", lambda name: "node.exe")
    result = _runner(tmp_path)("us", "close", _context())

    assert result["status"] == "error"
    assert "超时" in result["error"]


def test_round_task_covers_close_rounds(monkeypatch, tmp_path):
    task = dsh_bridge._round_task("us", "close", _context())
    assert "US" in task and "收盘复盘轮次" in task


def test_resolve_app_dir_finds_repo_app():
    resolved = dsh_bridge._resolve_app_dir()
    assert resolved is not None
    assert (resolved / "node_modules" / "@deepseek-ai" / "dsh").exists()


def test_install_dsh_runner_registers_when_enabled(monkeypatch):
    captured = {}

    def fake_set_runner(runner):
        captured["runner"] = runner

    monkeypatch.setattr("engine.scheduler.set_cycle_runner", fake_set_runner)
    monkeypatch.setenv("INVESTMENT_AUTO_APP_DIR", "")
    installed = dsh_bridge.install_dsh_runner()
    assert installed is True
    assert captured["runner"] is not None


def test_install_dsh_runner_skips_when_disabled(monkeypatch):
    monkeypatch.setitem(dsh_bridge.cfg.raw.setdefault("autonomous", {}), "dsh_bridge", {"enabled": False})
    installed = dsh_bridge.install_dsh_runner()
    assert installed is False
