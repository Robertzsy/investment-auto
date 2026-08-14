from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from src.platform.memory_store import StructuredMemoryStore


ROOT = Path(__file__).resolve().parents[2]
BACKUP_DIR = ROOT / "runtime" / "manager" / "change_backups"
ALLOWED_ROOTS = (
    ROOT,
)
_lock = threading.RLock()


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _resolve_path(value: str) -> Path:
    raw = str(value or "").strip().replace("\\", "/")
    if not raw or Path(raw).is_absolute() or ".." in Path(raw).parts:
        raise ValueError("只能使用项目内相对路径")
    path = (ROOT / raw).resolve()
    if not any(path == allowed or allowed in path.parents for allowed in ALLOWED_ROOTS):
        raise PermissionError("管理 AI 只能操作当前项目目录内的文件")
    return path


def _redact(text: str) -> str:
    result = text
    for key, value in os.environ.items():
        if value and any(marker in key.upper() for marker in ("KEY", "TOKEN", "SECRET", "PASSWORD", "WEBHOOK", "MONGODB")):
            result = result.replace(value, "***")
    return result[:12000]


class ChangeManager:
    """Versioned project changes with tests and automatic rollback."""

    def __init__(self, store: Optional[StructuredMemoryStore] = None) -> None:
        self.store = store or StructuredMemoryStore()

    def inspect(self, relative_path: str, *, max_chars: int = 24000) -> Dict[str, Any]:
        path = _resolve_path(relative_path)
        if not path.is_file():
            raise FileNotFoundError(relative_path)
        content = path.read_text(encoding="utf-8")
        return {
            "path": str(path.relative_to(ROOT)).replace("\\", "/"),
            "sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
            "content": content[:max(100, min(max_chars, 50000))],
            "truncated": len(content) > max_chars,
        }

    def apply_text_change(
        self,
        relative_path: str,
        new_content: str,
        *,
        reason: str,
        expected_sha256: str = "",
        tests: Sequence[str] = (),
    ) -> Dict[str, Any]:
        path = _resolve_path(relative_path)
        content = str(new_content)
        with _lock:
            previous = path.read_text(encoding="utf-8") if path.exists() else ""
            previous_hash = hashlib.sha256(previous.encode("utf-8")).hexdigest()
            if expected_sha256 and expected_sha256 != previous_hash:
                raise RuntimeError("文件在读取后已发生变化，请重新检查再修改")
            change_id = uuid.uuid4().hex
            backup = BACKUP_DIR / change_id / str(path.relative_to(ROOT))
            backup.parent.mkdir(parents=True, exist_ok=True)
            if path.exists():
                shutil.copy2(path, backup)
            path.parent.mkdir(parents=True, exist_ok=True)
            temporary = path.with_suffix(path.suffix + ".manager.tmp")
            temporary.write_text(content, encoding="utf-8")
            temporary.replace(path)
            commands = list(tests) or ["python -m pytest -q"]
            results = self._run_tests(commands)
            passed = all(item["returncode"] == 0 for item in results)
            rolled_back = False
            if not passed:
                rolled_back = True
                if backup.exists():
                    shutil.copy2(backup, path)
                elif path.exists():
                    path.unlink()
            else:
                restart_path = ROOT / "runtime" / "investment" / "restart_requested.json"
                restart_path.parent.mkdir(parents=True, exist_ok=True)
                restart_path.write_text(json.dumps({
                    "change_id": change_id,
                    "target": str(path.relative_to(ROOT)).replace("\\", "/"),
                    "requested_at": _now(),
                }, ensure_ascii=False, indent=2), encoding="utf-8")
            record = self.store.append("change_proposals", {
                "change_id": change_id,
                "target": str(path.relative_to(ROOT)).replace("\\", "/"),
                "reason": str(reason)[:4000],
                "previous_sha256": previous_hash,
                "new_sha256": hashlib.sha256(content.encode("utf-8")).hexdigest(),
                "tests": results,
                "status": "verified_restart_requested" if passed else "rolled_back",
                "rolled_back": rolled_back,
                "backup": str(backup.relative_to(ROOT)).replace("\\", "/") if backup.exists() else None,
            })
            return record

    @staticmethod
    def _run_tests(commands: Iterable[str]) -> list[Dict[str, Any]]:
        python = str(ROOT / ".venv" / "Scripts" / "python.exe")
        allowed = {
            "python -m pytest -q": [python, "-m", "pytest", "-q"],
            "python -m compileall src": [python, "-m", "compileall", "-q", "src"],
        }
        results = []
        for command in commands:
            normalized = re.sub(r"\s+", " ", str(command).strip())
            argv = allowed.get(normalized)
            if argv is None:
                # Whitelisted subset: python -m pytest -q <project test file(s)>.
                match = re.fullmatch(r"python -m pytest -q ([A-Za-z0-9_./\\-]+)", normalized)
                if match:
                    parts = [part for part in re.split(r"[\\/]", match.group(1)) if part]
                    candidate = (ROOT / Path(*parts)).resolve()
                    if candidate.exists() and any(ROOT / "tests" in path.parents or path == ROOT / "tests" for path in [candidate]):
                        argv = [python, "-m", "pytest", "-q", str(candidate.relative_to(ROOT)).replace("\\", "/")]
            if argv is None:
                raise ValueError(f"不允许的测试命令: {command}")
            completed = subprocess.run(argv, cwd=str(ROOT), capture_output=True, timeout=180)
            results.append({
                "command": normalized,
                "returncode": completed.returncode,
                "stdout": _redact(completed.stdout.decode("utf-8", errors="replace")),
                "stderr": _redact(completed.stderr.decode("utf-8", errors="replace")),
            })
            if completed.returncode:
                break
        return results
