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
import re
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Sequence

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
        f"你在执行 Investment Auto 2.1 的 {market.upper()} 市场{kind}（计划时间 {schedule_text}，"
        f"{'启动补跑' if catch_up else '准时运行'}）。\n\n"
        f"这是自主调度轮次。必须调用一次 investment_analysis_workflow(market=\"{market}\", "
        f"label=\"{context.get('label', 'auto')}\", submit=true)，由固定工作流完成四类基础研究、多空辩论、"
        f"研究经理、个股交易员、组合构建、三方风险辩论、风险经理和最终组合经理。不要自行简化步骤，"
        f"也不要绕过该工具直接调用 investment_submit_decisions。\n\n"
        f"工具返回的检查点、硬风控结果、成交和拒绝清单是唯一事实。最后用中文简要复述最终决策、"
        f"成交/拒绝、研究失败的故障安全处理和风险提示，不得编造成交。"
    )


def _analysis_task(
    market: str,
    symbols: Sequence[str],
    *,
    symbols_source: str,
    label: str,
    submit: bool,
    cycle_id: str,
) -> str:
    """Task text for a fixed-workflow ANALYSIS round (web-launched or resume).

    Two blocks, one fixed entry: when symbols are given the round analyzes
    exactly those and never re-screens; otherwise the fixed workflow performs
    its own screening fallback (autonomous rounds only — the manual launcher
    rejects empty symbols). submit=False always for web-launched rounds —
    trading stays behind the user's later explicit approval.

    The symbol list is embedded as STRICT JSON (json.dumps) so the model is
    never asked to repair a hand-built parameter literal.
    """
    symbol_list_json = json.dumps([str(symbol) for symbol in symbols], ensure_ascii=False)
    if symbols:
        scope_text = (
            f"本轮目标标的是用户/流程指定的证券：{symbol_list_json}。"
            "固定工作流只分析这些标的；持仓信息仅作为组合上下文，不得擅自把选股池或持仓中的其他标的"
            "纳入本轮目标列表，也不得再次运行全市场选股。"
        )
    else:
        scope_text = "本轮未指定标的，由固定工作流内部执行选股（最新选股缓存 + 持仓）确定目标。"
    return (
        f"你在执行 Investment Auto 2.1 的 {market.upper()} 市场固定投资分析流程"
        f"（轮次 id {cycle_id}，标签 {label}）。\n\n"
        f"必须调用一次 investment_analysis_workflow(market=\"{market}\", "
        f"{'symbols=' + symbol_list_json + ', ' if symbols else ''}"
        f"symbols_source=\"{symbols_source}\", submit={'true' if submit else 'false'}, "
        f"cycle_id=\"{cycle_id}\", label=\"{label}\")，由固定工作流完成四类基础研究、多空辩论、"
        f"研究经理、个股交易员、组合草案、三方风险辩论、风险经理和最终组合经理。不要自行简化步骤、"
        f"不要自己调用行情/新闻工具重写这套分析，也不要绕过该工具直接调用 investment_submit_decisions。\n\n"
        f"{scope_text}\n\n"
        f"工具返回的检查点与最终决策是唯一事实。最后用中文简要复述最终决策、研究失败标的的故障安全处理"
        f"与风险提示，不得编造成交。"
    )


def _internal_home(home: str | Path) -> Path:
    """Background rounds live in their own DSH home so their sessions
    (the headless top session and every role subagent) never leak into the
    user's session store."""
    return Path(home) / "agent-home"


def _sync_internal_home(app_dir: Path, main_home: Path, internal_home: Path) -> None:
    """Prepare the internal home before one headless round.

    - Product-owned trees (profiles/presets/skills/plugins) are force-seeded
      from the shipped app/ — the internal home carries the same composition
      (including the fixed workflow row) as the main home.
    - The settings storages (model/provider selection) are copied from the
      main home so background rounds use the user's configured model; the
      session-plane files (workspace.json, session projections) are NOT
      copied — that is the isolation point.
    - Dev mode (no IA_ACCESS_TOKEN) resolves credentials from the main
      home's .credentials.yaml; mirror it into the internal home so the
      default credentials provider keeps working. Production rounds use the
      DPAPI overlay through the engine API and need no file.
    """
    from engine import dsh_home

    internal_home.mkdir(parents=True, exist_ok=True)
    dsh_home.seed_dsh_home(app_dir, internal_home, force=True)
    storages_source = main_home / "storages"
    if storages_source.is_dir():
        storages_target = internal_home / "storages"
        storages_target.mkdir(parents=True, exist_ok=True)
        for child in storages_source.iterdir():
            if not child.is_file():
                continue
            if child.name in {"workspace.json", "session_projcache.json"}:
                continue  # session-plane state stays in the user's home
            try:
                shutil.copy2(child, storages_target / child.name)
            except OSError:
                logger.debug("[DSH-BRIDGE] internal home sync skipped %s", child.name)
    if not os.getenv("IA_ACCESS_TOKEN", "").strip():
        credentials = main_home / ".credentials.yaml"
        if credentials.is_file():
            try:
                shutil.copy2(credentials, internal_home / ".credentials.yaml")
            except OSError:
                logger.debug("[DSH-BRIDGE] internal home credentials mirror skipped")


def _cycle_id(market: str, context: Mapping[str, Any]) -> str:
    label = str(context.get("label", "auto"))[:40]
    scheduled_at = context.get("scheduled_at")
    stamp = scheduled_at.strftime("%Y%m%dT%H%M") if isinstance(scheduled_at, datetime) else ""
    raw = f"{stamp or datetime.now().strftime('%Y%m%dT%H%M')}-{market}-{label}"
    return re.sub(r"[^A-Za-z0-9_.-]+", "-", raw).strip("-")[:120]


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

    def _spawn(self, market: str, task: str, label: str, cycle_id: str) -> Dict[str, Any]:
        """Run one headless DSH round (shared by scheduled and analysis rounds)."""
        from engine.subprocess_utils import decode_subprocess_output, hidden_subprocess_kwargs

        env = dict(os.environ)
        env["DSH_TELEMETRY_DISABLED"] = "1"
        # IA is a self-maintaining product agent. Headless repair paths cannot
        # answer an interactive approval channel, so they receive the same
        # explicit full-access DSH policy as the desktop conversation.
        env["DSH_PERMISSION_MODE"] = "danger-full-access"
        env["IA_AUTONOMOUS_ROUND"] = "1"
        env["INVESTMENT_CYCLE_ID"] = cycle_id
        # Point the round's investment tools at the engine API. The shell
        # passes INVESTMENT_ENGINE_URL to the web process only; the engine
        # process knows its own API port (INVESTMENT_API_PORT) instead.
        api_port = os.getenv("INVESTMENT_API_PORT", "").strip()
        engine_url = os.getenv("INVESTMENT_ENGINE_URL", "").strip()
        if not engine_url and api_port:
            engine_url = f"http://127.0.0.1:{api_port}"
        if engine_url:
            env["INVESTMENT_ENGINE_URL"] = engine_url
        home = self.dsh_home or _resolve_dsh_home(self.app_dir)
        if home:
            # Isolate background rounds into an internal home (sessions never
            # surface in the product session bar) while inheriting settings
            # and dev credentials from the user's home.
            internal = _internal_home(home)
            try:
                _sync_internal_home(self.app_dir, Path(home), internal)
            except Exception as exc:  # noqa: BLE001 - sync must not kill a round
                logger.warning("[DSH-BRIDGE:%s] internal home sync failed: %s", market, exc)
            env["DSH_HOME"] = str(internal)
        command = [self.node, str(self.bin), "--profile", "investment"]
        # Desktop lifecycle: credentials live in the engine DPAPI store, and
        # the round must resolve them through the same provider the web UI
        # writes. Dev mode (no IA_ACCESS_TOKEN) keeps the default
        # .credentials.yaml provider.
        dpapi_patch = self.app_dir / "profiles" / "patches" / "dpapi-credentials.yml"
        if dpapi_patch.exists() and os.getenv("IA_ACCESS_TOKEN", "").strip():
            command += ["--patch", str(dpapi_patch)]
        command.append(task)
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
        return {
            "status": "generated",
            "report_text": report_text or "DSH 轮次未输出总结。",
            "label": label,
            "elapsed_seconds": round(time.monotonic() - start_monotonic, 1),
            "_start_wall": start_wall,
        }

    def __call__(self, market: str, cycle_type: str, context: Mapping[str, Any]) -> Dict[str, Any]:
        label = str(context.get("label", "auto"))[:40]
        task = _round_task(market, cycle_type, context)
        spawned = self._spawn(market, task, label, _cycle_id(market, context))
        if spawned.get("status") != "generated":
            return spawned
        audit = _latest_audit_since(market, label, spawned.pop("_start_wall"))
        result: Dict[str, Any] = {
            "status": "generated",
            "report_text": spawned.get("report_text") or "DSH 轮次未输出总结。",
            "label": label,
            "elapsed_seconds": spawned.get("elapsed_seconds"),
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

    def run_analysis_round(
        self,
        market: str,
        *,
        symbols: Sequence[str],
        symbols_source: str,
        label: str,
        submit: bool,
        cycle_id: str,
    ) -> Dict[str, Any]:
        """One fixed-workflow ANALYSIS round (no engine-side audit folding).

        The workflow itself posts its checkpoints and final result into
        ``runtime/analysis_runs`` through the engine API; the caller folds
        that durable record instead of trading audits (analysis-only rounds
        never submit, so no audit is expected).
        """
        task = _analysis_task(
            market,
            list(symbols or []),
            symbols_source=symbols_source,
            label=label,
            submit=submit,
            cycle_id=cycle_id,
        )
        return self._spawn(market, task, label, cycle_id)


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
