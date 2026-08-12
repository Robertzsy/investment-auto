"""Static UI security regressions for local control-panel pages."""
from __future__ import annotations

from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
UI = ROOT / "src" / "ui"


def test_all_markdown_renderers_are_sanitized():
    chat = (UI / "index.html").read_text(encoding="utf-8")
    macro = (UI / "macro.html").read_text(encoding="utf-8")
    dashboard = (UI / "dashboard.html").read_text(encoding="utf-8")

    assert "DOMPurify.sanitize" in chat
    assert "DOMPurify.sanitize" in macro
    assert "DOMPurify.sanitize" in dashboard
    assert "renderMarkdown(data.content || '')" in macro
    assert "renderMarkdown(preview)" in dashboard
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


def test_advanced_agent_and_hard_universe_controls_are_not_exposed():
    settings = (UI / "settings.html").read_text(encoding="utf-8")

    for removed in (
        "分阶段 Agent 研究工作流",
        "启用的分析角色",
        "AI 可交易标的",
        "agentWorkflowEnabled",
        "agentRoleCheckboxes",
        "universe_cn",
        "universe_hk",
        "universe_us",
        "universe_etf",
    ):
        assert removed not in settings


def test_market_context_is_embedded_in_dashboard_not_main_navigation():
    pages = {
        name: (UI / name).read_text(encoding="utf-8")
        for name in ("index.html", "dashboard.html", "settings.html", "macro.html")
    }

    assert "市场环境研判" in pages["dashboard.html"]
    assert "market-insight" in pages["dashboard.html"]
    assert "市场环境研判" in pages["macro.html"]
    for content in pages.values():
        assert "📰 宏观日报" not in content
