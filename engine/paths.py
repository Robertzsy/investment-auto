"""Unified path authority: application (code) root vs user-data root.

Every module previously derived the project root from its own location
(Path(__file__).resolve().parents[2]) and then read BOTH code (scripts,
static pages) and user data (runtime, config, data, logs) from the same
tree.  The desktop app separates these:

* app root   = where the code/static files live (install dir, read-only)
* data root  = where user data lives (config, runtime, data, logs, workspace)

In development (no INVESTMENT_AUTO_DATA_DIR) the data root equals the app
root, so python -m src.main run from a checkout behaves exactly as before.
The installer sets INVESTMENT_AUTO_DATA_DIR to %LocalAppData%/InvestmentAuto.
"""

from __future__ import annotations

import os
from pathlib import Path

# Application root: src/paths.py lives directly under src/, so the
# project root is one level up (unlike modules under src/subdir/ that use
# parents[2]).
APP_ROOT = Path(__file__).resolve().parents[1]

_DATA_DIR_ENV = "INVESTMENT_AUTO_DATA_DIR"


def data_root() -> Path:
    """The user-data root.  Defaults to the app root in development."""
    value = os.getenv(_DATA_DIR_ENV)
    return Path(value).expanduser().resolve() if value else APP_ROOT


def runtime_dir() -> Path:
    return data_root() / "runtime"


def config_dir() -> Path:
    return data_root() / "config"


def data_dir() -> Path:
    return data_root() / "data"


def logs_dir() -> Path:
    return data_root() / "logs"


def workspace_dir() -> Path:
    return data_root() / "workspace"


def scripts_dir() -> Path:
    return APP_ROOT / "scripts"


def market_config_dir() -> Path:
    return config_dir() / "market"
