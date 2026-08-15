from __future__ import annotations

import json

from src.manager.capabilities import CapabilityRegistry


def test_runtime_skill_install_and_load(tmp_path):
    registry = CapabilityRegistry(tmp_path / "capabilities")
    installed = registry.install_skill("config_editor", "Edit configuration", "Search before editing.")

    assert installed["status"] == "installed"
    assert registry.load_skill("config-editor")["instructions"] == "Search before editing."
    assert "config-editor" in registry.catalog_prompt()
    assert (registry.skill_dir / "config-editor" / "SKILL.md").is_file()


def test_runtime_tool_registers_project_callable(tmp_path):
    registry = CapabilityRegistry(tmp_path / "capabilities")
    installed = registry.install_tool(
        "security_search", "Search securities", "src.ui.agent_runtime", "search_security",
        {"type": "object", "properties": {"keyword": {"type": "string"}}, "required": ["keyword"]},
    )

    assert installed["status"] == "installed_next_turn"
    manifest = json.loads((registry.tool_dir / "security_search.json").read_text(encoding="utf-8"))
    assert manifest["module"] == "src.ui.agent_runtime"
    assert "security_search" in registry.toolset().tools
