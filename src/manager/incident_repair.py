from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from src.manager.execution_trace import ExecutionTrace
from src.manager.skill_registry import SkillRegistry
from src.platform.memory_store import StructuredMemoryStore
from src.subprocess_utils import decode_subprocess_output, hidden_subprocess_kwargs


_EXTERNAL_MARKERS = (
    "402", "billing", "余额", "quota", "network", "timeout", "timed out",
    "dns", "connection", "权限", "permission", "认证", "unauthorized",
)
_REPLAY_MARKER = "IA_REPAIR_REPLAY="


class IncidentRepairSupervisor:
    """Diagnose one failed trajectory and prove a repair by replaying its original request."""

    def __init__(self, store: Optional[StructuredMemoryStore] = None) -> None:
        self.store = store or StructuredMemoryStore()

    def _audit(self, **record: Any) -> Dict[str, Any]:
        return self.store.append("incident_repairs", record)

    def _target(self, execution_id: str = "") -> Dict[str, Any]:
        if str(execution_id or "").strip():
            trace = ExecutionTrace.load(execution_id)
            if trace.get("skill") == "incident-repair":
                raise ValueError("不能把 incident-repair 自身作为修复目标")
            return trace
        for row in ExecutionTrace.recent(limit=100):
            if row.get("skill") == "incident-repair" or row.get("status") in {"completed", "running"}:
                continue
            return ExecutionTrace.load(str(row.get("execution_id", "")))
        raise FileNotFoundError("没有可修复的 degraded/incomplete 执行轨迹")

    @staticmethod
    def _diagnosis(trace: Mapping[str, Any]) -> Dict[str, Any]:
        validation = trace.get("validation", {}) if isinstance(trace.get("validation"), Mapping) else {}
        failed_gates = [str(item) for item in validation.get("failed_quality_gates", [])]
        errors = []
        suspected_actions = []
        for step in trace.get("steps", []) if isinstance(trace.get("steps"), list) else []:
            if not isinstance(step, Mapping):
                continue
            if step.get("status") == "failed":
                errors.append(str(step.get("error", "")))
                suspected_actions.append(str(step.get("action", "")))
            output = step.get("output", {}) if isinstance(step.get("output"), Mapping) else {}
            if output.get("error"):
                errors.append(str(output.get("error")))
            if isinstance(output.get("errors"), Mapping):
                errors.extend(str(value) for value in output["errors"].values() if str(value))
            if output.get("recoveries") and isinstance(output.get("recoveries"), list):
                errors.extend(
                    str(item.get("reason", ""))
                    for item in output["recoveries"]
                    if isinstance(item, Mapping) and item.get("reason")
                )
        joined = " ".join(failed_gates + errors).casefold()
        if any(marker in joined for marker in _EXTERNAL_MARKERS):
            category = "external_blocker"
            repairable = False
        elif any("identity" in item or "fact_consistent" in item for item in failed_gates) or "身份" in joined:
            category = "identity_mismatch"
            repairable = True
        elif any("freshness" in item for item in failed_gates) or "过期" in joined:
            category = "stale_data"
            repairable = True
        elif any("coverage" in item or "available" in item for item in failed_gates) or "覆盖" in joined:
            category = "data_gap"
            repairable = True
        elif validation.get("missing_outputs") or validation.get("missing_steps"):
            category = "contract_failure"
            repairable = True
        else:
            category = "workflow_failure"
            repairable = True
        suggested_files = {
            "identity_mismatch": ["src/manager/security_identity.py", "src/manager/action_registry.py"],
            "stale_data": ["src/manager/security_identity.py", "src/manager/action_registry.py"],
            "data_gap": ["src/manager/action_registry.py", "src/manager/builtin_skills"],
            "contract_failure": ["src/manager/completion_validator.py", "src/manager/builtin_skills"],
            "workflow_failure": ["src/manager/skill_runtime.py", "src/manager/action_registry.py"],
            "external_blocker": [],
        }[category]
        return {
            "category": category,
            "repairable": repairable,
            "failed_quality_gates": failed_gates,
            "missing_steps": list(validation.get("missing_steps", [])),
            "missing_outputs": list(validation.get("missing_outputs", [])),
            "errors": errors[:8],
            "suspected_actions": list(dict.fromkeys(filter(None, suspected_actions))),
            "suggested_files": suggested_files,
            "requires_replay": repairable,
        }

    def inspect(self, execution_id: str = "") -> Dict[str, Any]:
        trace = self._target(execution_id)
        return {
            "execution_id": trace.get("execution_id"),
            "request": trace.get("request", ""),
            "skill": trace.get("skill", ""),
            "skill_version": trace.get("skill_version", ""),
            "status": trace.get("status", ""),
            "diagnosis": self._diagnosis(trace),
        }

    def replay(self, execution_id: str = "") -> Dict[str, Any]:
        from src.manager.session_store import SessionStore
        from src.manager.skill_runtime import SkillRuntime

        trace = self._target(execution_id)
        package = SkillRegistry().get(str(trace.get("skill", "")))
        if package.manifest.side_effect_level != "read_only":
            raise PermissionError("自动回放只允许 read_only Skill；写入和投资执行必须由用户再次授权")
        inputs = trace.get("inputs", {}) if isinstance(trace.get("inputs"), Mapping) else {}
        replay = SkillRuntime(sessions=SessionStore()).run(
            str(trace.get("request", "")),
            skill_name=package.manifest.name,
            inputs=dict(inputs),
            requested_by="incident-repair-supervisor",
        )
        success = replay.get("status") == "completed" and replay.get("validation", {}).get("passed") is True
        return {
            "original_execution_id": trace.get("execution_id"),
            "replay_execution_id": replay.get("execution_id"),
            "status": "verified" if success else "not_verified",
            "semantic_validation_passed": success,
            "replay_status": replay.get("status"),
            "validation": replay.get("validation", {}),
            "user_report": replay.get("user_report", ""),
            "error": replay.get("error", ""),
        }

    def replay_fresh(self, execution_id: str) -> Dict[str, Any]:
        """Replay in a new interpreter so a just-written patch is actually imported."""
        from src.manager.change_manager import ROOT

        completed = subprocess.run(
            [sys.executable, "-m", "src.manager.repair_replay", str(execution_id)],
            cwd=str(ROOT),
            capture_output=True,
            timeout=240,
            **hidden_subprocess_kwargs(),
        )
        stdout = decode_subprocess_output(completed.stdout)
        stderr = decode_subprocess_output(completed.stderr)
        payload: Optional[Dict[str, Any]] = None
        for line in reversed(stdout.splitlines()):
            if not line.startswith(_REPLAY_MARKER):
                continue
            try:
                candidate = json.loads(line[len(_REPLAY_MARKER):])
            except json.JSONDecodeError:
                continue
            if isinstance(candidate, dict):
                payload = candidate
                break
        if payload is None:
            raise RuntimeError(
                "新进程语义回放没有返回可验证结果：" + (stderr or stdout or "空输出")[:2000]
            )
        payload["fresh_process"] = True
        payload["process_returncode"] = completed.returncode
        if completed.returncode and not payload.get("error"):
            payload["error"] = (stderr or "新进程回放失败")[:2000]
        return payload

    @staticmethod
    def _materialize_patch(
        manager: Any,
        *,
        path: str,
        expected_sha256: str,
        new_content: str,
        patch_find: str,
        patch_replace: str,
    ) -> tuple[str, str]:
        try:
            inspected = manager.inspect(path, max_chars=50_000)
        except FileNotFoundError:
            if patch_find:
                raise ValueError("新文件不能使用 patch_find；请提供完整 new_content")
            if expected_sha256:
                raise ValueError("创建新文件时 expected_sha256 必须为空")
            if not new_content:
                raise ValueError("创建新文件必须提供 new_content")
            if len(new_content) > 50_000:
                raise ValueError("单个新文件补丁不能超过 50000 字符")
            return str(new_content), "create_file"

        if inspected.get("truncated"):
            raise ValueError("目标文件超过最小补丁处理上限，必须先缩小故障范围")
        actual_hash = str(inspected.get("sha256", ""))
        if not expected_sha256:
            raise ValueError("修改现有文件必须提供 inspect 返回的 expected_sha256")
        if expected_sha256 != actual_hash:
            raise RuntimeError("文件在诊断后已发生变化，请重新 inspect 后再修复")
        if not patch_find:
            raise ValueError("现有文件只接受最小精确补丁 patch_find/patch_replace，不接受整文件替换")
        if len(patch_find) > 20_000 or len(patch_replace) > 20_000:
            raise ValueError("最小补丁的查找或替换片段不能超过 20000 字符")
        if max(len(patch_find.splitlines()), len(patch_replace.splitlines())) > 200:
            raise ValueError("一次修复最多改动 200 行")
        content = str(inspected.get("content", ""))
        occurrences = content.count(patch_find)
        if occurrences != 1:
            raise ValueError(f"patch_find 必须在目标文件中精确出现一次，当前出现 {occurrences} 次")
        patched = content.replace(patch_find, patch_replace, 1)
        if patched == content:
            raise ValueError("补丁没有产生任何变化")
        return patched, "exact_replace"

    def repair(
        self,
        *,
        execution_id: str = "",
        path: str = "",
        new_content: str = "",
        patch_find: str = "",
        patch_replace: str = "",
        expected_sha256: str = "",
        reason: str = "",
    ) -> Dict[str, Any]:
        from src.manager.change_manager import ChangeManager

        target = self._target(execution_id)
        diagnosis = self._diagnosis(target)
        if diagnosis["category"] == "external_blocker":
            result = {
                "status": "external_blocker",
                "execution_id": target.get("execution_id"),
                "diagnosis": diagnosis,
                "mutation_count": 0,
                "message": "外部账户、网络、权限或供应商状态不能通过本地代码伪修复。",
            }
            result["audit"] = self._audit(
                execution_id=target.get("execution_id"),
                status="external_blocker",
                category=diagnosis["category"],
                mutation_count=0,
            )
            return result
        has_patch = bool(str(new_content or "") or str(patch_find or ""))
        if not str(path or "").strip() or not has_patch:
            replay = self.replay(str(target.get("execution_id", "")))
            status = "already_fixed" if replay["semantic_validation_passed"] else "diagnosed"
            result = {
                "status": status,
                "execution_id": target.get("execution_id"),
                "diagnosis": diagnosis,
                "mutation_count": 0,
                "replay": replay,
            }
            result["audit"] = self._audit(
                execution_id=target.get("execution_id"),
                status=status,
                category=diagnosis["category"],
                mutation_count=0,
                replay_execution_id=replay.get("replay_execution_id"),
            )
            return result
        normalized_path = str(path).replace("\\", "/").strip()
        if normalized_path not in diagnosis["suggested_files"] and not any(
            normalized_path.startswith(value.rstrip("/") + "/") for value in diagnosis["suggested_files"]
        ):
            raise PermissionError("补丁目标不在结构化诊断给出的最小文件范围内")
        manager = ChangeManager()
        materialized, patch_mode = self._materialize_patch(
            manager,
            path=normalized_path,
            expected_sha256=expected_sha256,
            new_content=str(new_content),
            patch_find=str(patch_find),
            patch_replace=str(patch_replace),
        )
        tests = [
            "python -m compileall src",
            f"python -m src.manager.repair_verifier {diagnosis['category']}",
        ]
        change = manager.apply_text_change(
            normalized_path,
            materialized,
            reason=reason or f"incident {target.get('execution_id')}: {diagnosis['category']}",
            expected_sha256=expected_sha256,
            tests=tests,
        )
        if change.get("rolled_back"):
            result = {
                "status": "rolled_back",
                "execution_id": target.get("execution_id"),
                "diagnosis": diagnosis,
                "mutation_count": 1,
                "change": change,
                "semantic_validation_passed": False,
                "patch_mode": patch_mode,
            }
            result["audit"] = self._audit(
                execution_id=target.get("execution_id"),
                status="rolled_back",
                category=diagnosis["category"],
                mutation_count=1,
                target=normalized_path,
                change_id=change.get("change_id"),
                phase="target_verification",
            )
            return result
        replay: Dict[str, Any]
        rollback_error = ""
        try:
            replay = self.replay_fresh(str(target.get("execution_id", "")))
        except Exception as exc:
            replay = {
                "status": "error",
                "semantic_validation_passed": False,
                "error": str(exc)[:4000],
                "fresh_process": True,
            }
        if not replay.get("semantic_validation_passed"):
            try:
                manager.rollback(change, reason="原始请求的新进程语义回放未通过")
                status = "rolled_back"
            except Exception as exc:
                status = "rollback_failed"
                rollback_error = str(exc)[:4000]
        else:
            status = "verified_repair"
        result = {
            "status": status,
            "execution_id": target.get("execution_id"),
            "diagnosis": diagnosis,
            "mutation_count": 1,
            "change_id": change.get("change_id"),
            "semantic_validation_passed": bool(replay.get("semantic_validation_passed")),
            "replay": replay,
            "patch_mode": patch_mode,
            "rollback_error": rollback_error,
        }
        result["audit"] = self._audit(
            execution_id=target.get("execution_id"),
            status=status,
            category=diagnosis["category"],
            mutation_count=1,
            target=normalized_path,
            change_id=change.get("change_id"),
            replay_execution_id=replay.get("replay_execution_id"),
            semantic_validation_passed=bool(replay.get("semantic_validation_passed")),
            fresh_process=True,
            rollback_error=rollback_error,
        )
        return result


def patch_fingerprint(path: str, new_content: str) -> str:
    return hashlib.sha256(f"{Path(path).as_posix()}\0{new_content}".encode("utf-8")).hexdigest()
