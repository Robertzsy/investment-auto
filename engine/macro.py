from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

from engine.config import cfg
from engine.runtime_lock import atomic_claim
from engine.subprocess_utils import decode_subprocess_output, hidden_subprocess_kwargs

ROOT = Path(__file__).resolve().parent.parent
from engine.paths import runtime_dir
DATA_ROOT = runtime_dir() / "macro"
SCRIPT = ROOT / "scripts" / "macro-environment" / "run.js"
logger = logging.getLogger("investment-auto.macro")


def _now(value: Optional[datetime] = None) -> datetime:
    if value is not None:
        return value
    return datetime.now(ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai")))


def report_date(value: Optional[datetime] = None) -> str:
    return (_now(value).date() - timedelta(days=1)).isoformat()


def report_path(date: Optional[str] = None, data_root: Optional[Path] = None) -> Path:
    root = data_root or DATA_ROOT
    return root / "daily" / f"{date or report_date()}.md"


def latest_dates(data_root: Optional[Path] = None) -> List[str]:
    root = data_root or DATA_ROOT
    dates = set()
    for directory in (root / "daily", root / "news"):
        if not directory.exists():
            continue
        suffix = ".md" if directory.name == "daily" else ".json"
        for path in directory.glob(f"*{suffix}"):
            if len(path.stem) == 10 and path.stem[4] == "-" and path.stem[7] == "-":
                dates.add(path.stem)
    return sorted(dates, reverse=True)


def _external_macro_roots() -> List[Path]:
    roots: List[Path] = []
    configured = os.getenv("MACRO_SOURCE_DIR")
    if configured:
        roots.append(Path(configured))

    local_openclaw = Path.home() / ".openclaw" / "workspace" / "data" / "macro"
    roots.append(local_openclaw)

    if os.name == "nt":
        # Discover WSL OpenClaw workspaces without hard-coding a distro or user.
        wsl_root = Path(r"\\wsl.localhost")
        try:
            for distro in wsl_root.iterdir():
                home = distro / "home"
                if not home.exists():
                    continue
                for user_home in home.iterdir():
                    roots.append(user_home / ".openclaw" / "workspace" / "data" / "macro")
        except OSError:
            pass

    unique: List[Path] = []
    seen = set()
    for root in roots:
        key = str(root)
        if key not in seen:
            seen.add(key)
            unique.append(root)
    return unique


def sync_external_report(date: Optional[str] = None, data_root: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    target_root = data_root or DATA_ROOT
    target_date = date or report_date()
    target_report = report_path(target_date, target_root)
    if target_report.exists():
        return {"status": "exists", "date": target_date, "report": str(target_report)}

    for source_root in _external_macro_roots():
        try:
            source_report = source_root / "daily" / f"{target_date}.md"
            source_news = source_root / "news" / f"{target_date}.json"
            if not source_report.exists() and not source_news.exists():
                continue
            copied = []
            if source_report.exists():
                target_report.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_report, target_report)
                copied.append(str(target_report))
            if source_news.exists():
                target_news = target_root / "news" / source_news.name
                target_news.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy2(source_news, target_news)
                copied.append(str(target_news))
            marks = target_root / "marks.json"
            if target_report.exists():
                current = {}
                if marks.exists():
                    try:
                        current = json.loads(marks.read_text(encoding="utf-8"))
                    except Exception:
                        current = {}
                current["daily"] = target_date
                marks.parent.mkdir(parents=True, exist_ok=True)
                marks.write_text(json.dumps(current, ensure_ascii=False, indent=2), encoding="utf-8")
                return {"status": "synced", "date": target_date, "source": str(source_root), "copied": copied}
            # A JSON-only source is useful as a cache, but is not a completed
            # daily report; the native generator will continue below.
        except OSError:
            continue
    return None


def run_daily(*, force: bool = False, now: Optional[datetime] = None, data_root: Optional[Path] = None) -> Dict[str, Any]:
    root = data_root or DATA_ROOT
    current = _now(now)
    target_date = report_date(current)
    target_report = report_path(target_date, root)
    lock_path = target_report.with_suffix(target_report.suffix + ".lock")

    with atomic_claim(lock_path, stale_seconds=int(cfg.schedule.get("macro_timeout_seconds", 240)) + 300) as claimed:
        if not claimed:
            if target_report.exists():
                return {"status": "exists", "date": target_date, "report": str(target_report)}
            return {"status": "in_progress", "date": target_date, "report": str(target_report)}
        if target_report.exists() and not force:
            return {"status": "exists", "date": target_date, "report": str(target_report)}

        if not force:
            synced = sync_external_report(target_date, root)
            if synced:
                logger.info("Macro daily report synced for %s", target_date)
                return synced

        if not SCRIPT.exists():
            raise FileNotFoundError(f"宏观日报脚本不存在: {SCRIPT}")
        node = shutil.which("node")
        if not node:
            raise RuntimeError("未找到 Node.js，无法生成宏观日报")
        proxy_enabled = any(os.getenv(name) for name in ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy"))
        if proxy_enabled and not shutil.which("curl"):
            raise RuntimeError("检测到 HTTP(S) 代理，但未找到 curl；请安装 curl 或取消代理后重试")

        env = os.environ.copy()
        env["MACRO_DATA_DIR"] = str(root)
        for directory in ("news", "daily", "weekly", "monthly", "yearly"):
            (root / directory).mkdir(parents=True, exist_ok=True)
        reference_date = current.date().isoformat()
        logger.info("Generating macro daily report for %s", target_date)
        process = subprocess.run(
            [node, str(SCRIPT), "daily", "--date", reference_date],
            cwd=str(ROOT),
            env=env,
            capture_output=True,
            timeout=int(cfg.schedule.get("macro_timeout_seconds", 240)),
            **hidden_subprocess_kwargs(),
        )
        stdout = decode_subprocess_output(process.stdout)
        stderr = decode_subprocess_output(process.stderr)
        if process.returncode != 0:
            raise RuntimeError(f"宏观日报生成失败: {(stderr or stdout)[-2000:]}")
        if not target_report.exists():
            raise RuntimeError(f"宏观日报命令成功但未生成目标日期文件: {target_report}")
        logger.info("Macro daily report generated: %s", target_report)
        return {
            "status": "generated",
            "date": target_date,
            "report": str(target_report),
            "log_tail": (stderr or stdout)[-1200:],
        }
