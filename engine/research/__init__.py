"""Offline research helpers.

Investment Auto 2.0 keeps the deterministic rule backtest engine; the
fresh-agent research loop is replaced by DSH's native subagent/workflow
capabilities in the app plane.
"""

from engine.research.backtest import run_rule_backtest

__all__ = ["run_rule_backtest"]
