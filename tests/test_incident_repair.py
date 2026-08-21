from __future__ import annotations

from types import SimpleNamespace

import pytest

from src.manager.change_manager import ChangeManager
from src.manager.execution_trace import ExecutionTrace
from src.manager.incident_repair import IncidentRepairSupervisor
from src.manager.repair_verifier import verify
from src.manager.skill_runtime import SkillRuntime
from src.platform.memory_store import StructuredMemoryStore


def _trace(*, error: str = "", failed_gates=None):
    steps = []
    if error:
        steps.append({"id": "snapshot", "action": "security.snapshot", "status": "failed", "error": error})
    return {
        "execution_id": "a" * 32,
        "request": "今天的A股行情怎么样",
        "skill": "market-overview",
        "skill_version": "1.0.0",
        "status": "degraded",
        "inputs": {"request": "今天的A股行情怎么样", "market": "cn"},
        "steps": steps,
        "validation": {
            "failed_quality_gates": list(failed_gates or ["quality.market_breadth_available"]),
            "missing_steps": [],
            "missing_outputs": [],
        },
    }


def _mock_trace(monkeypatch, value):
    monkeypatch.setattr(ExecutionTrace, "recent", classmethod(lambda cls, limit=50, root=None: [{
        "execution_id": value["execution_id"], "skill": value["skill"], "status": value["status"]
    }]))
    monkeypatch.setattr(ExecutionTrace, "load", classmethod(lambda cls, execution_id, root=None: value))


def test_incident_supervisor_replays_original_request_and_requires_semantic_pass(monkeypatch, tmp_path):
    value = _trace()
    _mock_trace(monkeypatch, value)
    calls = []

    def run(self, request, **kwargs):
        calls.append((request, kwargs))
        return {
            "status": "completed",
            "execution_id": "b" * 32,
            "validation": {"passed": True},
            "user_report": "verified",
            "error": "",
        }

    monkeypatch.setattr(SkillRuntime, "run", run)
    supervisor = IncidentRepairSupervisor(StructuredMemoryStore(tmp_path / "memory"))
    result = supervisor.repair(execution_id=value["execution_id"])
    assert result["status"] == "already_fixed"
    assert result["mutation_count"] == 0
    assert result["replay"]["semantic_validation_passed"] is True
    assert calls[0][0] == value["request"]
    assert calls[0][1]["skill_name"] == "market-overview"
    assert supervisor.store.recent("incident_repairs")[0]["status"] == "already_fixed"


def test_incident_supervisor_marks_external_state_without_code_mutation(monkeypatch, tmp_path):
    value = _trace(error="provider returned 402 insufficient balance", failed_gates=[])
    _mock_trace(monkeypatch, value)
    result = IncidentRepairSupervisor(StructuredMemoryStore(tmp_path / "memory")).repair(
        execution_id=value["execution_id"]
    )
    assert result["status"] == "external_blocker"
    assert result["mutation_count"] == 0
    assert result["diagnosis"]["repairable"] is False


def test_incident_patch_rolls_back_when_original_request_still_fails(monkeypatch, tmp_path):
    value = _trace(failed_gates=["quality.coverage_ratio"])
    _mock_trace(monkeypatch, value)
    applied = []
    rolled_back = []

    monkeypatch.setattr(ChangeManager, "inspect", lambda self, path, **kwargs: {
        "path": path,
        "sha256": "old",
        "content": "before\ntarget\nafter\n",
        "truncated": False,
    })
    monkeypatch.setattr(ChangeManager, "apply_text_change", lambda self, path, new_content, **kwargs: (
        applied.append((path, new_content, kwargs)) or {
            "change_id": "change-1",
            "target": path,
            "new_sha256": "new",
            "rolled_back": False,
            "backup": "change-1/src/manager/action_registry.py",
        }
    ))
    monkeypatch.setattr(ChangeManager, "rollback", lambda self, change, **kwargs: (
        rolled_back.append((change, kwargs)) or {"status": "semantic_replay_rolled_back"}
    ))
    monkeypatch.setattr(IncidentRepairSupervisor, "replay_fresh", lambda self, execution_id="": {
        "semantic_validation_passed": False,
        "status": "not_verified",
        "replay_execution_id": "c" * 32,
        "fresh_process": True,
    })

    supervisor = IncidentRepairSupervisor(StructuredMemoryStore(tmp_path / "memory"))
    result = supervisor.repair(
        execution_id=value["execution_id"],
        path="src/manager/action_registry.py",
        patch_find="target",
        patch_replace="fixed",
        expected_sha256="old",
        reason="fix coverage",
    )
    assert result["status"] == "rolled_back"
    assert result["mutation_count"] == 1
    assert len(applied) == 1
    assert len(rolled_back) == 1
    assert applied[0][1] == "before\nfixed\nafter\n"
    assert applied[0][2]["tests"] == [
        "python -m compileall src",
        "python -m src.manager.repair_verifier data_gap",
    ]
    assert supervisor.store.recent("incident_repairs")[0]["fresh_process"] is True


def test_existing_file_repair_rejects_full_file_replacement():
    manager = SimpleNamespace(inspect=lambda path, **kwargs: {
        "sha256": "hash",
        "content": "original",
        "truncated": False,
    })
    with pytest.raises(ValueError, match="patch_find"):
        IncidentRepairSupervisor._materialize_patch(
            manager,
            path="src/manager/action_registry.py",
            expected_sha256="hash",
            new_content="replacement",
            patch_find="",
            patch_replace="",
        )


def test_fresh_replay_uses_new_interpreter_and_parses_marker(monkeypatch):
    calls = []

    def run(argv, **kwargs):
        calls.append((argv, kwargs))
        return SimpleNamespace(
            returncode=0,
            stdout=(
                'noise\nIA_REPAIR_REPLAY={"status":"verified",'
                '"semantic_validation_passed":true,"replay_execution_id":"fresh"}\n'
            ).encode(),
            stderr=b"",
        )

    monkeypatch.setattr("src.manager.incident_repair.subprocess.run", run)
    result = IncidentRepairSupervisor().replay_fresh("a" * 32)
    assert result["semantic_validation_passed"] is True
    assert result["fresh_process"] is True
    assert calls[0][0][1:3] == ["-m", "src.manager.repair_replay"]


@pytest.mark.parametrize("category", [
    "identity_mismatch", "stale_data", "data_gap", "contract_failure", "workflow_failure",
])
def test_installed_runtime_repair_verifier_has_no_pytest_dependency(category):
    result = verify(category)
    assert result["status"] == "passed"
    assert result["probes"]


def test_real_fresh_process_replays_deterministic_read_only_skill(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTMENT_AUTO_DATA_DIR", str(tmp_path))
    trace_root = tmp_path / "runtime" / "manager" / "trajectories"
    trace = ExecutionTrace(
        request="查看 Harness 能力目录",
        skill="system-administration",
        version="1.0.0",
        session_scope="system_admin",
        inputs={"request": "查看 Harness 能力目录"},
        root=trace_root,
    )
    trace.finish(
        status="degraded",
        validation={"passed": False, "failed_quality_gates": ["quality.synthetic"]},
        result={"status": "degraded"},
    )
    result = IncidentRepairSupervisor().replay_fresh(trace.execution_id)
    assert result["fresh_process"] is True
    assert result["semantic_validation_passed"] is True
    assert result["replay_status"] == "completed"
