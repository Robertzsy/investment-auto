from __future__ import annotations

import io
import json
from pathlib import Path

from src.ui import server


class _Handler:
    def __init__(self, body: dict | None = None) -> None:
        payload = json.dumps(body or {}).encode("utf-8")
        self.headers = {"Content-Length": str(len(payload))}
        self.rfile = io.BytesIO(payload)
        self.status = 0
        self.payload: dict = {}

    def _json_response(self, status: int, data: dict) -> None:
        self.status = status
        self.payload = data


def test_harness_snapshot_exposes_control_plane_without_local_roots(monkeypatch):
    monkeypatch.setattr("src.manager.execution_trace.ExecutionTrace.recent", classmethod(lambda cls, limit=30: []))
    monkeypatch.setattr("src.manager.session_store.SessionStore.records", lambda self: [])
    monkeypatch.setattr("src.manager.skill_scheduler.SkillScheduler.list", lambda self: [])
    monkeypatch.setattr("src.platform.memory_store.StructuredMemoryStore.recent", lambda self, *args, **kwargs: [])

    payload = server._harness_snapshot(limit=5)

    assert payload["runtime"] == "skill-runtime"
    assert payload["version"] == "0.9.1"
    assert payload["summary"]["skills"] >= 8
    assert all("root" not in skill for skill in payload["skills"])
    assert all(skill["workflow"] for skill in payload["skills"])
    assert payload["repairs"] == []


def test_harness_schedule_api_create_and_control(monkeypatch):
    created = []

    def fake_create(self, **kwargs):
        created.append(kwargs)
        return {"status": "scheduled", **kwargs}

    monkeypatch.setattr("src.manager.skill_scheduler.SkillScheduler.create", fake_create)
    create = _Handler({
        "skill_name": "portfolio-review",
        "schedule_id": "daily-risk",
        "cron": "0 18 * * 1-5",
        "timezone": "Asia/Shanghai",
        "inputs": {"market": "cn"},
    })
    server.ChatHandler._handle_harness_schedule_create(create)  # type: ignore[arg-type]
    assert create.status == 201
    assert created[0]["inputs"] == {"market": "cn"}

    monkeypatch.setattr(
        "src.manager.skill_scheduler.SkillScheduler.set_enabled",
        lambda self, schedule_id, enabled: {"status": "disabled", "schedule_id": schedule_id},
    )
    control = _Handler({"schedule_id": "daily-risk", "action": "disable"})
    server.ChatHandler._handle_harness_schedule_control(control)  # type: ignore[arg-type]
    assert control.status == 200
    assert control.payload["status"] == "disabled"


def test_desktop_harness_page_and_chat_surface_are_wired():
    ui = Path(__file__).parents[1] / "src" / "ui"
    harness = (ui / "harness.html").read_text(encoding="utf-8")
    chat = (ui / "index.html").read_text(encoding="utf-8")

    assert "/api/harness?limit=80" in harness
    assert "/api/harness/schedules/control" in harness
    assert "完成验证与执行轨迹" in harness
    assert "受监督修复审计" in harness
    assert 'href="/harness"' in chat
    assert "loadHarnessSummary();" in chat
    assert "完全操作权限" not in chat
