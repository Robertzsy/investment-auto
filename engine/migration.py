r"""Legacy-data migration for the first-run wizard (step 0).

Detects a previous checkout (D:\investment-auto or INVESTMENT_AUTO_HOME),
plans which user data can be imported, copies it into the desktop data
root - never moves or deletes the source - and backs up any target files
it would overwrite.  Logs, locks and transient checkpoints are excluded.
Secrets extracted from the legacy .env go into the DPAPI store instead of
the plain-text environment file.
"""

from __future__ import annotations

import json
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

from engine import paths

TIMEZONE = ZoneInfo("Asia/Shanghai")

# runtime subtrees worth importing (everything else is transient)
_RUNTIME_ITEMS = (
    "data",
    "reports",
    "memory",
    "screener",
    "optimizer",
    "investment/mandate.json",
    "investment/bus",           # command queue continuity is not needed; skip
)
# refine: bus is transient; mandate is needed
_RUNTIME_INCLUDE = (
    "data",
    "reports",
    "memory",
    "screener",
    "optimizer",
    "investment/mandate.json",
    "trading/audit",
    "trading/agent_memory",
    "manager/capabilities",     # self-created tools/skills
    "manager/skills",           # executable runtime-created Skill packages
    "manager/sessions",         # persistent domain-session state
    "manager/skill_schedules",  # direct structured Skill schedules
    "manager/trajectories",     # auditable Skill execution traces
    "research/workspace",
)
_RUNTIME_EXCLUDE = ("logs", "locks", "checkpoints", "agent_failures", "bus", "launcher")


def detect_sources() -> List[Dict[str, Any]]:
    """Find candidate legacy project roots."""
    candidates: List[Path] = []
    env_home = __import__("os").getenv("INVESTMENT_AUTO_HOME", "")
    if env_home:
        candidates.append(Path(env_home))
    candidates.append(Path(r"D:\investment-auto"))
    found = []
    for candidate in candidates:
        if (candidate / "src" / "main.py").exists() and candidate.resolve() != paths.APP_ROOT:
            found.append({"path": str(candidate), "name": str(candidate)})
    return found


def plan_migration(source: str) -> Dict[str, Any]:
    """List what could be imported from a legacy checkout."""
    root = Path(source)
    items = []
    if (root / "config" / "config.yaml").exists():
        items.append("config")
    if (root / ".env").exists():
        items.append("env")
    for item in _RUNTIME_INCLUDE:
        if (root / "runtime" / item).exists():
            items.append("runtime/" + item)
    return {"source": str(root), "items": items, "excluded": sorted(_RUNTIME_EXCLUDE)}


def run_migration(source: str, items: Optional[List[str]] = None) -> Dict[str, Any]:
    """Copy selected items into the data root; never touches the source.

    Any target file that would be overwritten is backed up under
    runtime/backups/migrate/<timestamp>/ first.
    """
    root = Path(source)
    if not (root / "src" / "main.py").exists():
        raise ValueError(f"源目录不是 investment-auto 项目: {source}")
    target_root = paths.data_root()
    if root.resolve() == target_root.resolve():
        return {"status": "skipped", "reason": "源目录就是当前数据目录"}

    selected = [str(item).strip() for item in (items or []) if str(item).strip()]
    if not selected:
        selected = plan_migration(str(root))["items"]
    timestamp = datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S")
    backup_root = paths.runtime_dir() / "backups" / "migrate" / timestamp
    copied: List[str] = []
    skipped: List[str] = []
    secrets_migrated: List[str] = []
    portfolio_repaired = False

    for item in selected:
        # "env" maps to the hidden .env file at the project root.
        if item == "env":
            source_path = root / ".env"
            target_path = target_root / ".env"
        else:
            source_path = root / item
            target_path = target_root / item
        if not source_path.exists():
            skipped.append(item)
            continue
        # backup anything that would be overwritten
        if target_path.exists():
            backup_path = backup_root / item
            backup_path.parent.mkdir(parents=True, exist_ok=True)
            if target_path.is_dir():
                shutil.copytree(target_path, backup_path, dirs_exist_ok=True)
            else:
                shutil.copy2(target_path, backup_path)
        target_path.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            shutil.copytree(source_path, target_path, dirs_exist_ok=True)
        else:
            shutil.copy2(source_path, target_path)
        copied.append(item)

    # A legacy tree can contain a syntactically valid but incomplete account
    # shell such as {"accounts": {}}.  Normalize it immediately so running a
    # cycle before reopening the setup wizard cannot persist zero-capital
    # market accounts.
    migrated_portfolio = target_root / "runtime" / "data" / "portfolio.json"
    if "runtime/data" in copied and migrated_portfolio.exists():
        from engine.portfolio.account import normalize_portfolio

        raw_portfolio = json.loads(migrated_portfolio.read_text(encoding="utf-8"))
        normalized_portfolio = normalize_portfolio(raw_portfolio)
        if normalized_portfolio != raw_portfolio:
            temporary = migrated_portfolio.with_suffix(migrated_portfolio.suffix + ".tmp")
            temporary.write_text(
                json.dumps(normalized_portfolio, ensure_ascii=False, indent=2),
                encoding="utf-8",
            )
            temporary.replace(migrated_portfolio)
            portfolio_repaired = True

    # Legacy .env secrets -> DPAPI store; plain-text keys never land in .env.
    if "env" in selected or not selected:
        secrets_migrated = _migrate_env_secrets(root, target_root)

    return {
        "status": "migrated",
        "source": str(root),
        "copied": copied,
        "skipped": skipped,
        "backup": str(backup_root),
        "secrets_migrated": secrets_migrated,
        "portfolio_repaired": portfolio_repaired,
    }


# Env vars that must never survive migration: secrets live in DPAPI, and
# machine-specific paths would point the desktop app back at the old machine.
_SECRET_MARKERS = ("API_KEY", "APY_KEY", "TOKEN", "SECRET", "WEBHOOK", "URI", "PASSWORD", "PASSWD", "CREDENTIAL")
_MACHINE_SPECIFIC_ENV = {"CONFIG_PATH", "INVESTMENT_AUTO_HOME", "IA_ACCESS_TOKEN"}


def _migrate_env_secrets(source: Path, target_root: Path) -> List[str]:
    """Extract API keys from the legacy .env into the DPAPI store."""
    from engine.secret_store import save_secret

    env_file = source / ".env"
    if not env_file.exists():
        return []
    migrated = []
    key_re = re.compile(r"^([A-Z][A-Z0-9_]*)\s*=\s*(.+)$")
    for line in env_file.read_text(encoding="utf-8", errors="replace").splitlines():
        match = key_re.match(line.strip())
        if not match:
            continue
        name, value = match.group(1), match.group(2).strip()
        if not value:
            continue
        if any(marker in name.upper() for marker in _SECRET_MARKERS):
            try:
                save_secret(name, value)
                migrated.append(name)
            except Exception:
                continue
    # Remove secrets and machine-specific paths from the copied .env:
    # secrets now live in DPAPI, and a stale CONFIG_PATH would redirect the
    # desktop app config loading back to the old machine checkout.
    target_env = target_root / ".env"
    if target_env.exists():
        filtered = []
        for line in target_env.read_text(encoding="utf-8", errors="replace").splitlines():
            match = key_re.match(line.strip())
            drop = False
            if match and match.group(2).strip():
                name_upper = match.group(1).upper()
                drop = name_upper in _MACHINE_SPECIFIC_ENV or any(
                    marker in name_upper for marker in _SECRET_MARKERS
                )
            if not drop:
                filtered.append(line)
        target_env.write_text("\n".join(filtered) + "\n", encoding="utf-8")
    return migrated
