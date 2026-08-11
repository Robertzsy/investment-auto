"""Macro storage and startup catch-up regressions."""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

from src import macro, scheduler


def test_macro_report_syncs_into_standalone_runtime(monkeypatch, tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "daily").mkdir(parents=True)
    (source / "news").mkdir(parents=True)
    (source / "daily" / "2026-08-10.md").write_text("# macro", encoding="utf-8")
    (source / "news" / "2026-08-10.json").write_text('{"news": []}', encoding="utf-8")
    monkeypatch.setattr(macro, "_external_macro_roots", lambda: [source])

    result = macro.sync_external_report("2026-08-10", target)

    assert result and result["status"] == "synced"
    assert (target / "daily" / "2026-08-10.md").read_text(encoding="utf-8") == "# macro"
    assert (target / "news" / "2026-08-10.json").exists()
    assert "2026-08-10" in (target / "marks.json").read_text(encoding="utf-8")


def test_json_only_macro_source_is_not_marked_complete(monkeypatch, tmp_path):
    source = tmp_path / "source"
    target = tmp_path / "target"
    (source / "news").mkdir(parents=True)
    (source / "news" / "2026-08-10.json").write_text('{"news": []}', encoding="utf-8")
    monkeypatch.setattr(macro, "_external_macro_roots", lambda: [source])

    assert macro.sync_external_report("2026-08-10", target) is None
    assert (target / "news" / "2026-08-10.json").exists()
    assert not (target / "daily" / "2026-08-10.md").exists()
    assert not (target / "marks.json").exists()


def test_macro_empty_runtime_creates_directories_and_passes_reference_date(monkeypatch, tmp_path):
    monkeypatch.setattr(macro, "_external_macro_roots", lambda: [])
    monkeypatch.setattr(macro.shutil, "which", lambda name: "/usr/bin/node")

    def fake_run(command, **kwargs):
        assert command[-2:] == ["--date", "2026-08-11"]
        for directory in ("news", "daily", "weekly", "monthly", "yearly"):
            assert (tmp_path / directory).is_dir()
        (tmp_path / "daily" / "2026-08-10.md").write_text("# generated", encoding="utf-8")
        return SimpleNamespace(returncode=0, stdout=b"ok", stderr=b"")

    monkeypatch.setattr(macro.subprocess, "run", fake_run)
    result = macro.run_daily(
        force=True,
        now=datetime(2026, 8, 11, 9, 0, tzinfo=ZoneInfo("Asia/Shanghai")),
        data_root=tmp_path,
    )
    assert result["status"] == "generated"
    assert (tmp_path / "daily" / "2026-08-10.md").exists()


def test_bundled_macro_scripts_write_runtime_macro_by_default():
    root = Path(__file__).resolve().parents[1]
    collect = (root / "scripts" / "macro-environment" / "collect.js").read_text(encoding="utf-8")
    report = (root / "scripts" / "macro-environment" / "report.js").read_text(encoding="utf-8")
    run = (root / "scripts" / "macro-environment" / "run.js").read_text(encoding="utf-8")
    assert "process.env.MACRO_DATA_DIR" in collect
    assert "process.env.MACRO_DATA_DIR" in report
    assert "runtime', 'macro" in collect
    assert "runtime', 'macro" in report
    assert "--date" in run
    assert "execFileSync" in run


def test_startup_catch_up_finds_missed_cn_rounds(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "REPORT_DIR", tmp_path)
    now = datetime(2026, 8, 11, 13, 28, tzinfo=ZoneInfo("Asia/Shanghai"))

    planned = scheduler.planned_catch_up(now, ["cn"])
    assert [item["label"] for item in planned] == ["0930", "1030", "1300"]

    scheduler._round_report_path("cn", "0930", now).parent.mkdir(parents=True, exist_ok=True)
    scheduler._round_report_path("cn", "0930", now).write_text("done", encoding="utf-8")
    planned = scheduler.planned_catch_up(now, ["cn"])
    assert [item["label"] for item in planned] == ["1030", "1300"]


def test_scheduler_report_completion_disables_deepseek_thinking():
    calls = []

    class FakeLLM:
        provider_name = "deepseek"

        def chat(self, messages, **kwargs):
            calls.append(kwargs)
            return "完整报告"

    assert scheduler._complete_report(FakeLLM(), [{"role": "user", "content": "x"}]) == "完整报告"
    assert calls == [{
        "temperature": 0.2,
        "max_tokens": 4096,
        "extra_body": {"thinking": {"type": "disabled"}},
    }]


def test_us_evening_misfire_keeps_original_schedule_date(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(scheduler, "_account_context", lambda market: {"account": {}, "holding_snapshots": []})
    monkeypatch.setattr(scheduler, "_latest_macro_excerpt", lambda: "")

    class FakeLLM:
        def chat(self, messages, **kwargs):
            return "report"

    monkeypatch.setattr(scheduler, "resolve_llm", lambda **kwargs: FakeLLM())
    actual = datetime(2026, 8, 11, 1, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
    scheduled = scheduler._scheduled_reference(actual, "21:35")
    assert scheduled == datetime(2026, 8, 10, 21, 35, tzinfo=ZoneInfo("Asia/Shanghai"))

    result = scheduler._run_intraday_job("us", "21:35", "2135", now=actual, scheduled_at=scheduled)
    assert result["status"] == "generated"
    assert (tmp_path / "20260810-us-2135.md").exists()
    assert not (tmp_path / "20260811-us-2135.md").exists()


def test_report_claim_prevents_duplicate_job_execution(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "REPORT_DIR", tmp_path)
    now = datetime(2026, 8, 11, 10, 31, tzinfo=ZoneInfo("Asia/Shanghai"))
    report = scheduler._round_report_path("cn", "1030", now)
    report.parent.mkdir(parents=True, exist_ok=True)
    report.with_suffix(report.suffix + ".lock").write_text("busy", encoding="utf-8")
    monkeypatch.setattr(scheduler, "resolve_llm", lambda **kwargs: (_ for _ in ()).throw(AssertionError("must not run")))

    result = scheduler._run_intraday_job("cn", "10:30", "1030", now=now, scheduled_at=now.replace(minute=30))
    assert result["status"] == "in_progress"


def test_scheduler_process_lease_blocks_duplicate_instances(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "SCHEDULER_LOCK", tmp_path / "scheduler.lock")
    first = scheduler.start(catch_up=False)
    try:
        with pytest.raises(RuntimeError, match="调度器已经在运行"):
            scheduler.start(catch_up=False)
    finally:
        first.shutdown(wait=False)
    second = scheduler.start(catch_up=False)
    second.shutdown(wait=False)


def test_scheduler_lease_converts_windows_share_violation_to_running_error(monkeypatch, tmp_path):
    lease = scheduler.ProcessLease(tmp_path / "scheduler.lock")

    def denied(*args, **kwargs):
        raise PermissionError("Windows sharing violation")

    monkeypatch.setattr(Path, "open", denied)
    with pytest.raises(RuntimeError, match="调度器已经在运行"):
        lease.acquire()


def test_scheduler_registers_macro_job(monkeypatch, tmp_path):
    monkeypatch.setattr(scheduler, "SCHEDULER_LOCK", tmp_path / "scheduler.lock")
    monkeypatch.setattr(scheduler, "run_catch_up", lambda: [])
    instance = scheduler.start(catch_up=False)
    try:
        assert "macro-daily" in {job.id for job in instance.get_jobs()}
    finally:
        instance.shutdown(wait=False)
