"""Engine-unit tests kept from the 1.x suite: mandate overlays, hard risk caps,
and delayed-outcome investment memory (Investment Auto 2.0 engine plane)."""
from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from engine.investment import mandate
from engine.investment.reflection import InvestmentReflectionService
from engine.platform.memory_store import StructuredMemoryStore
from engine.trading.risk import build_orders


def test_strategy_mandate_is_versioned_and_overlays_hard_limits(monkeypatch, tmp_path):
    path = tmp_path / "mandate.json"
    monkeypatch.setattr(mandate, "MANDATE_FILE", path)

    selected = mandate.set_mandate("conservative", selected_by="user")
    auto, trading, market = mandate.effective_configs(
        {"min_confidence": 0.1},
        {"max_daily_trades": 99},
        {"risk": {"min_cash_reserve_pct": 0, "max_drawdown_pct": -99}},
        selected,
    )

    assert selected["version"] == 2
    assert auto["min_confidence"] == 0.75
    assert auto["max_total_position_pct"] == 55
    assert trading["max_daily_trades"] == 4
    assert market["risk"]["min_cash_reserve_pct"] == 45
    assert market["risk"]["max_drawdown_pct"] == -8


def test_total_position_limit_restricts_new_buy_capacity():
    result = build_orders(
        [{"symbol": "NEW", "action": "BUY", "target_weight": 0.2, "confidence": 0.9}],
        account={
            "cash": 50_000,
            "holdings": [{"code": "HELD", "shares": 500, "costPrice": 100}],
            "tradeHistory": [],
        },
        prices={"HELD": 100, "NEW": 100},
        allowed_symbols=["HELD", "NEW"],
        market_config={"trading": {"lot_size": 1}, "risk": {"min_cash_reserve_pct": 5}},
        autonomous_config={
            "min_confidence": 0.5,
            "max_position_pct": 30,
            "max_order_value_pct": 30,
            "max_cycle_turnover_pct": 40,
            "max_orders_per_cycle": 5,
            "max_total_position_pct": 55,
        },
        trading_config={"max_daily_trades": 10},
        now=datetime(2026, 8, 13),
    )
    assert result["orders"][0]["shares"] == 50


def test_only_delayed_market_outcomes_enter_investment_memory(tmp_path):
    store = StructuredMemoryStore(tmp_path)
    service = InvestmentReflectionService(store)
    pending = service.reflect_cycle(
        {
            "market": "us",
            "status": "generated",
            "autonomous": {
                "status": "no_trade",
                "prices": {"AAPL": 100},
                "chair": {"decisions": [{"symbol": "AAPL", "action": "BUY", "confidence": 0.8}]},
            },
        },
        mandate={"profile": "neutral", "risk_policy_version": "neutral-v1"},
        trigger="scheduler",
    )
    assert service.recent("us") == []

    start = datetime.fromisoformat(pending["created_at"]).date()
    rows = [
        {"date": (start + timedelta(days=offset)).isoformat(), "close": 100 + offset}
        for offset in range(1, 6)
    ]
    evaluated = service.evaluate_pending(
        "us",
        history_loader=lambda symbol, lookback=0: {"data": rows},
    )

    assert evaluated[0]["outcome_status"] == "evaluated"
    assert evaluated[0]["evaluated_horizons"] == ["T+1", "T+5"]
    assert service.recent("us", 1)[0]["source_record_id"] == pending["record_id"]
