"""Task-specific tools for the research loop.

These pydantic-ai Tools are injected into the fresh agent depending on the
task type.  Code changes go through the versioned ChangeManager: SHA-256
read, versioned backup, full test suite, automatic rollback on failure.
"""

from __future__ import annotations

import json
from typing import Any, Dict, Mapping, Optional

from pydantic_ai import RunContext, Tool

from src.research.loop import ResearchDeps


def _run_backtest_impl(
    market: str,
    symbols: str,
    lookback: int = 60,
    momentum_days: int = 5,
    weight_per_symbol: Optional[float] = None,
    cost_rate: float = 0.00225,
) -> Dict[str, Any]:
    from src.research.backtest import run_rule_backtest

    return run_rule_backtest(
        market,
        [item.strip() for item in str(symbols or "").split(",") if item.strip()],
        lookback=lookback,
        momentum_days=momentum_days,
        weight_per_symbol=weight_per_symbol,
        cost_rate=cost_rate,
    )


async def run_backtest(
    ctx: RunContext[ResearchDeps],
    market: str,
    symbols: str,
    lookback: int = 60,
    momentum_days: int = 5,
    weight_per_symbol: Optional[float] = None,
    cost_rate: float = 0.00225,
) -> Dict[str, Any]:
    """Run a deterministic rule backtest and record the result in the workspace.

    Args:
        market: cn, hk, us or etf.
        symbols: Comma-separated codes, for example 600519,000858.
        lookback: Trading days of history to load (10-500).
        momentum_days: Momentum lookback window (1-60).
        weight_per_symbol: Equal weight when empty.
        cost_rate: Flat round-trip cost per weight change.
    """
    result = _run_backtest_impl(market, symbols, lookback, momentum_days, weight_per_symbol, cost_rate)
    payload = json.dumps(result, ensure_ascii=False, default=str)
    ctx.deps.workspace.write_file(
        ctx.deps.round_no,
        f"backtest-{str(market or 'x').lower()}-{str(symbols or '')[:40]}.json".replace(",", "_"),
        payload,
    )
    summary = {key: result.get(key) for key in (
        "total_return", "annualized_return", "max_drawdown", "sharpe",
        "benchmark_return", "turnover_cost", "trading_days", "error",
    )}
    return summary


def _apply_code_change_impl(path: str, new_content: str, reason: str = "") -> Dict[str, Any]:
    from src.manager.change_manager import ChangeManager

    return ChangeManager().apply_text_change(path, new_content, reason=reason or "research-loop")


async def apply_code_change(
    ctx: RunContext[ResearchDeps],
    path: str,
    new_content: str,
    reason: str = "",
) -> Dict[str, Any]:
    """Modify one project file through the versioned change manager.

    The change manager records the previous SHA-256, writes a versioned
    backup, runs the full test suite and rolls the file back automatically
    when tests fail.  Only use this for bugfix or experiment application;
    never for secrets or runtime data.

    Args:
        path: Project-relative file path such as src/trading/risk.py.
        new_content: Complete new UTF-8 content of the file.
        reason: One-sentence human-readable reason recorded in the backup.
    """
    result = _apply_code_change_impl(path, new_content, reason)
    ctx.deps.workspace.write_file(
        ctx.deps.round_no,
        f"code-change-{str(path).replace('/', '-').replace(chr(92), '-')[:60]}.json",
        json.dumps(result, ensure_ascii=False, default=str),
    )
    return result


def task_tools(task: str):
    """Return the extra pydantic-ai tools for one task type."""
    backtest_tool = Tool(run_backtest, sequential=True, timeout=180)
    if task == "backtest":
        return [backtest_tool]
    if task == "strategy_experiment":
        return [backtest_tool, Tool(apply_code_change, sequential=True, timeout=420)]
    if task == "bugfix":
        return [Tool(apply_code_change, sequential=True, timeout=420)]
    raise ValueError("task 必须是 backtest、strategy_experiment 或 bugfix")
