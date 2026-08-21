"""DSH bridge: the engine-side half of autonomous investment rounds (P3).

The engine scheduler calls the registered cycle runner at each round time.
This module builds the DSH headless bridge runner: it spawns a one-shot
`dsh --profile investment "<task>"` session (the same DSH app the desktop
conversation uses), which researches and submits decisions through the
`investment_*` tools; the runner then reads the engine-written audit for the
**authoritative** decisions/execution facts and returns the cycle result the
scheduler folds into the round report.

Fail-safe: fills only ever happen inside `submit_decisions` under the paper
broker's portfolio lock, and audits are atomically written — a crashed
headless run can lose a round but can never duplicate fills.
"""
from __future__ import annotations

import json
import logging
import os
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from engine.config import cfg
from engine.paths import APP_ROOT, runtime_dir

logger = logging.getLogger("investment-auto.dsh-bridge")

AUDIT_DIR = runtime_dir() / "trading" / "audit"
_MAX_REPORT_CHARS = 20000


def _bridge_config() -> Dict[str, Any]:
    return dict(cfg.autonomous.get("dsh_bridge", {}) or {})


def _resolve_app_dir() -> Optional[Path]:
    configured = os.getenv("INVESTMENT_AUTO_APP_DIR", "") or str(_bridge_config().get("app_dir", "") or "")
    if configured:
        path = Path(configured)
        if not path.is_absolute():
            path = APP_ROOT / path
        return path if (path / "node_modules" / "@deepseek-ai" / "dsh").exists() else None
    default = APP_ROOT / "app"
    return default if (default / "node_modules" / "@deepseek-ai" / "dsh").exists() else None


def _resolve_dsh_home(app_dir: Path) -> str:
    env_home = os.getenv("DSH_HOME", "")
    if env_home:
        return env_home
    configured = str(_bridge_config().get("dsh_home", "") or "")
    if configured:
        return configured
    dev_home = app_dir / "dev-home"
    return str(dev_home) if dev_home.exists() else ""


def _round_task(market: str, cycle_type: str, context: Mapping[str, Any]) -> str:
    catch_up = bool(context.get("catch_up"))
    scheduled_at = context.get("scheduled_at")
    schedule_text = scheduled_at.isoformat(timespec="minutes") if isinstance(scheduled_at, datetime) else str(scheduled_at or "")
    kind = "收盘复盘轮次" if cycle_type == "close" else "盘中轮次"
    return (
        f"你在执行 Investment Auto 2.0 的 {market.upper()} 市场{kind}（计划时间 {schedule_text}，"
        f"{'启动补跑' if catch_up else '准时运行'}）。\n\n"
        f"请按 complete-investment-cycle 的流程完成整轮：\n"
        f"1. investment_status 读取模式/暂停/紧急停止；紧急停止激活时停止并说明。\n"
        f"2. investment_screening({market}) 取候选池；investment_portfolio({market}) 与持仓快照确定现状；investment_mandate 读取授权书硬边界。\n"
        f"3. 对候选与持仓逐只用 investment_market_snapshot / investment_market_history 研究技术面，必要时 web_search 查事件；数据不足的标的放弃并说明。\n"
        f"4. 形成决策清单（BUY/SELL/HOLD + target_weight + confidence + reason），置信度不得低于授权书 min_confidence，"
        f"并用 investment_submit_decisions(market=\"{market}\", label=\"{context.get('label', 'auto')}\", note=分析摘要) 提交。\n"
        f"5. 引擎返回的成交/拒绝清单是唯一事实；不要编造引擎没有返回的成交，被拒原因如实记录。\n\n"
        f"最后用中文输出结构化总结：研究要点、提交的决策、成交与拒绝清单（以引擎返回为准）、风险提示与下一步计划。"
    )


def _latest_audit_since(market: str, label: str, since: float) -> Optional[Dict[str, Any]]:
    if not AUDIT_DIR.exists():
        return None
    candidates = []
    for path in AUDIT_DIR.glob("*.json"):
        try:
            if path.stat().st_mtime >= since and path.name.endswith(f"-{market}-{label}.json"):
                candidates.append(path)
        except OSError:
            continue
    if not candidates:
        return None
    latest = max(candidates, key=lambda path: path.stat().st_mtime)
    try:
        payload = json.loads(latest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"audit_file": str(latest), "error": f"审计不可读: {exc}"}
    if not isinstance(payload, Mapping):
        return {"audit_file": str(latest), "error": "审计结构异常"}
    return {"audit_file": str(latest), **dict(payload)}


class DshBridgeRunner:
    """Cycle runner: spawn a headless DSH round, then fold in engine truth."""

    def __init__(
        self,
        *,
        app_dir: Path,
        node: Optional[str] = None,
        dsh_home: str = "",
        timeout_seconds: Optional[int] = None,
    ) -> None:
        self.app_dir = Path(app_dir)
        self.bin = self.app_dir / "node_modules" / "@deepseek-ai" / "dsh" / "lib" / "bin.js"
        if not self.bin.exists():
            raise RuntimeError(f"DSH CLI not found under {self.app_dir}")
        self.node = node or shutil.which("node") or "node"
        self.dsh_home = dsh_home
        configured_timeout = int(_bridge_config().get("node_timeout_seconds", 0) or 0)
        self.timeout_seconds = int(timeout_seconds or configured_timeout or 1800)

    def __call__(self, market: str, cycle_type: str, context: Mapping[str, Any]) -> Dict[str, Any]:
        from engine.subprocess_utils import decode_subprocess_output, hidden_subprocess_kwargs

        label = str(context.get("label", "auto"))[:40]
        task = _round_task(market, cycle_type, context)
        env = dict(os.environ)
        env["DSH_TELEMETRY_DISABLED"] = "1"
        home = self.dsh_home or _resolve_dsh_home(self.app_dir)
        if home:
            env["DSH_HOME"] = home
        command = [self.node, str(self.bin), "--profile", "investment", task]
        start_monotonic = time.time()
        start_wall = time.time()
        logger.info("[DSH-BRIDGE:%s] spawning headless round (label=%s)", market, label)
        try:
            completed = subprocess.run(
                command,
                capture_output=True,
                timeout=self.timeout_seconds,
                env=env,
                **hidden_subprocess_kwargs(),
            )
        except subprocess.TimeoutExpired as exc:
            logger.exception("[DSH-BRIDGE:%s] headless round timed out", market)
            return {
                "status": "error",
                "error": f"DSH 轮次超时（>{self.timeout_seconds}s）",
                "stderr_tail": decode_subprocess_output(exc.stderr)[-1000:],
            }
        except OSError as exc:
            logger.exception("[DSH-BRIDGE:%s] headless spawn failed", market)
            return {"status": "error", "error": f"无法启动 DSH 进程: {exc}"}

        stdout = decode_subprocess_output(completed.stdout).strip()
        stderr = decode_subprocess_output(completed.stderr).strip()
        report_text = stdout[:_MAX_REPORT_CHARS]
        if completed.returncode != 0:
            logger.error("[DSH-BRIDGE:%s] headless round failed rc=%s: %s", market, completed.returncode, stderr[-800:])
            return {
                "status": "error",
                "error": f"DSH 轮次进程失败（rc={completed.returncode}）",
                "report_text": report_text or None,
                "stderr_tail": stderr[-1000:],
            }

        audit = _latest_audit_since(market, label, start_wall)
        result: Dict[str, Any] = {
            "status": "generated",
            "report_text": report_text or "DSH 轮次未输出总结。",
            "label": label,
            "elapsed_seconds": round(time.monotonic() - start_monotonic, 1),
        }
        if audit is None:
            result["warnings"] = ["轮次未产生决策审计（可能未提交决策或标的全部放弃）"]
            result["execution"] = {"fills": [], "rejected": []}
            result["decisions"] = []
        elif audit.get("error"):
            result["warnings"] = [str(audit.get("error"))]
            result["execution"] = {"fills": [], "rejected": []}
        else:
            result["decisions"] = audit.get("decisions", [])
            result["execution"] = audit.get("execution", {"fills": [], "rejected": []})
            result["risk"] = audit.get("risk", {})
            result["mandate"] = audit.get("mandate", {})
            result["audit_file"] = audit.get("audit_file")
        logger.info("[DSH-BRIDGE:%s] round finished: fills=%s", market, len(result.get("execution", {}).get("fills", [])))
        return result


def install_dsh_runner() -> bool:
    """Register the bridge runner on the scheduler when enabled and available."""
    if not bool(_bridge_config().get("enabled", True)):
        logger.info("[DSH-BRIDGE] disabled by autonomous.dsh_bridge.enabled=false")
        return False
    app_dir = _resolve_app_dir()
    if app_dir is None:
        logger.warning("[DSH-BRIDGE] DSH app dir not found (set INVESTMENT_AUTO_APP_DIR or autonomous.dsh_bridge.app_dir)")
        return False
    from engine.scheduler import set_cycle_runner

    set_cycle_runner(DshBridgeRunner(app_dir=app_dir))
    logger.info("[DSH-BRIDGE] cycle runner installed from %s", app_dir)
    return True
