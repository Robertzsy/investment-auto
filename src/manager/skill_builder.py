from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence

from src.manager.action_registry import ActionRegistry
from src.manager.skill_registry import SkillRegistry
from src.manager.skill_runtime import SkillRuntime
from src.paths import runtime_dir


class SkillBuilder:
    """Compile, test, register and optionally fulfill one Skill atomically."""

    def __init__(self, registry: Optional[SkillRegistry] = None) -> None:
        self.registry = registry or SkillRegistry()

    @staticmethod
    def _annotate_action(name: str, *, session_scope: str, side_effect_level: str) -> None:
        from src.manager.capabilities import CapabilityRegistry

        path = CapabilityRegistry().tool_dir / f"{name}.json"
        payload = json.loads(path.read_text(encoding="utf-8"))
        payload["session_scopes"] = [session_scope]
        payload["side_effect_level"] = side_effect_level
        temporary = path.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(path)

    def create(
        self,
        *,
        manifest: Mapping[str, Any],
        instructions: str,
        workflow: Mapping[str, Any],
        completion_contract: Mapping[str, Any],
        custom_actions: Sequence[Mapping[str, Any]] = (),
        test_inputs: Optional[Mapping[str, Any]] = None,
        fulfill_request: str = "",
        fulfill_inputs: Optional[Mapping[str, Any]] = None,
    ) -> Dict[str, Any]:
        from src.manager.tool_factory import create_manager_tool, uninstall_manager_tool_complete

        if test_inputs is None:
            raise ValueError("创建 Skill 必须提供 test_inputs；空参数 Skill 请显式传入 {}")
        from src.secret_store import redact_text

        for label, value in (
            ("description", str(manifest.get("description", ""))),
            ("instructions", instructions),
            ("custom_actions", json.dumps(list(custom_actions), ensure_ascii=False, default=str)),
        ):
            if redact_text(value) != value:
                raise ValueError(f"{label} 包含疑似密钥；Skill 文件只能引用密钥名称，不能保存密钥原文")
        session_scope = str(manifest.get("session_scope", "system_admin"))
        created_actions: List[str] = []
        installed_skill = ""
        action_results: List[Dict[str, Any]] = []
        try:
            for action in custom_actions:
                raw_name = str(action.get("name", "")).strip()
                if raw_name.startswith("custom."):
                    raw_name = raw_name.split(".", 1)[1]
                result = create_manager_tool(
                    name=raw_name,
                    description=str(action.get("description", "")),
                    code=str(action.get("code", "")),
                    function_name=str(action.get("function_name", "run")),
                    parameters_schema=action.get("parameters_schema", {}),
                    test_args=action.get("test_args") if isinstance(action.get("test_args"), Mapping) else {},
                    fulfill_args=None,
                    reason="skill-builder",
                    tests=[str(value) for value in action.get("tests", []) if str(value).strip()],
                    reserved_names={name.split(".")[-1] for name in ActionRegistry(include_dynamic=False).names},
                )
                if result.get("status") not in {"created", "created_and_fulfilled"}:
                    raise RuntimeError(f"自建 Action 失败: {result}")
                created_actions.append(raw_name)
                self._annotate_action(raw_name, session_scope=session_scope, side_effect_level="read_only")
                action_results.append(result)
            available = ActionRegistry(include_dynamic=True)
            missing = [
                str(value) for value in manifest.get("allowed_actions", [])
                if str(value) not in available.names
            ]
            if missing:
                raise ValueError(f"Skill 引用了不存在的 Action: {missing}")
            package = self.registry.install(
                manifest=manifest,
                instructions=instructions,
                workflow=workflow,
                completion_contract=completion_contract,
            )
            installed_skill = package.manifest.name
            trial_result: Dict[str, Any] = {}
            trial_result = SkillRuntime(
                registry=self.registry,
                actions=ActionRegistry(include_dynamic=True),
            ).run(
                f"验证新 Skill {installed_skill}",
                skill_name=installed_skill,
                inputs=test_inputs,
                requested_by="skill-builder-test",
            )
            if trial_result.get("status") != "completed":
                raise RuntimeError(f"Skill 试运行未通过完成契约: {trial_result.get('validation')}")
            fulfill_result: Dict[str, Any] = {}
            if fulfill_request:
                fulfill_result = SkillRuntime(
                    registry=self.registry,
                    actions=ActionRegistry(include_dynamic=True),
                ).run(
                    fulfill_request,
                    skill_name=installed_skill,
                    inputs=fulfill_inputs or test_inputs or {},
                    requested_by="skill-builder-fulfill",
                )
            fulfill_status = (
                "created_and_fulfilled"
                if fulfill_result.get("status") == "completed"
                else "created_fulfill_incomplete"
                if fulfill_result
                else "created"
            )
            return {
                "status": fulfill_status,
                "skill": package.catalog_record(),
                "custom_actions": action_results,
                "test_result": trial_result,
                "fulfill_result": fulfill_result,
            }
        except Exception:
            if installed_skill:
                try:
                    self.registry.uninstall_runtime(installed_skill)
                except Exception:
                    pass
            for name in reversed(created_actions):
                try:
                    uninstall_manager_tool_complete(name)
                except Exception:
                    pass
            raise


def parse_json_object(value: str, label: str, *, default: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if not str(value or "").strip():
        return dict(default or {})
    parsed = json.loads(value)
    if not isinstance(parsed, Mapping):
        raise ValueError(f"{label} 必须是 JSON object")
    return dict(parsed)


def parse_json_list(value: str, label: str) -> List[Dict[str, Any]]:
    if not str(value or "").strip():
        return []
    parsed = json.loads(value)
    if not isinstance(parsed, list) or not all(isinstance(item, Mapping) for item in parsed):
        raise ValueError(f"{label} 必须是 JSON object 数组")
    return [dict(item) for item in parsed]
