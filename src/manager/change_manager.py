from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import threading
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from src.platform.memory_store import StructuredMemoryStore
from src.subprocess_utils import hidden_subprocess_kwargs


ROOT = Path(__file__).resolve().parents[2]
from src.paths import runtime_dir
BACKUP_DIR = runtime_dir() / "manager" / "change_backups"
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
            try:
                results = self._run_tests(commands)
            except Exception as exc:
                # A verifier launch/timeout failure is still a failed change.
                # Never leave the just-written file in place because the test
                # harness itself raised before returning a normal result.
                results = [{
                    "command": "test-harness",
                    "returncode": -1,
                    "stdout": "",
                    "stderr": _redact(str(exc)),
                }]
            passed = all(item["returncode"] == 0 for item in results)
            rolled_back = False
            if not passed:
                rolled_back = True
                if backup.exists():
                    shutil.copy2(backup, path)
                elif path.exists():
                    path.unlink()
            else:
                restart_path = runtime_dir() / "investment" / "restart_requested.json"
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
                "backup": str(backup.relative_to(BACKUP_DIR)).replace("\\", "/") if backup.exists() else None,
            })
            return record

    def rollback(self, change: Mapping[str, Any], *, reason: str) -> Dict[str, Any]:
        """Restore one verified change when its semantic replay does not pass."""
        target = _resolve_path(str(change.get("target", "")))
        expected_hash = str(change.get("new_sha256", ""))
        backup_value = str(change.get("backup") or "").replace("\\", "/")
        with _lock:
            current = target.read_text(encoding="utf-8") if target.exists() else ""
            current_hash = hashlib.sha256(current.encode("utf-8")).hexdigest()
            if expected_hash and current_hash != expected_hash:
                raise RuntimeError("补丁应用后文件又发生变化，拒绝覆盖并发修改")
            if backup_value:
                backup = (BACKUP_DIR / Path(backup_value)).resolve()
                backup_root = BACKUP_DIR.resolve()
                if backup_root not in backup.parents or not backup.is_file():
                    raise FileNotFoundError("修复补丁的可恢复备份不存在")
                target.parent.mkdir(parents=True, exist_ok=True)
                temporary = target.with_suffix(target.suffix + ".rollback.tmp")
                shutil.copy2(backup, temporary)
                temporary.replace(target)
            elif target.exists():
                target.unlink()
            restart_path = runtime_dir() / "investment" / "restart_requested.json"
            try:
                pending = json.loads(restart_path.read_text(encoding="utf-8"))
                if str(pending.get("change_id", "")) == str(change.get("change_id", "")):
                    restart_path.unlink()
            except (OSError, json.JSONDecodeError, AttributeError):
                pass
            return self.store.append("change_proposals", {
                "change_id": str(change.get("change_id", "")),
                "target": str(change.get("target", "")),
                "status": "semantic_replay_rolled_back",
                "rolled_back": True,
                "reason": str(reason)[:2000],
                "restored_at": _now(),
            })

    @staticmethod
    def _run_tests(commands: Iterable[str]) -> list[Dict[str, Any]]:
        # Use the interpreter that owns the running management service.  In a
        # source checkout this is the active virtual environment; in the
        # desktop edition it is the bundled Python runtime (there is no
        # project-root .venv in an installed application).
        python = sys.executable
        allowed = {
            "python -m pytest -q": [python, "-m", "pytest", "-q"],
            "python -m compileall src": [python, "-m", "compileall", "-q", "src"],
        }
        repair_categories = {
            "identity_mismatch", "stale_data", "data_gap", "contract_failure", "workflow_failure",
        }
        for category in repair_categories:
            allowed[f"python -m src.manager.repair_verifier {category}"] = [
                python, "-m", "src.manager.repair_verifier", category,
            ]
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
            completed = subprocess.run(
                argv,
                cwd=str(ROOT),
                capture_output=True,
                timeout=180,
                **hidden_subprocess_kwargs(),
            )
            results.append({
                "command": normalized,
                "returncode": completed.returncode,
                "stdout": _redact(completed.stdout.decode("utf-8", errors="replace")),
                "stderr": _redact(completed.stderr.decode("utf-8", errors="replace")),
            })
            if completed.returncode:
                break
        return results
