from __future__ import annotations

from src import notifications


def test_notifications_are_non_blocking_when_disabled(monkeypatch):
    monkeypatch.setattr(notifications.cfg, "_data", {"notify": {"enabled": False}})
    assert notifications.deliver_report(title="x", content="y") == {"status": "disabled"}


def test_notification_skips_missing_webhook(monkeypatch):
    monkeypatch.setattr(notifications.cfg, "_data", {
        "notify": {"enabled": True, "channels": ["webhook"], "webhook_url_env": "TEST_WEBHOOK"}
    })
    monkeypatch.delenv("TEST_WEBHOOK", raising=False)
    result = notifications.deliver_report(title="x", content="y")
    assert result["status"] == "skipped"
