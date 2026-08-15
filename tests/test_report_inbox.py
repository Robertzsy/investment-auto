from __future__ import annotations

from src.manager.report_inbox import pending_events, publish_cycle_report


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
