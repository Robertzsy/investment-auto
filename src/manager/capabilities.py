from __future__ import annotations

import importlib
import json
import re
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

from pydantic_ai import FunctionToolset, Tool


ROOT = Path(__file__).resolve().parents[2]
CAPABILITY_DIR = ROOT / "runtime" / "manager" / "capabilities"
SKILL_DIR = CAPABILITY_DIR / "skills"
TOOL_DIR = CAPABILITY_DIR / "tools"
_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_MODULE_RE = re.compile(r"^src(?:\.[A-Za-z_][A-Za-z0-9_]*)+$")
_FUNCTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    if not _NAME_RE.fullmatch(normalized):
        raise ValueError("能力名称必须是 2-64 位小写字母、数字或下划线，且以字母开头")
    return normalized


def _skill_name(value: str) -> str:
    normalized = re.sub(r"[^a-z0-9-]+", "-", str(value or "").strip().lower()).strip("-")
    if not re.fullmatch(r"^[a-z][a-z0-9-]{1,63}$", normalized):
        raise ValueError("Skill 名称必须是 2-64 位小写字母、数字或连字符")
    return normalized


def _write_json(path: Path, value: Mapping[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(path)


class CapabilityRegistry:
    """Persistent runtime registry for manager skills and typed project tools."""

    def __init__(self, root: Optional[Path] = None) -> None:
        self.root = (root or CAPABILITY_DIR).resolve()
        self.skill_dir = self.root / "skills"
        self.tool_dir = self.root / "tools"

    def install_skill(self, name: str, description: str, instructions: str) -> Dict[str, Any]:
        capability = _skill_name(name)
        description = str(description or "").strip()[:1000]
        instructions = str(instructions or "").strip()
        if not description or not instructions:
            raise ValueError("Skill 的 description 和 instructions 不能为空")
        payload = {
            "name": capability,
            "description": description,
            "instructions": instructions,
            "enabled": True,
        }
        target = self.skill_dir / capability / "SKILL.md"
        target.parent.mkdir(parents=True, exist_ok=True)
        safe_description = re.sub(r"\s+", " ", description)
        content = (
            f"---\nname: {capability}\n"
            f"description: {safe_description}\n---\n\n{instructions}\n"
        )
        temporary = target.with_suffix(".md.tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(target)
        return {"status": "installed", "kind": "skill", **payload}

    def load_skill(self, name: str) -> Dict[str, Any]:
        capability = _skill_name(name)
        path = self.skill_dir / capability / "SKILL.md"
        if not path.is_file():
            raise FileNotFoundError(f"Skill 不存在: {capability}")
        text = path.read_text(encoding="utf-8")
        description = re.search(r"^description:\s*(.+)$", text, flags=re.MULTILINE)
        instructions = text.split("---", 2)[-1].strip() if text.startswith("---") else text.strip()
        payload = {
            "name": capability,
            "description": description.group(1).strip() if description else capability,
            "instructions": instructions,
            "enabled": True,
        }
        return {"status": "loaded", "kind": "skill", **payload}

    def install_tool(
        self,
        name: str,
        description: str,
        module: str,
        function: str,
        parameters_schema: Mapping[str, Any] | str,
    ) -> Dict[str, Any]:
        capability = _name(name)
        description = str(description or "").strip()[:2000]
        module = str(module or "").strip()
        function = str(function or "").strip()
        if not description:
            raise ValueError("Tool description 不能为空")
        if not _MODULE_RE.fullmatch(module) or not _FUNCTION_RE.fullmatch(function):
            raise ValueError("Tool 只能引用 src 包内的 Python 函数")
        schema = json.loads(parameters_schema) if isinstance(parameters_schema, str) else dict(parameters_schema)
        if schema.get("type") != "object" or not isinstance(schema.get("properties", {}), Mapping):
            raise ValueError("parameters_schema 必须是 JSON object schema")
        target = getattr(importlib.import_module(module), function, None)
        if not callable(target):
            raise ValueError(f"目标函数不可调用: {module}.{function}")
        payload = {
            "name": capability,
            "description": description,
            "module": module,
            "function": function,
            "parameters_schema": schema,
            "enabled": True,
        }
        _write_json(self.tool_dir / f"{capability}.json", payload)
        return {"status": "installed_next_turn", "kind": "tool", **payload}

    def catalog(self) -> Dict[str, List[Dict[str, Any]]]:
        def records(directory: Path) -> List[Dict[str, Any]]:
            result = []
            for path in sorted(directory.glob("*.json")) if directory.exists() else []:
                try:
                    payload = json.loads(path.read_text(encoding="utf-8"))
                    if payload.get("enabled", True):
                        result.append(payload)
                except (OSError, json.JSONDecodeError, TypeError):
                    continue
            return result

        skills = []
        for path in sorted(self.skill_dir.glob("*/SKILL.md")) if self.skill_dir.exists() else []:
            try:
                skills.append(self.load_skill(path.parent.name) | {"status": "installed"})
            except (OSError, ValueError):
                continue
        return {"skills": skills, "tools": records(self.tool_dir)}

    def catalog_prompt(self) -> str:
        catalog = self.catalog()
        lines = []
        for skill in catalog["skills"]:
            lines.append(f"- Skill {skill.get('name')}: {skill.get('description')}")
        for tool in catalog["tools"]:
            lines.append(f"- Tool {tool.get('name')}: {tool.get('description')}")
        return "\n".join(lines) if lines else "（当前没有运行时扩展能力）"

    @staticmethod
    def _runtime_callable(module: str, function: str):
        def invoke(**kwargs: Any) -> Any:
            target = getattr(importlib.import_module(module), function)
            return target(**kwargs)

        return invoke

    def toolset(self, reserved: Optional[set[str]] = None) -> FunctionToolset[Any]:
        result = FunctionToolset(id="manager_runtime_capabilities")
        reserved = reserved or set()
        for manifest in self.catalog()["tools"]:
            try:
                name = _name(str(manifest.get("name", "")))
                if name in reserved:
                    continue
                tool = Tool.from_schema(
                    self._runtime_callable(str(manifest["module"]), str(manifest["function"])),
                    name=name,
                    description=str(manifest.get("description", "")),
                    json_schema=dict(manifest.get("parameters_schema", {})),
                    sequential=True,
                )
                result.add_tool(tool)
            except Exception:
                continue
        return result
