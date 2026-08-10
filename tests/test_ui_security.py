"""Static UI security regressions for local control-panel pages."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "ui"


def test_all_markdown_renderers_are_sanitized():
    chat = (UI / "index.html").read_text(encoding="utf-8")
    macro = (UI / "macro.html").read_text(encoding="utf-8")

    assert "DOMPurify.sanitize" in chat
    assert "DOMPurify.sanitize" in macro
    assert "renderMarkdown(data.content || '')" in macro
    assert "marked.parse(data.content || '')" not in macro


def test_dashboard_and_settings_escape_dynamic_html_values():
    dashboard = (UI / "dashboard.html").read_text(encoding="utf-8")
    settings = (UI / "settings.html").read_text(encoding="utf-8")

    for value in ("h.market", "h.code", "h.name", "t.name", "t.action", "name"):
        assert f"escapeHtml({value})" in dashboard
    assert "function escapeHtml(value)" in settings
    assert "value=\"${currentModel}\"" not in settings
    assert "value=\"${currentBase}\"" not in settings
    assert "name.textContent = info.name" in settings
    assert "description.textContent = info.desc" in settings


def test_default_model_is_always_present_in_ui_catalog():
    chat = (UI / "index.html").read_text(encoding="utf-8")
    settings = (UI / "settings.html").read_text(encoding="utf-8")
    config = yaml.safe_load((ROOT / "config" / "config.yaml").read_text(encoding="utf-8"))

    expression = "[...new Set([cfg.model, ...(cfg.variants || [])].filter(Boolean))]"
    assert expression in chat
    assert expression in settings
    for provider in config["llm"]["models"].values():
        assert provider["model"] in provider.get("variants", [])
