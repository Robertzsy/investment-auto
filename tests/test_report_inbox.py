from __future__ import annotations

from src.manager.report_inbox import acknowledge_event, pending_events, publish_cycle_report


def test_cycle_report_is_atomically_queued_for_chat(tmp_path):
    result = {
        "status": "generated", "market": "cn", "label": "1300", "report": "report.md",
        "autonomous": {"status": "error", "error": "risk manager failed", "fills": []},
    }
    queued = publish_cycle_report(result, title="CN report", report_content="full body", inbox_dir=tmp_path)

    assert queued["status"] == "queued_for_chat"
    events = pending_events(tmp_path)
    assert events[0]["execution_status"] == "error"
    assert "投资流程发生错误" in events[0]["content"]
    assert "full body" in events[0]["content"]


def test_acknowledge_removes_report_after_live_client_receives_it(tmp_path):
    queued = publish_cycle_report(
        {"status": "generated", "market": "cn", "label": "button", "autonomous": {}},
        title="CN report",
        report_content="body",
        inbox_dir=tmp_path,
    )

    assert acknowledge_event(queued["event_id"], tmp_path) is True
    assert pending_events(tmp_path) == []
    assert acknowledge_event("../escape", tmp_path) is False
