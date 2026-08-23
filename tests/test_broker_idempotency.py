"""Broker-level execution-receipt tests: the fills' durability point."""
from __future__ import annotations

import json
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from engine.trading import broker

NOW = datetime(2026, 8, 12, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))
CN_CONFIG = {
    "trading": {
        "settlement": "T+1",
        "lot_size": 100,
        "commission_rate": 0.00025,
        "stamp_tax": 0.001,
        "stamp_tax_side": "sell",
        "slippage": 0.001,
        "price_decimals": 2,
    },
}


@pytest.fixture()
def portfolio_path(tmp_path):
    path = tmp_path / "portfolio.json"
    path.write_text(json.dumps({
        "version": 2,
        "multiMarket": True,
        "accounts": {
            "cn": {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []},
        },
        "fxRates": {},
    }), encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(broker, "PORTFOLIO_LOCK", tmp_path / ".portfolio.lock")
    return tmp_path


def _orders():
    return [{
        "symbol": "600519",
        "side": "BUY",
        "shares": 100,
        "reference_price": 100,
        "decision_id": "dsh-1",
        "reason": "测试买入",
    }]


def _read(portfolio_path):
    return json.loads(portfolio_path.read_text(encoding="utf-8"))


def _execute(portfolio_path, **kwargs):
    return broker.execute_orders(
        "cn",
        _orders(),
        market_config=CN_CONFIG,
        trading_mode="paper",
        now=NOW,
        portfolio_path=portfolio_path,
        **kwargs,
    )


def test_first_execution_fills_and_writes_receipt(portfolio_path):
    result = _execute(portfolio_path, idempotency_key="cycle-1", decision_fingerprint="fp-1")
    assert len(result["fills"]) == 1
    assert result.get("replayed") is not True
    payload = _read(portfolio_path)
    receipt = payload["execution_receipts"]["cycle-1"]
    assert receipt["decision_fingerprint"] == "fp-1"
    assert len(receipt["fills"]) == 1
    # The fills and the receipt live in the SAME document.
    assert len(payload["accounts"]["cn"]["tradeHistory"]) == 1


def test_same_key_same_content_replays_without_double_fills(portfolio_path):
    first = _execute(portfolio_path, idempotency_key="cycle-1", decision_fingerprint="fp-1")
    second = _execute(portfolio_path, idempotency_key="cycle-1", decision_fingerprint="fp-1")
    assert second["replayed"] is True
    assert second["fills"] == first["fills"]
    assert len(_read(portfolio_path)["accounts"]["cn"]["tradeHistory"]) == 1


def test_same_key_different_content_is_refused(portfolio_path):
    _execute(portfolio_path, idempotency_key="cycle-1", decision_fingerprint="fp-1")
    with pytest.raises(ValueError, match="不同的决策内容"):
        _execute(portfolio_path, idempotency_key="cycle-1", decision_fingerprint="fp-2")
    assert len(_read(portfolio_path)["accounts"]["cn"]["tradeHistory"]) == 1


def test_receipt_is_durable_across_process_restart(portfolio_path):
    """Simulate a crash between fills and bookkeeping: reload the portfolio
    from disk (a fresh process sees exactly what was atomically written)."""
    _execute(portfolio_path, idempotency_key="cycle-1", decision_fingerprint="fp-1")
    assert "cycle-1" in _read(portfolio_path)["execution_receipts"]
    replay = broker.execute_orders(
        "cn",
        _orders(),
        market_config=CN_CONFIG,
        trading_mode="paper",
        now=NOW,
        portfolio_path=portfolio_path,
        idempotency_key="cycle-1",
        decision_fingerprint="fp-1",
    )
    assert replay["replayed"] is True
    assert len(replay["fills"]) == 1
    assert len(_read(portfolio_path)["accounts"]["cn"]["tradeHistory"]) == 1


def test_receipts_are_pruned_beyond_cap(portfolio_path):
    payload = _read(portfolio_path)
    receipts = payload.setdefault("execution_receipts", {})
    for index in range(205):
        receipts[f"old-{index}"] = {"completed_at": f"2026-01-01T{index % 24:02d}:00:00", "fills": []}
    portfolio_path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    _execute(portfolio_path, idempotency_key="cycle-1", decision_fingerprint="fp-1")
    after = _read(portfolio_path)
    assert "cycle-1" in after["execution_receipts"]
    assert len(after["execution_receipts"]) <= 200
