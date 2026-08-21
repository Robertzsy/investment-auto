from __future__ import annotations

import json
import re
import shutil
import uuid
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from src.manager.skill_models import (
    SESSION_SCOPES,
    SIDE_EFFECT_LEVELS,
    SkillManifest,
    SkillPackage,
    WorkflowStep,
)
from src.paths import APP_ROOT, runtime_dir


BUILTIN_SKILL_DIR = APP_ROOT / "src" / "manager" / "builtin_skills"
CUSTOM_SKILL_DIR = runtime_dir() / "manager" / "skills"
_NAME = re.compile(r"^[a-z][a-z0-9-]{1,63}$")
_ACTION = re.compile(r"^[a-z][a-z0-9_.-]{1,95}$")


class SkillRegistry:
    """Load immutable built-ins plus runtime-created executable Skill packages."""

    def __init__(self, builtin_root: Optional[Path] = None, custom_root: Optional[Path] = None) -> None:
        self.builtin_root = (builtin_root or BUILTIN_SKILL_DIR).resolve()
        self.custom_root = (custom_root or CUSTOM_SKILL_DIR).resolve()

    @staticmethod
    def validate_manifest(manifest: SkillManifest) -> None:
        if not _NAME.fullmatch(manifest.name):
            raise ValueError("Skill 名称必须是 2-64 位小写字母、数字或连字符")
        if not manifest.description:
            raise ValueError("Skill description 不能为空")
        if manifest.session_scope not in SESSION_SCOPES:
            raise ValueError(f"Skill session_scope 无效: {manifest.session_scope}")
        if manifest.side_effect_level not in SIDE_EFFECT_LEVELS:
            raise ValueError(f"Skill side_effect_level 无效: {manifest.side_effect_level}")
        if not manifest.allowed_actions:
            raise ValueError("Skill allowed_actions 不能为空")
        invalid = [value for value in manifest.allowed_actions if not _ACTION.fullmatch(value)]
        if invalid:
            raise ValueError(f"Skill Action 名称无效: {invalid}")

    @staticmethod
    def validate_workflow(steps: List[WorkflowStep], manifest: SkillManifest) -> None:
        if not steps:
            raise ValueError("Skill workflow 至少需要一个步骤")
        ids = [step.step_id for step in steps]
        if len(ids) != len(set(ids)):
            raise ValueError("Skill workflow 步骤 id 不能重复")
        undeclared = [step.action for step in steps if step.action not in manifest.allowed_actions]
        if undeclared:
            raise ValueError(f"Workflow 使用了未授权 Action: {undeclared}")

    def _load(self, root: Path, source: str) -> SkillPackage:
        manifest_value = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
        workflow_value = json.loads((root / "workflow.json").read_text(encoding="utf-8"))
        contract_value = json.loads((root / "completion.schema.json").read_text(encoding="utf-8"))
        instructions = (root / "SKILL.md").read_text(encoding="utf-8")
        if not isinstance(manifest_value, Mapping) or not isinstance(workflow_value, Mapping):
            raise ValueError(f"Skill 文件格式无效: {root}")
        manifest = SkillManifest.from_mapping(manifest_value)
        steps = [
            WorkflowStep.from_mapping(item, index)
            for index, item in enumerate(workflow_value.get("steps", []))
            if isinstance(item, Mapping)
        ]
        self.validate_manifest(manifest)
        self.validate_workflow(steps, manifest)
        if not isinstance(contract_value, Mapping):
            raise ValueError("completion.schema.json 必须是 JSON object")
        return SkillPackage(
            manifest=manifest,
            instructions=instructions,
            steps=steps,
            completion_contract=dict(contract_value),
            root=str(root),
            source=source,
        )

    def catalog(self, *, enabled_only: bool = True) -> List[SkillPackage]:
        packages: Dict[str, SkillPackage] = {}
        for source, base in (("builtin", self.builtin_root), ("runtime", self.custom_root)):
            if not base.exists():
                continue
            for root in sorted(path for path in base.iterdir() if path.is_dir()):
                try:
                    package = self._load(root, source)
                    if enabled_only and not package.manifest.enabled:
                        continue
                    if package.manifest.name in packages and source == "runtime":
                        raise ValueError(f"运行时 Skill 不得覆盖内置 Skill: {package.manifest.name}")
                    packages[package.manifest.name] = package
                except (OSError, ValueError, json.JSONDecodeError):
                    continue
        return sorted(packages.values(), key=lambda item: (-item.manifest.priority, item.manifest.name))

    def get(self, name: str) -> SkillPackage:
        normalized = str(name or "").strip().lower()
        for package in self.catalog():
            if package.manifest.name == normalized:
                return package
        raise KeyError(f"Skill 不存在或不可用: {normalized}")

    def records(self) -> List[Dict[str, Any]]:
        return [package.catalog_record() for package in self.catalog()]

    def catalog_prompt(self) -> str:
        rows = []
        for package in self.catalog():
            manifest = package.manifest
            rows.append(
                f"- {manifest.name} [{manifest.session_scope}/{manifest.side_effect_level}]: "
                f"{manifest.description}"
            )
        return "\n".join(rows) if rows else "（没有可用 Skill）"

    def install(
        self,
        *,
        manifest: Mapping[str, Any],
        instructions: str,
        workflow: Mapping[str, Any],
        completion_contract: Mapping[str, Any],
    ) -> SkillPackage:
        parsed_manifest = SkillManifest.from_mapping(manifest)
        parsed_steps = [
            WorkflowStep.from_mapping(item, index)
            for index, item in enumerate(workflow.get("steps", []))
            if isinstance(item, Mapping)
        ]
        self.validate_manifest(parsed_manifest)
        self.validate_workflow(parsed_steps, parsed_manifest)
        if not instructions.strip():
            raise ValueError("SKILL.md instructions 不能为空")
        if not isinstance(completion_contract, Mapping):
            raise ValueError("completion_contract 必须是对象")
        if any(package.manifest.name == parsed_manifest.name for package in self.catalog(enabled_only=False)):
            raise FileExistsError(f"Skill 已存在，不允许静默覆盖: {parsed_manifest.name}")
        target = self.custom_root / parsed_manifest.name
        temporary = self.custom_root / f".{parsed_manifest.name}-{uuid.uuid4().hex}.tmp"
        temporary.mkdir(parents=True, exist_ok=False)
        try:
            (temporary / "manifest.json").write_text(
                json.dumps(parsed_manifest.to_dict(), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (temporary / "workflow.json").write_text(
                json.dumps({"steps": [step.to_dict() for step in parsed_steps]}, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            (temporary / "completion.schema.json").write_text(
                json.dumps(dict(completion_contract), ensure_ascii=False, indent=2), encoding="utf-8"
            )
            (temporary / "SKILL.md").write_text(instructions.strip() + "\n", encoding="utf-8")
            package = self._load(temporary, "runtime")
            target.parent.mkdir(parents=True, exist_ok=True)
            temporary.replace(target)
            return self._load(target, "runtime")
        except Exception:
            shutil.rmtree(temporary, ignore_errors=True)
            raise

    def uninstall_runtime(self, name: str) -> bool:
        normalized = str(name or "").strip().lower()
        if not _NAME.fullmatch(normalized):
            raise ValueError("Skill 名称无效")
        target = self.custom_root / normalized
        if not target.is_dir():
            return False
        shutil.rmtree(target)
        return True
