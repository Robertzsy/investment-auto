from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

from src.trading import agent_workflow, checkpoints

NOW = datetime(2026, 8, 14, 12, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


def test_stage_roundtrip_and_list_stages(tmp_path):
    checkpoints.CHECKPOINT_DIR = tmp_path / "checkpoints"
    checkpoints.init_checkpoint("cycle-1", "cn", ["600519"])
    checkpoints.save_stage("cycle-1", "600519:base_analysis", {"reports": {"technical_analyst": {"summary": "ok"}}})

    saved = checkpoints.load_stage("cycle-1", "600519:base_analysis")
    assert saved["reports"]["technical_analyst"]["summary"] == "ok"
    assert checkpoints.list_stages("cycle-1") == ["600519-base_analysis"]  # stage names are sanitized
    assert checkpoints.load_checkpoint("cycle-1")["status"] == "running"
    checkpoints.CHECKPOINT_DIR = checkpoints.ROOT / "runtime" / "trading" / "checkpoints"


def test_list_incomplete_filters_stale_and_completed(tmp_path):
    checkpoints.CHECKPOINT_DIR = tmp_path / "checkpoints"
    checkpoints.init_checkpoint("fresh", "cn", ["600519"])
    checkpoints.init_checkpoint("done", "cn", ["600519"])
    checkpoints.mark_completed("done")
    # Stale: rewrite index.json with an old updated_at.
    stale_index = checkpoints._index_path("fresh")
    payload = json.loads(stale_index.read_text(encoding="utf-8"))
    payload["updated_at"] = (NOW - timedelta(hours=3)).isoformat(timespec="seconds")
    stale_index.write_text(json.dumps(payload), encoding="utf-8")

    incomplete = checkpoints.list_incomplete(now=NOW, stale_minutes=90)
    ids = [item["cycle_id"] for item in incomplete]
    assert ids == []  # fresh is stale, done is completed

    checkpoints.CHECKPOINT_DIR = checkpoints.ROOT / "runtime" / "trading" / "checkpoints"


def test_checkpointed_reuses_archived_stage(monkeypatch, tmp_path):
    checkpoints.CHECKPOINT_DIR = tmp_path / "checkpoints"
    calls = []

    def runner():
        calls.append(1)
        return {"value": 42}

    checkpoint = {"cycle_id": "cycle-1"}
    result, resumed = agent_workflow._checkpointed(checkpoint, "stage-a", runner)
    assert result["value"] == 42 and not resumed
    result2, resumed2 = agent_workflow._checkpointed(checkpoint, "stage-a", runner)
    assert result2["value"] == 42 and resumed2
    assert len(calls) == 1  # runner did not re-run
    checkpoints.CHECKPOINT_DIR = checkpoints.ROOT / "runtime" / "trading" / "checkpoints"


def _context() -> dict:
    return {
        "as_of": "2026-08-14T12:00:00+08:00",
        "market": "cn",
        "allowed_symbols": ["600519"],
        "account": {"cash": 100000, "holdings": []},
        "snapshots": {
            "600519": {
                "realtime": {"price": 100, "pe": 20},
                "indicators": {"rsi": {"rsi14": 55}},
                "history": [{"date": "2026-08-13", "close": 99}],
            }
        },
        "screening": {
            "selected": [{"symbol": "600519", "score": 80}],
            "selected_symbols": ["600519"],
        },
        "macro_excerpt": "",
        "optimizer": {},
        "market_rules": {"trading": {"settlement": "T+1"}},
    }


def _config() -> dict:
    return {
        "agent_workflow": {
            "enabled": True,
            "require_citations": True,
            "minimum_citations": 1,
            "minimum_base_analysts": 2,
            "research_debate_rounds": 1,
            "risk_debate_rounds": 1,
            "memory_enabled": True,
            "memory_entries_per_role": 3,
            "memory_chars_per_entry": 200,
            "roles": {},
        }
    }


def test_interrupted_symbol_research_resumes_without_new_llm_calls(monkeypatch, tmp_path):
    checkpoints.CHECKPOINT_DIR = tmp_path / "checkpoints"
    calls = []

    def fake_call(role, **kwargs):
        evidence = kwargs["evidence"]
        calls.append(role)
        citation = next(iter(evidence))
        return {
            "role": role,
            "summary": "resume-test",
            "findings": [{"claim": "x", "impact": "neutral", "evidence_ids": [citation]}],
            "stance": "HOLD",
            "confidence": 0.6,
            "data_gaps": [],
            "citations": [citation],
        }

    monkeypatch.setattr(agent_workflow, "_call_role", fake_call)
    checkpoint = {"cycle_id": "cycle-1"}
    first = agent_workflow._run_symbol_research(
        "600519",
        context=_context(),
        evidence=agent_workflow.build_evidence_catalog(_context()),
        settings=_config()["agent_workflow"],
        memory_store=agent_workflow.AgentMemoryStore(tmp_path / "memory"),
        generated_at="2026-08-14T12:00:00+08:00",
        checkpoint=checkpoint,
    )
    assert first["status"] == "completed"
    first_calls = len(calls)
    assert first_calls > 0

    second = agent_workflow._run_symbol_research(
        "600519",
        context=_context(),
        evidence=agent_workflow.build_evidence_catalog(_context()),
        settings=_config()["agent_workflow"],
        memory_store=agent_workflow.AgentMemoryStore(tmp_path / "memory"),
        generated_at="2026-08-14T12:00:00+08:00",
        checkpoint=checkpoint,
    )
    assert second["status"] == "completed"
    assert second["trader"]["summary"] == "resume-test"
    assert len(calls) == first_calls  # every stage came from the archive

    checkpoints.CHECKPOINT_DIR = checkpoints.ROOT / "runtime" / "trading" / "checkpoints"


def test_execution_fail_safe_state_machine(tmp_path):
    checkpoints.CHECKPOINT_DIR = tmp_path / "checkpoints"
    checkpoints.init_checkpoint("cycle-x", "cn", ["600519"])
    checkpoints.mark_execution_pending("cycle-x")
    state = checkpoints.load_checkpoint("cycle-x")
    assert state["execution_pending"] and not state["execution_completed"]

    checkpoints.mark_execution_completed("cycle-x")
    state = checkpoints.load_checkpoint("cycle-x")
    assert state["execution_completed"] and state["status"] == "completed"
    checkpoints.CHECKPOINT_DIR = checkpoints.ROOT / "runtime" / "trading" / "checkpoints"


def test_discard_checkpoint_removes_directory(tmp_path):
    checkpoints.CHECKPOINT_DIR = tmp_path / "checkpoints"
    checkpoints.init_checkpoint("cycle-y", "cn", ["600519"])
    checkpoints.save_stage("cycle-y", "s1", {"a": 1})
    checkpoints.discard_checkpoint("cycle-y")
    assert not (tmp_path / "checkpoints" / "cycle-y").exists()
    checkpoints.CHECKPOINT_DIR = checkpoints.ROOT / "runtime" / "trading" / "checkpoints"
