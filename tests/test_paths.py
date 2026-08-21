"""P0 path-separation tests: code root vs user-data root."""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from src import paths

ROOT = Path(__file__).resolve().parents[1]


def test_data_root_defaults_to_app_root():
    assert paths.data_root() == paths.APP_ROOT


def test_data_root_env_redirects_user_data(monkeypatch, tmp_path):
    monkeypatch.setenv("INVESTMENT_AUTO_DATA_DIR", str(tmp_path))
    assert paths.data_root() == tmp_path
    assert paths.runtime_dir() == tmp_path / "runtime"
    assert paths.config_dir() == tmp_path / "config"
    assert paths.data_dir() == tmp_path / "data"
    assert paths.logs_dir() == tmp_path / "logs"
    assert paths.workspace_dir() == tmp_path / "workspace"
    # code/static stays under the app root
    assert paths.scripts_dir() == paths.APP_ROOT / "scripts"
    assert paths.market_config_dir() == tmp_path / "config" / "market"


def test_relative_data_dir_resolves_against_cwd(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("INVESTMENT_AUTO_DATA_DIR", "relative-data")
    assert paths.data_root() == (tmp_path / "relative-data").resolve()


def test_module_constants_follow_data_root_in_fresh_process(monkeypatch, tmp_path):
    """Module-level path constants are bound at import time, so the data
    directory must be set before the process starts (the launcher does this).
    A fresh interpreter with the env set must bind constants to the data root."""
    data = tmp_path / "fresh-data"
    code = (
        "from src.portfolio import account as a\n"
        "from src import scheduler as s\n"
        "print(a.RUNTIME)\n"
        "print(s.SCHEDULER_LOCK)\n"
    )
    env = dict(os.environ)
    env["INVESTMENT_AUTO_DATA_DIR"] = str(data)
    result = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True, text=True, cwd=str(ROOT), env=env,
    )
    assert result.returncode == 0, result.stderr
    lines = result.stdout.strip().splitlines()
    assert Path(lines[0]) == data / "runtime" / "data"
    assert Path(lines[1]) == data / "runtime" / "scheduler.lock"
