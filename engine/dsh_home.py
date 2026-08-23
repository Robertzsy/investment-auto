"""DSH home seeding for the installed desktop product.

The installer ships app/ (profiles, presets, skills, plugins) beside the
engine; on startup the engine seeds them into $DSH_HOME (the user data
root). Pure Python — the installed product never runs PowerShell. Existing
user files are never overwritten unless force=True (mirrors seed.ps1).

Development keeps app/scripts/seed.ps1 for app/dev-home; the same layout
contract applies.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger("investment-auto.dsh-home")


def _copy_tree(source: Path, target: Path, *, force: bool) -> None:
    if not source.exists():
        return
    target.mkdir(parents=True, exist_ok=True)
    for child in source.iterdir():
        destination = target / child.name
        if destination.exists() and not force:
            continue
        if child.is_dir():
            shutil.copytree(child, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(child, destination)


def _install_plugins(plugins_root: Path, profiles_root: Path, *, force: bool) -> None:
    if not plugins_root.exists():
        return
    for plugin in plugins_root.iterdir():
        if not plugin.is_dir():
            continue
        manifest_path = plugin / "package.json"
        if not manifest_path.exists():
            continue
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        name = str(manifest.get("name", ""))
        if "/" not in name:
            logger.warning("plugin %s has no scoped package name; skipped", plugin.name)
            continue
        for profile in profiles_root.iterdir():
            if not profile.is_dir() or profile.name == "node_modules":
                continue
            destination = profile / "node_modules" / name
            if destination.exists() and not force:
                continue
            if destination.exists():
                shutil.rmtree(destination)
            shutil.copytree(plugin, destination)


def seed_dsh_home(app_dir: str | Path, dsh_home: str | Path, *, force: bool = False) -> Dict[str, Any]:
    """Seed profiles/presets/skills/plugins from app/ into DSH_HOME."""
    app = Path(app_dir)
    home = Path(dsh_home)
    result: Dict[str, List[str]] = {"seeded": [], "kept": []}
    _copy_tree(app / "profiles", home / "profiles", force=force)
    _copy_tree(app / "presets", home / ".agent-presets", force=force)
    _copy_tree(app / "skills", home / "skills", force=force)
    _install_plugins(app / "plugins", home / "profiles", force=force)
    result["seeded"] = sorted(str(path.relative_to(home)) for path in home.rglob("*") if path.is_dir() and path.name == "investment")
    logger.info("DSH home seeded: %s", home)
    return result


def seed_from_env() -> bool:
    """Seed from INVESTMENT_AUTO_APP_DIR/DSH_HOME when the app dir exists.

    The installed product owns profiles/presets/skills/plugins (the product
    exposes no editor for them; the user's override layer is the home-level
    cordis.patch.yml, which is never seeded). Seeding therefore refreshes the
    shipped trees on every engine start so product updates — e.g. the 2.1
    product-shell profile composition — reach existing installations. Extra
    user-created entries (extra profiles, presets, skills) are left alone;
    user DATA (sessions, workspaces, credentials, config) lives outside these
    trees and is never touched.
    """
    app_dir = os.getenv("INVESTMENT_AUTO_APP_DIR", "").strip()
    dsh_home = os.getenv("DSH_HOME", "").strip()
    if not app_dir or not dsh_home:
        return False
    app_path = Path(app_dir)
    if not (app_path / "profiles").exists():
        return False
    seed_dsh_home(app_path, dsh_home, force=True)
    return True
