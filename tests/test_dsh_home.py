"""Engine tests for DSH home seeding (installed-desktop path)."""
from __future__ import annotations

import json

from engine import dsh_home


def _make_app(tmp_path):
    app = tmp_path / "app"
    (app / "profiles" / "investment-web").mkdir(parents=True)
    (app / "profiles" / "investment-web" / "cordis.patch.yml").write_text("- id: system-prompt\n  config:\n    persona: test\n", encoding="utf-8")
    (app / "presets" / "investment").mkdir(parents=True)
    (app / "presets" / "investment" / "preset.yml").write_text("name: 投资助手\n", encoding="utf-8")
    (app / "skills" / "security-analysis").mkdir(parents=True)
    (app / "skills" / "security-analysis" / "SKILL.md").write_text("---\nname: security-analysis\ndescription: test skill\n---\nbody\n", encoding="utf-8")
    plugin = app / "plugins" / "dsh-test-plugin"
    plugin.mkdir(parents=True)
    (plugin / "package.json").write_text(json.dumps({"name": "@investment-auto/dsh-test-plugin"}), encoding="utf-8")
    return app


def test_seed_populates_profiles_presets_skills_and_plugins(tmp_path):
    app = _make_app(tmp_path)
    home = tmp_path / "home"

    dsh_home.seed_dsh_home(app, home)

    assert (home / "profiles" / "investment-web" / "cordis.patch.yml").exists()
    assert (home / ".agent-presets" / "investment" / "preset.yml").exists()
    assert (home / "skills" / "security-analysis" / "SKILL.md").exists()
    plugin_target = home / "profiles" / "investment-web" / "node_modules" / "@investment-auto" / "dsh-test-plugin"
    assert (plugin_target / "package.json").exists()


def test_seed_never_overwrites_user_files_without_force(tmp_path):
    app = _make_app(tmp_path)
    home = tmp_path / "home"
    (home / "profiles" / "investment-web").mkdir(parents=True)
    user_patch = home / "profiles" / "investment-web" / "cordis.patch.yml"
    user_patch.write_text("# user-owned\n", encoding="utf-8")

    dsh_home.seed_dsh_home(app, home)
    assert user_patch.read_text(encoding="utf-8") == "# user-owned\n"

    dsh_home.seed_dsh_home(app, home, force=True)
    assert user_patch.read_text(encoding="utf-8") != "# user-owned\n"


def test_seed_from_env_requires_both_vars(monkeypatch, tmp_path):
    app = _make_app(tmp_path)
    monkeypatch.setenv("INVESTMENT_AUTO_APP_DIR", str(app))
    monkeypatch.delenv("DSH_HOME", raising=False)
    assert dsh_home.seed_from_env() is False

    monkeypatch.setenv("DSH_HOME", str(tmp_path / "home"))
    assert dsh_home.seed_from_env() is True
    assert (tmp_path / "home" / "profiles" / "investment-web").exists()


def test_seed_from_env_ignores_missing_app(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTMENT_AUTO_APP_DIR", str(tmp_path / "nope"))
    monkeypatch.setenv("DSH_HOME", str(tmp_path / "home"))
    assert dsh_home.seed_from_env() is False
