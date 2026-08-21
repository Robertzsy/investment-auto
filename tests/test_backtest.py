"""Engine-unit tests kept from the 1.x suite: deterministic rule backtests."""
from __future__ import annotations

import pytest

from engine.research import backtest


def _rising_closes(market, symbol, lookback):
    dates = [f"2026-01-{i + 1:02d}" for i in range(40)]
    return [(date, 100.0 + index) for index, date in enumerate(dates)]


def test_rule_backtest_momentum_wins_on_uptrend(monkeypatch):
    monkeypatch.setattr(backtest, "_history_closes", _rising_closes)
    result = backtest.run_rule_backtest(
        "cn", ["600519", "000858"], lookback=30, momentum_days=5,
    )
    assert "error" not in result
    assert result["total_return"] > 0
    assert result["benchmark_return"] > 0
    assert result["max_drawdown"] <= 0
    assert result["trading_days"] > 10
    assert len(result["nav_series"]) == result["trading_days"] + 1


def test_rule_backtest_requires_symbols():
    with pytest.raises(ValueError, match="至少"):
        backtest.run_rule_backtest("cn", [])
