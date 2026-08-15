"""Legacy-data migration tests (copy-only, backups, secret extraction)."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from src import migration, paths


def _legacy_tree(tmp_path: Path) -> Path:
    root = tmp_path / "legacy"
    (root / "src").mkdir(parents=True)
    (root / "src" / "main.py").write_text("# legacy", encoding="utf-8")
    (root / "config").mkdir()
    (root / "config" / "config.yaml").write_text("markets:\n  enable: [cn]\n", encoding="utf-8")
    (root / ".env").write_text(
        "DEEPSEEK_API_KEY=sk-legacy-secret\nPUBLIC_REGION=cn-east\n", encoding="utf-8"
    )
    (root / "runtime" / "data").mkdir(parents=True)
    (root / "runtime" / "data" / "portfolio.json").write_text("{}", encoding="utf-8")
    (root / "runtime" / "logs").mkdir(parents=True)
    (root / "runtime" / "logs" / "x.log").write_text("noise", encoding="utf-8")
    (root / "runtime" / "locks").mkdir()
    (root / "runtime" / "locks" / "cn.lock").write_text("1", encoding="utf-8")
    (root / "runtime" / "trading" / "checkpoints").mkdir(parents=True)
    (root / "runtime" / "trading" / "checkpoints" / "cycle.json").write_text("{}", encoding="utf-8")
    return root


def test_detect_and_plan_lists_items_only(monkeypatch, tmp_path):
    root = _legacy_tree(tmp_path)
    monkeypatch.setenv("INVESTMENT_AUTO_HOME", str(root))
    sources = migration.detect_sources()
    assert any(s["path"] == str(root) for s in sources)
    plan = migration.plan_migration(str(root))
    assert "config" in plan["items"]
    assert "env" in plan["items"]
    assert "runtime/data" in plan["items"]
    assert not any("logs" in item for item in plan["items"])
    assert not any("locks" in item for item in plan["items"])
    assert not any("checkpoints" in item for item in plan["items"])


def test_migration_copies_without_touching_source(monkeypatch, tmp_path):
    root = _legacy_tree(tmp_path)
    data_root = tmp_path / "data-root"
    monkeypatch.setattr(paths, "data_root", lambda: data_root)
    monkeypatch.setenv("INVESTMENT_AUTO_HOME", str(root))
    before = (root / "config" / "config.yaml").read_text(encoding="utf-8")
    result = migration.run_migration(str(root), ["config", "env", "runtime/data"])

    assert result["status"] == "migrated"
    assert "config" in result["copied"]
    assert (data_root / "config" / "config.yaml").exists()
    # source untouched
    assert (root / "config" / "config.yaml").read_text(encoding="utf-8") == before
    assert (root / "src" / "main.py").exists()


def test_migration_backs_up_existing_target(monkeypatch, tmp_path):
    root = _legacy_tree(tmp_path)
    data_root = tmp_path / "data-root"
    (data_root / "config").mkdir(parents=True)
    (data_root / "config" / "config.yaml").write_text("existing", encoding="utf-8")
    monkeypatch.setattr(paths, "data_root", lambda: data_root)
    result = migration.run_migration(str(root), ["config"])

    assert result["status"] == "migrated"
    backups = list((data_root / "runtime" / "backups" / "migrate").rglob("config.yaml"))
    assert len(backups) == 1
    assert backups[0].read_text(encoding="utf-8") == "existing"
    assert (data_root / "config" / "config.yaml").read_text(encoding="utf-8") != "existing"


@pytest.mark.skipif(os.name != "nt", reason="DPAPI is Windows-only")
def test_env_secrets_move_to_dpapi_and_plaintext_removed(monkeypatch, tmp_path):
    root = _legacy_tree(tmp_path)
    data_root = tmp_path / "data-root"
    monkeypatch.setattr(paths, "data_root", lambda: data_root)
    result = migration.run_migration(str(root), ["env"])

    assert "DEEPSEEK_API_KEY" in result["secrets_migrated"]
    from src import secret_store

    assert secret_store.load_secret("DEEPSEEK_API_KEY") == "sk-legacy-secret"
    copied_env = (data_root / ".env").read_text(encoding="utf-8")
    assert "sk-legacy-secret" not in copied_env  # plaintext gone
    assert "PUBLIC_REGION=cn-east" in copied_env  # non-secret lines kept
