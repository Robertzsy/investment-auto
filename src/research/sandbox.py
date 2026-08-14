"""Restricted command sandbox for the offline research plane.

The trading path and the management conversation never get a shell.  The
research loop does, under these constraints:

* executable whitelist (python, pytest, git read-only, node scripts);
* no shell metacharacters - commands run as an argv list, so ; && | > cannot
  be interpreted;
* every path argument must resolve inside the project root (the workspace is
  inside it); escapes via .. or absolute paths outside are rejected;
* minimal environment - production secrets, database URIs and webhook URLs
  are never injected into child processes;
* hard timeout with process kill; truncated output.
"""

from __future__ import annotations

import os
import shlex
import subprocess
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

ROOT = Path(__file__).resolve().parents[2]

_EXECUTABLE_WHITELIST = {"python", "pytest", "git", "node"}
_GIT_READ_ONLY = {"diff", "status", "log", "show", "rev-parse", "branch"}
_SENSITIVE_ENV_MARKERS = (
    "API_KEY", "APY_KEY", "TOKEN", "SECRET", "PASSWORD", "WEBHOOK", "MONGODB_URI",
)
_MAX_OUTPUT_CHARS = 4000


def minimal_env() -> Dict[str, str]:
    """Child environment: system essentials plus no project secrets."""
    env: Dict[str, str] = {}
    for key in ("SYSTEMROOT", "PATH", "PATHEXT", "SystemRoot", "TEMP", "TMP", "LANG"):
        value = os.environ.get(key)
        if value:
            env[key] = value
    for key, value in os.environ.items():
        if key.startswith("DSH_"):
            env[key] = value
    # Children run with the workspace as cwd, so the project root must be
    # importable for pytest runs inside the bugfix task.
    env["PYTHONPATH"] = str(ROOT)
    return env


def _resolve_path_argument(value: str, workdir: Path) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = workdir / candidate
    return candidate.resolve()


def run_command(
    command: str,
    *,
    workdir: Path,
    timeout: int = 60,
    project_root: Optional[Path] = None,
) -> Dict[str, Any]:
    """Execute one whitelisted command inside the project boundary.

    Raises ValueError for rejected commands; never returns a nonzero exit
    silently - callers get exit_code and truncated output.
    """
    root = (project_root or ROOT).resolve()
    directory = Path(workdir).resolve()
    if directory != root and root not in directory.parents:
        raise ValueError("工作目录必须位于项目目录内")
    try:
        argv = shlex.split(str(command or ""))
    except ValueError as exc:
        raise ValueError(f"命令解析失败: {exc}") from exc
    if not argv:
        raise ValueError("命令不能为空")
    executable = Path(argv[0]).name.lower()
    if executable not in _EXECUTABLE_WHITELIST:
        raise ValueError(f"命令白名单仅允许: {', '.join(sorted(_EXECUTABLE_WHITELIST))}")

    # git is read-only for research agents.
    if executable == "git":
        if len(argv) < 2 or argv[1] not in _GIT_READ_ONLY:
            raise ValueError("git 仅允许只读子命令: " + ", ".join(sorted(_GIT_READ_ONLY)))

    # Path arguments must stay inside the project root.
    for argument in argv[1:]:
        text = str(argument)
        if not text or text.startswith("-"):
            continue
        if chr(92) in text or "/" in text or "." in text:
            resolved = _resolve_path_argument(text, directory)
            if resolved != root and root not in resolved.parents:
                raise ValueError(f"路径参数越出项目目录: {argument}")

    try:
        completed = subprocess.run(
            argv,
            cwd=str(directory),
            capture_output=True,
            timeout=max(1, int(timeout)),
            env=minimal_env(),
        )
    except subprocess.TimeoutExpired as exc:
        return {
            "exit_code": None,
            "timed_out": True,
            "stdout": "",
            "stderr": f"命令超时（{timeout}s）: {command[:200]}",
        }
    stdout = _decode(completed.stdout)
    stderr = _decode(completed.stderr)
    return {
        "exit_code": completed.returncode,
        "timed_out": False,
        "stdout": stdout[-_MAX_OUTPUT_CHARS:],
        "stderr": stderr[-_MAX_OUTPUT_CHARS:],
        "truncated": len(stdout) > _MAX_OUTPUT_CHARS or len(stderr) > _MAX_OUTPUT_CHARS,
    }


def _decode(raw: bytes) -> str:
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding, errors="strict")
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")
