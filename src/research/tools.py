"""Management-plane entry points for the research loop.

These plain functions are installable through the CapabilityRegistry, giving
the conversation manager read-only visibility and controlled execution of
the offline research plane.  None of them can place orders or touch the
live account.
"""

from __future__ import annotations

from dataclasses import asdict
from typing import Any, Dict, Mapping, Optional

from src.research.workspace import ResearchWorkspace


def run_backtest_tool(
    market: str,
    symbols: str,
    lookback: int = 60,
    momentum_days: int = 5,
    weight_per_symbol: Optional[float] = None,
) -> Dict[str, Any]:
    """Run one deterministic rule backtest (research only, no orders)."""
    from src.research.backtest import run_rule_backtest

    return run_rule_backtest(
        market,
        [item.strip() for item in str(symbols or "").split(",") if item.strip()],
        lookback=lookback,
        momentum_days=momentum_days,
        weight_per_symbol=weight_per_symbol,
    )


def run_strategy_experiment(
    objective: str,
    market: str = "cn",
    symbols: str = "",
    max_rounds: int = 4,
) -> Dict[str, Any]:
    """Run fresh-agent research rounds (backtest/experiment/bugfix) in the isolated workspace."""
    from pathlib import Path

    from src.config import cfg
    from src.research.loop import run_research_loop
    from src.research.tasks import task_tools

    architecture = cfg.raw.get("architecture", {}) or {}
    settings = architecture.get("research", {}) or {}
    if not bool(settings.get("enabled", True)):
        return {"status": "disabled", "reason": "architecture.research.enabled=false"}
    shell_mode = str(settings.get("shell", "restricted")).strip().lower()
    workspace_value = str(settings.get("workspace", "")).strip()
    workspace_root = None
    if workspace_value:
        from src.research.workspace import ROOT as _ROOT

        workspace_root = Path(workspace_value)
        if not workspace_root.is_absolute():
            workspace_root = _ROOT / workspace_root
    task = "strategy_experiment"
    outcome = run_research_loop(
        objective,
        task,
        market=market,
        max_rounds=int(max_rounds or settings.get("max_rounds", 6)),
        workspace_root=workspace_root,
        include_shell_tools=shell_mode != "none",
        shell_timeout_seconds=int(settings.get("shell_timeout_seconds", 60)),
        extra_tools=task_tools(task),
    )
    return asdict(outcome)


def list_research_results(limit: int = 10) -> Dict[str, Any]:
    """List recent research runs and their final reports."""
    workspace = ResearchWorkspace()
    runs = []
    if workspace.root.exists():
        for run_dir in sorted(workspace.root.glob("*"), reverse=True):
            if not run_dir.is_dir():
                continue
            reports = ResearchWorkspace(run_id=run_dir.name).read_round_reports(1)
            meta = None
            meta_path = run_dir / "meta.json"
            try:
                import json

                meta = json.loads(meta_path.read_text(encoding="utf-8"))
            except Exception:
                meta = None
            runs.append({
                "run_id": run_dir.name,
                "task": (meta or {}).get("task"),
                "objective": str((meta or {}).get("objective", ""))[:200],
                "final_status": reports[-1].get("status") if reports else "unknown",
                "final_report": reports[-1] if reports else {},
            })
            if len(runs) >= max(1, min(100, int(limit))):
                break
    return {"runs": runs}


def apply_experiment_to_config(path: str, new_content: str, reason: str = "") -> Dict[str, Any]:
    """Apply one reviewed experiment change through the versioned change manager.

    The change manager backs up the previous version, runs the full test
    suite and rolls back automatically on failure.  Use only after the
    research run has been reviewed; never for secrets or runtime data.
    """
    from src.manager.change_manager import ChangeManager

    return ChangeManager().apply_text_change(path, new_content, reason=reason or "research-experiment")


def register_research_tools() -> Dict[str, Any]:
    """Install the research tools into the manager capability registry."""
    from src.manager.capabilities import CapabilityRegistry

    registry = CapabilityRegistry()
    manifests = {
        "run_backtest": ("src.research.tools", "run_backtest_tool", "运行确定性规则回测（研究用途，不下单）", {
            "type": "object",
            "properties": {
                "market": {"type": "string", "description": "cn、hk、us 或 etf"},
                "symbols": {"type": "string", "description": "逗号分隔代码，如 600519,000858"},
                "lookback": {"type": "integer", "description": "历史交易日数，默认 60"},
                "momentum_days": {"type": "integer", "description": "动量窗口，默认 5"},
                "weight_per_symbol": {"type": "number", "description": "每标的权重，留空等权"},
            },
            "required": ["market", "symbols"],
        }),
        "run_strategy_experiment": ("src.research.tools", "run_strategy_experiment", "在隔离工作区运行新鲜 Agent 研究循环", {
            "type": "object",
            "properties": {
                "objective": {"type": "string", "description": "不可变研究目标"},
                "market": {"type": "string"},
                "symbols": {"type": "string"},
                "max_rounds": {"type": "integer"},
            },
            "required": ["objective"],
        }),
        "list_research_results": ("src.research.tools", "list_research_results", "列出最近的研究运行与最终报告", {
            "type": "object",
            "properties": {"limit": {"type": "integer"}},
            "required": [],
        }),
        "apply_experiment_to_config": ("src.research.tools", "apply_experiment_to_config", "经版本化变更管理器应用已审阅的实验修改（全量测试+回滚）", {
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "项目内文件路径，如 config/config.yaml"},
                "new_content": {"type": "string", "description": "完整新文件内容"},
                "reason": {"type": "string"},
            },
            "required": ["path", "new_content"],
        }),
    }
    installed = {}
    for name, (module, function, description, schema) in manifests.items():
        try:
            installed[name] = registry.install_tool(
                name,
                description,
                module,
                function,
                schema,
            )
        except Exception as exc:
            installed[name] = {"status": "error", "error": str(exc)}
    return {"installed": installed}
