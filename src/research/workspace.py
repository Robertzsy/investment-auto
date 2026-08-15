"""Shared research workspace: the durable memory between fresh rounds.

Each run gets its own directory under runtime/research/workspace.  Rounds
write files and a structured report.json; the next round's prompt only ever
sees the workspace index plus the bounded summaries of previous reports -
never the previous agent's full conversation.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
from src.paths import runtime_dir
DEFAULT_WORKSPACE = runtime_dir() / "research" / "workspace"
TIMEZONE = ZoneInfo("Asia/Shanghai")


def _safe(value: Any, fallback: str = "value") -> str:
    safe = re.sub(r"[^A-Za-z0-9._-]+", "-", str(value or fallback).strip()).strip("-")[:64]
    return safe or fallback


class ResearchWorkspace:
    def __init__(self, root: Optional[Path] = None, run_id: str = "") -> None:
        self.root = (root or DEFAULT_WORKSPACE).resolve()
        self.run_id = _safe(run_id or datetime.now(TIMEZONE).strftime("%Y%m%d-%H%M%S"))
        self.run_dir = self.root / self.run_id

    def initialize(self, objective: str, task: str) -> None:
        self.run_dir.mkdir(parents=True, exist_ok=True)
        meta = {
            "run_id": self.run_id,
            "task": task,
            "objective": objective[:2000],
            "started_at": datetime.now(TIMEZONE).isoformat(timespec="seconds"),
        }
        self._atomic_write(self.run_dir / "meta.json", meta)

    def round_dir(self, round_no: int) -> Path:
        directory = self.run_dir / f"round-{int(round_no):02d}"
        directory.mkdir(parents=True, exist_ok=True)
        return directory

    def write_round_report(self, round_no: int, report: Mapping[str, Any]) -> Path:
        directory = self.round_dir(round_no)
        payload = dict(report)
        payload.setdefault("round", int(round_no))
        payload.setdefault("saved_at", datetime.now(TIMEZONE).isoformat(timespec="seconds"))
        path = directory / "report.json"
        self._atomic_write(path, payload)
        return path

    def read_round_reports(self, limit: int = 3) -> List[Dict[str, Any]]:
        reports: List[Dict[str, Any]] = []
        if not self.run_dir.exists():
            return reports
        for path in sorted(self.run_dir.glob("round-*/report.json")):
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            if isinstance(payload, dict):
                reports.append(payload)
        return reports[-max(1, limit):]

    def write_file(self, round_no: int, relative_path: str, content: str) -> Path:
        directory = self.round_dir(round_no)
        safe_relative = self._safe_relative(relative_path)
        path = directory / safe_relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        return path

    def read_file(self, relative_path: str) -> str:
        safe_relative = self._safe_relative(relative_path)
        candidates = []
        if str(safe_relative).startswith("round-"):
            candidates.append(self.run_dir / safe_relative)
        else:
            round_dirs = sorted(self.run_dir.glob("round-*")) if self.run_dir.exists() else []
            candidates.extend(round_dir / safe_relative for round_dir in reversed(round_dirs))
            candidates.append(self.run_dir / safe_relative)
        for candidate in candidates:
            if candidate.is_file():
                return candidate.read_text(encoding="utf-8")[:20000]
        raise FileNotFoundError(relative_path)

    def list_files(self) -> List[str]:
        if not self.run_dir.exists():
            return []
        return sorted(
            str(path.relative_to(self.run_dir)).replace(chr(92), "/")
            for path in self.run_dir.rglob("*") if path.is_file()
        )

    def index(self) -> Dict[str, Any]:
        files = self.list_files()
        return {
            "run_id": self.run_id,
            "workspace": str(self.run_dir),
            "files": files[:200],
            "reports": self.read_round_reports(3),
        }

    @staticmethod
    def _safe_relative(relative_path: str) -> Path:
        normalized = str(relative_path or "").replace(chr(92), "/")
        candidate = Path(normalized)
        if candidate.is_absolute() or ".." in candidate.parts:
            raise ValueError("研究工作区只允许相对路径，且不得包含 ..")
        return candidate

    @staticmethod
    def _atomic_write(path: Path, payload: Mapping[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(path.suffix + ".tmp")
        temporary.write_text(
            json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        temporary.replace(path)
