from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Mapping


SESSION_SCOPES = {
    "investment_research",
    "portfolio_management",
    "investment_execution",
    "system_admin",
}

SIDE_EFFECT_LEVELS = {"read_only", "portfolio_write", "investment_execution", "system_admin"}


@dataclass(frozen=True)
class SkillManifest:
    name: str
    description: str
    version: str
    intents: List[str]
    triggers: List[str]
    session_scope: str
    side_effect_level: str
    allowed_actions: List[str]
    priority: int = 0
    enabled: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> "SkillManifest":
        return cls(
            name=str(value.get("name", "")).strip(),
            description=str(value.get("description", "")).strip(),
            version=str(value.get("version", "1.0.0")).strip() or "1.0.0",
            intents=[str(item).strip() for item in value.get("intents", []) if str(item).strip()],
            triggers=[str(item).strip() for item in value.get("triggers", []) if str(item).strip()],
            session_scope=str(value.get("session_scope", "")).strip(),
            side_effect_level=str(value.get("side_effect_level", "read_only")).strip(),
            allowed_actions=[
                str(item).strip() for item in value.get("allowed_actions", []) if str(item).strip()
            ],
            priority=int(value.get("priority", 0) or 0),
            enabled=bool(value.get("enabled", True)),
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "version": self.version,
            "intents": list(self.intents),
            "triggers": list(self.triggers),
            "session_scope": self.session_scope,
            "side_effect_level": self.side_effect_level,
            "allowed_actions": list(self.allowed_actions),
            "priority": self.priority,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class WorkflowStep:
    step_id: str
    action: str
    required: bool = True
    retries: int = 0
    input_defaults: Dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], index: int) -> "WorkflowStep":
        return cls(
            step_id=str(value.get("id", f"step_{index + 1}")).strip() or f"step_{index + 1}",
            action=str(value.get("action", "")).strip(),
            required=bool(value.get("required", True)),
            retries=max(0, min(3, int(value.get("retries", 0) or 0))),
            input_defaults=dict(value.get("input_defaults", {}))
            if isinstance(value.get("input_defaults", {}), Mapping)
            else {},
        )

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.step_id,
            "action": self.action,
            "required": self.required,
            "retries": self.retries,
            "input_defaults": dict(self.input_defaults),
        }


@dataclass(frozen=True)
class SkillPackage:
    manifest: SkillManifest
    instructions: str
    steps: List[WorkflowStep]
    completion_contract: Dict[str, Any]
    root: str
    source: str

    def catalog_record(self) -> Dict[str, Any]:
        return {
            **self.manifest.to_dict(),
            "source": self.source,
            "root": self.root,
        }
