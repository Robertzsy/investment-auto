from __future__ import annotations

import json
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from src.trading.broker import execute_orders
from src.trading.control import activate_kill_switch, load_state, reset_kill_switch, set_paused
from src.trading import controller
from src.trading.controller import _account_for_agents, _normalize_decisions, _parse_json_object
from src.trading.risk import build_orders


NOW = datetime(2026, 8, 11, 10, 30, tzinfo=ZoneInfo("Asia/Shanghai"))

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
    "risk": {
        "single_stock_max_pct": 12,
        "min_cash_reserve_pct": 3,
        "hard_stop_pct": -8,
        "trailing_stop_pct": -5,
        "max_drawdown_pct": -20,
    },
}

AUTO_CONFIG = {
    "min_confidence": 0.65,
    "max_position_pct": 12,
    "max_order_value_pct": 20,
    "max_cycle_turnover_pct": 30,
    "max_orders_per_cycle": 3,
}


def _portfolio(path: Path, *, cash: float = 100_000, holdings=None) -> None:
    path.write_text(json.dumps({
        "version": 2,
        "multiMarket": True,
        "accounts": {
            "cn": {
                "totalCapital": 100_000,
                "cash": cash,
                "holdings": holdings or [],
                "tradeHistory": [],
            },
        },
        "fxRates": {},
    }), encoding="utf-8")


def test_ai_target_weight_is_recomputed_and_hard_capped():
    result = build_orders(
        [{
            "symbol": "600519",
            "action": "BUY",
            "target_weight": 0.80,
            "confidence": 0.9,
            "reason": "test",
        }],
        account={"cash": 100_000, "holdings": [], "tradeHistory": []},
        prices={"600519": 100},
        allowed_symbols=["600519"],
        market_config=CN_CONFIG,
        autonomous_config=AUTO_CONFIG,
        trading_config={"max_daily_trades": 6},
        now=NOW,
    )

    assert len(result["orders"]) == 1
    order = result["orders"][0]
    assert order["target_weight"] == pytest.approx(0.12)
    assert order["shares"] == 100


def test_low_confidence_and_unknown_symbols_are_rejected():
    result = build_orders(
        [
            {"symbol": "600519", "action": "BUY", "target_weight": 0.1, "confidence": 0.2},
            {"symbol": "000001", "action": "BUY", "target_weight": 0.1, "confidence": 0.9},
        ],
        account={"cash": 100_000, "holdings": [], "tradeHistory": []},
        prices={"600519": 100, "000001": 10},
        allowed_symbols=["600519"],
        market_config=CN_CONFIG,
        autonomous_config=AUTO_CONFIG,
        trading_config={"max_daily_trades": 6},
        now=NOW,
    )

    assert result["orders"] == []
    assert len(result["rejected"]) == 2


def test_hard_stop_generates_forced_exit_without_ai_decision():
    account = {
        "cash": 90_000,
        "holdings": [{"code": "600519", "quantity": 100, "cost": 100, "highPrice": 110}],
        "tradeHistory": [],
    }
    result = build_orders(
        [],
        account=account,
        prices={"600519": 90},
        allowed_symbols=["600519"],
        market_config=CN_CONFIG,
        autonomous_config=AUTO_CONFIG,
        trading_config={"max_daily_trades": 0},
        now=NOW,
    )

    assert result["orders"][0]["side"] == "SELL"
    assert result["orders"][0]["shares"] == 100
    assert result["protective_decisions"][0]["forced"] is True


def test_take_profit_is_staged_and_rounded_to_market_lot():
    market_config = json.loads(json.dumps(CN_CONFIG))
    market_config["risk"].update({
        "take_profit_1_pct": 12,
        "take_profit_1_sell_ratio": 0.4,
        "take_profit_2_pct": 20,
        "take_profit_2_sell_ratio": 0.5,
    })
    account = {
        "cash": 50_000,
        "holdings": [{"code": "600519", "quantity": 500, "cost": 100, "highPrice": 115}],
        "tradeHistory": [],
    }
    result = build_orders(
        [],
        account=account,
        prices={"600519": 115},
        allowed_symbols=["600519"],
        market_config=market_config,
        autonomous_config=AUTO_CONFIG,
        trading_config={"max_daily_trades": 0},
        now=NOW,
    )

    assert result["orders"][0]["shares"] == 200
    assert result["orders"][0]["protection_stage"] == "take_profit_1"


def test_account_drawdown_circuit_breaker_forces_full_liquidation():
    account = {
        "cash": 90_000,
        "highWaterMark": 125_000,
        "holdings": [{"code": "600519", "quantity": 100, "cost": 100}],
        "tradeHistory": [],
    }
    result = build_orders(
        [],
        account=account,
        prices={"600519": 100},
        allowed_symbols=["600519"],
        market_config=CN_CONFIG,
        autonomous_config=AUTO_CONFIG,
        trading_config={"max_daily_trades": 0},
        now=NOW,
    )

    assert result["circuit_breaker"] is True
    assert result["orders"][0]["shares"] == 100
    assert result["orders"][0]["protection_stage"] == "drawdown_liquidation"


def test_paper_broker_enforces_t_plus_one_and_updates_cash(tmp_path):
    portfolio = tmp_path / "portfolio.json"
    _portfolio(portfolio)
    buy = {
        "symbol": "600519",
        "side": "BUY",
        "shares": 100,
        "reference_price": 100,
        "target_weight": 0.1,
        "confidence": 0.9,
        "reason": "entry",
        "decision_id": "buy-1",
    }

    bought = execute_orders(
        "cn", [buy],
        market_config=CN_CONFIG,
        trading_mode="paper",
        now=NOW,
        portfolio_path=portfolio,
        equity_snapshot=100_000,
        mark_prices={"600519": 100},
    )
    assert len(bought["fills"]) == 1
    data = json.loads(portfolio.read_text(encoding="utf-8"))
    account = data["accounts"]["cn"]
    assert account["cash"] < 90_000
    assert account["holdings"][0]["quantity"] == 100
    assert account["highWaterMark"] == 100_000

    sell = {**buy, "side": "SELL", "reference_price": 101, "reason": "exit", "decision_id": "sell-1"}
    same_day = execute_orders(
        "cn", [sell],
        market_config=CN_CONFIG,
        trading_mode="paper",
        now=NOW + timedelta(hours=3),
        portfolio_path=portfolio,
    )
    assert same_day["fills"] == []
    assert "可卖数量不足" in same_day["rejected"][0]["reason"]

    next_day = execute_orders(
        "cn", [sell],
        market_config=CN_CONFIG,
        trading_mode="paper",
        now=NOW + timedelta(days=1),
        portfolio_path=portfolio,
    )
    assert len(next_day["fills"]) == 1
    data = json.loads(portfolio.read_text(encoding="utf-8"))
    assert data["accounts"]["cn"]["holdings"] == []


def test_broker_refuses_non_paper_mode(tmp_path):
    portfolio = tmp_path / "portfolio.json"
    _portfolio(portfolio)
    with pytest.raises(RuntimeError, match="paper"):
        execute_orders(
            "cn", [],
            market_config=CN_CONFIG,
            trading_mode="live",
            now=NOW,
            portfolio_path=portfolio,
        )


def test_human_control_requires_explicit_kill_reset(tmp_path):
    path = tmp_path / "control.json"
    assert load_state(path)["paused"] is False
    killed = activate_kill_switch(path=path, reason="test")
    assert killed["paused"] is True
    assert killed["kill_switch"] is True
    with pytest.raises(RuntimeError, match="reset-kill"):
        set_paused(False, path=path)
    reset = reset_kill_switch(path=path)
    assert reset["kill_switch"] is False
    assert reset["paused"] is True
    resumed = set_paused(False, path=path)
    assert resumed["paused"] is False


def test_chair_json_parser_and_decision_normalizer():
    fence = chr(96) * 3
    payload = _parse_json_object(
        fence + 'json\n{"thesis":"ok","decisions":[{"symbol":"aapl","action":"buy"}]}\n' + fence
    )
    decisions = _normalize_decisions(payload, 5)
    assert decisions == [{
        "symbol": "AAPL",
        "action": "BUY",
        "decision_id": "ai-1",
    }]


def test_external_agent_context_omits_trade_history_and_notes():
    result = _account_for_agents({
        "cash": 100,
        "holdings": [{"code": "600519", "quantity": 1, "cost": 10, "secret": "omit"}],
        "tradeHistory": [{"code": "600519", "note": "private note"}],
    })
    assert "tradeHistory" not in result
    assert "secret" not in result["holdings"][0]
    assert result["trade_count"] == 1


def test_autonomous_cycle_runs_committee_risk_and_execution(monkeypatch, tmp_path):
    autonomous = {
        **AUTO_CONFIG,
        "enabled": True,
        "auto_execute": True,
        "trade_on_catch_up": False,
        "committee_roles": ["analyst", "risk_chairman"],
        "minimum_agent_responses": 2,
        "minimum_priced_symbols": 1,
        "max_universe_size": 2,
        "market_data_workers": 1,
        "agent_workers": 2,
        "cycle_timeout_seconds": 30,
        "universe": {"cn": ["600519"]},
    }
    monkeypatch.setitem(controller.cfg.raw, "autonomous", autonomous)
    monkeypatch.setenv("AUTONOMOUS_TRADING_ENABLED", "true")
    monkeypatch.setattr(controller, "load_state", lambda: {"paused": False, "kill_switch": False})
    monkeypatch.setattr(controller, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(controller, "CYCLE_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(controller.account_store, "account", lambda market: {
        "cash": 100_000,
        "holdings": [],
        "tradeHistory": [],
    })
    monkeypatch.setattr(controller, "_fetch_snapshots", lambda symbols, workers: ({
        "600519": {"realtime": {"price": 100}, "history": [], "indicators": {}},
    }, {}))
    monkeypatch.setattr(controller, "_run_committee_member", lambda role, context: {
        "role": role,
        "response": '{"summary":"ok"}',
    })
    monkeypatch.setattr(controller, "_chair_decision", lambda market, context, committee, config: {
        "thesis": "test",
        "decisions": [{
            "decision_id": "chair-1",
            "symbol": "600519",
            "action": "BUY",
            "target_weight": 0.1,
            "confidence": 0.9,
            "reason": "committee consensus",
        }],
    })
    captured = {}

    def fake_execute(market, orders, **kwargs):
        captured["orders"] = orders
        return {"fills": [{"code": "600519", "action": "BUY", "shares": orders[0]["shares"]}], "rejected": []}

    monkeypatch.setattr(controller, "execute_orders", fake_execute)

    result = controller.run_autonomous_cycle("cn", label="test", now=NOW)

    assert result["status"] == "executed"
    assert captured["orders"][0]["shares"] == 100
    assert Path(result["audit_file"]).exists()


def test_autonomous_cycle_uses_staged_workflow_portfolio_decisions(monkeypatch, tmp_path):
    autonomous = {
        **AUTO_CONFIG,
        "enabled": True,
        "auto_execute": False,
        "minimum_priced_symbols": 1,
        "max_universe_size": 2,
        "market_data_workers": 1,
        "cycle_timeout_seconds": 30,
        "universe": {"cn": ["600519"]},
        "agent_workflow": {"enabled": True},
    }
    monkeypatch.setitem(controller.cfg.raw, "autonomous", autonomous)
    monkeypatch.setenv("AUTONOMOUS_TRADING_ENABLED", "true")
    monkeypatch.setattr(controller, "load_state", lambda: {"paused": False, "kill_switch": False})
    monkeypatch.setattr(controller, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr(controller, "CYCLE_LOCK_DIR", tmp_path / "locks")
    monkeypatch.setattr(controller.account_store, "account", lambda market: {
        "cash": 100_000, "holdings": [], "tradeHistory": [],
    })
    monkeypatch.setattr(controller, "_fetch_snapshots", lambda symbols, workers: ({
        "600519": {"realtime": {"price": 100}, "history": [], "indicators": {}},
    }, {}))
    from src.trading import agent_workflow
    monkeypatch.setattr(agent_workflow, "run_analysis_workflow", lambda context, config: {
        "workflow": "tradingagents_staged_v1",
        "portfolio_manager": {
            "thesis": "test",
            "decisions": [{
                "decision_id": "portfolio-1", "symbol": "600519", "action": "BUY",
                "target_weight": 0.1, "confidence": 0.9, "reason": "cited",
                "evidence_ids": ["MARKET:600519"],
            }],
        },
    })

    result = controller.run_autonomous_cycle("cn", label="staged", now=NOW)

    assert result["status"] == "no_trade"
    assert result["agent_workflow"]["workflow"] == "tradingagents_staged_v1"
    assert result["chair"]["decisions"][0]["decision_id"] == "portfolio-1"


def test_catch_up_never_replays_trades_by_default(monkeypatch):
    monkeypatch.setenv("AUTONOMOUS_TRADING_ENABLED", "true")
    monkeypatch.setattr(controller, "load_state", lambda: {"paused": False, "kill_switch": False})
    monkeypatch.setitem(controller.cfg.raw, "autonomous", {
        "enabled": True,
        "trade_on_catch_up": False,
    })
    result = controller.run_autonomous_cycle("cn", label="0930", now=NOW, catch_up=True)
    assert result["status"] == "skipped"
