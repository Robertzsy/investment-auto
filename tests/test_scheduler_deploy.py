"""Regression tests for scheduler weekday mapping and container deployment."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]


def _next_fire(trigger, after: datetime) -> datetime:
    next_fire = trigger.get_next_fire_time(None, after)
    assert next_fire is not None
    return next_fire


def test_scheduler_root_is_repository_root():
    from src import scheduler

    assert scheduler.ROOT == REPO_ROOT


def test_market_triggers_use_apscheduler_weekday_numbering(monkeypatch):
    from src import scheduler

    monkeypatch.setitem(scheduler.cfg.schedule, "weekdays_only", True)
    monkeypatch.setitem(scheduler.cfg.schedule, "us_early_morning_days", "1-5")
    timezone = ZoneInfo("Asia/Shanghai")

    cn = scheduler._cron_trigger("cn", "09:30")
    us_evening = scheduler._cron_trigger("us", "21:35")
    us_early = scheduler._cron_trigger("us", "01:00")

    # Saturday advances CN and US evening jobs to Monday (APScheduler 0-4).
    saturday = datetime(2026, 8, 8, 0, 0, tzinfo=timezone)
    assert _next_fire(cn, saturday) == datetime(2026, 8, 10, 9, 30, tzinfo=timezone)
    assert _next_fire(us_evening, saturday) == datetime(2026, 8, 10, 21, 35, tzinfo=timezone)

    # Beijing-time US early jobs map the US Monday session to Tuesday (1-5).
    monday = datetime(2026, 8, 10, 0, 0, tzinfo=timezone)
    assert _next_fire(us_early, monday) == datetime(2026, 8, 11, 1, 0, tzinfo=timezone)
    friday = datetime(2026, 8, 14, 2, 0, tzinfo=timezone)
    assert _next_fire(us_early, friday) == datetime(2026, 8, 15, 1, 0, tzinfo=timezone)


def test_scheduler_respects_weekdays_only_false(monkeypatch):
    from src import scheduler

    monkeypatch.setitem(scheduler.cfg.schedule, "weekdays_only", False)
    timezone = ZoneInfo("Asia/Shanghai")
    trigger = scheduler._cron_trigger("cn", "09:30")

    saturday = datetime(2026, 8, 8, 0, 0, tzinfo=timezone)
    assert _next_fire(trigger, saturday) == datetime(2026, 8, 8, 9, 30, tzinfo=timezone)


def test_us_early_morning_days_are_configured_and_normalized(monkeypatch):
    from src import scheduler

    config = yaml.safe_load((REPO_ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))
    assert config["schedule"]["us_early_morning_days"] == "1-5"
    assert scheduler._normalize_days([1, 2, 3, 4, 5]) == "1-5"

    monkeypatch.setitem(scheduler.cfg.schedule, "weekdays_only", True)
    monkeypatch.setitem(scheduler.cfg.schedule, "us_early_morning_days", "1,2-5")
    assert scheduler._day_of_week("us", "04:10") == "1-5"
    assert scheduler._day_of_week("us", "23:30") == "0-4"

    with pytest.raises(ValueError):
        scheduler._normalize_days("1-7")


def test_compose_runs_scheduler_and_loopback_only_chat():
    compose = yaml.safe_load((REPO_ROOT / "docker-compose.yml").read_text(encoding="utf-8"))
    services = compose["services"]

    assert set(services) == {"scheduler", "chat"}
    assert services["scheduler"]["command"][-1] == "run"
    assert services["chat"]["command"][-1] == "chat"
    assert services["chat"]["environment"] == {
        "CHAT_HOST": "0.0.0.0",
        "CHAT_PORT": 8080,
        "CHAT_OPEN_BROWSER": "false",
    }
    assert services["chat"]["ports"] == ["127.0.0.1:8080:8080"]
    assert "ports" not in services["scheduler"]

    expected_volumes = {"./config:/app/config", "./runtime:/app/runtime"}
    assert set(services["scheduler"]["volumes"]) == expected_volumes
    assert set(services["chat"]["volumes"]) == expected_volumes


def test_container_context_and_runtime_chat_files_are_excluded():
    dockerfile = (REPO_ROOT / "Dockerfile").read_text(encoding="utf-8")
    dockerignore = (REPO_ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()
    gitignore = (REPO_ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
    main_source = (REPO_ROOT / "src" / "main.py").read_text(encoding="utf-8")

    assert "EXPOSE 8080" in dockerfile
    for pattern in (".env", ".git", ".venv", "runtime"):
        assert pattern in dockerignore
    assert "/runtime/chat_*" in gitignore
    for variable in ("CHAT_HOST", "CHAT_PORT", "CHAT_OPEN_BROWSER"):
        assert variable in main_source
