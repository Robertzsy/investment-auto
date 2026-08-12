from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import screening
from src.optimizer import runner
from src.screening import engine
from src.screening import storage


NOW = datetime(2026, 8, 12, 10, 0, tzinfo=ZoneInfo("Asia/Shanghai"))


@pytest.fixture(autouse=True)
def _isolate_screening_storage(monkeypatch):
    """Unit tests must not read a developer's or deployment's MongoDB URI."""
    monkeypatch.setattr(engine, "get_screening_store", lambda settings: None)


def _snapshot(symbol: str, d20: float, amount: float) -> dict:
    return {
        "realtime": {"price": 100, "amount": amount, "name": symbol},
        "history": [],
        "indicators": {
            "mas": {"ma5": 105, "ma10": 102, "ma20": 99, "ma60": 95},
            "change": {"d5": d20 / 4, "d20": d20},
            "macd": {"bar": 1},
            "rsi": {"rsi14": 58},
            "volume": {"ratio": 1.4},
            "volatility": 2.0,
        },
    }


def test_hard_filter_excludes_st_illiquid_and_extreme_change():
    rows, rejected = engine._filter_candidates(
        [
            {"symbol": "600001", "name": "*ST测试", "price": 10, "amount": 2e8, "market_cap": 8e9},
            {"symbol": "600002", "name": "低流动性", "price": 10, "amount": 1e6, "market_cap": 8e9},
            {"symbol": "600003", "name": "极端波动", "price": 10, "amount": 2e8, "market_cap": 8e9, "change_pct": 19},
            {"symbol": "600004", "name": "正常公司", "price": 10, "amount": 2e8, "market_cap": 8e9, "change_pct": 2},
        ],
        "cn",
        {
            "min_price": {"cn": 3},
            "min_amount": {"cn": 1e8},
            "min_market_cap": {"cn": 5e9},
            "max_abs_change_pct": 15,
            "exclude_name_patterns": ["ST", "退"],
        },
    )

    assert [row["symbol"] for row in rows] == ["600004"]
    assert rejected == {"excluded_name": 1, "liquidity": 1, "daily_change": 1}


def test_us_common_stock_is_not_mistaken_for_cn_st_stock():
    rows, rejected = engine._filter_candidates(
        [{
            "symbol": "AAPL",
            "name": "Apple Inc. Common Stock",
            "price": 100,
            "amount": 1e9,
            "market_cap": 2e12,
        }],
        "us",
        {"exclude_name_patterns": ["ST", "WARRANT", "RIGHT", "UNIT", "PREFERRED"]},
    )
    assert [row["symbol"] for row in rows] == ["AAPL"]
    assert rejected == {}


def test_market_screen_discovers_scores_and_keeps_holdings(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "SCREENING_DIR", tmp_path)
    monkeypatch.setattr(engine.fetcher, "market_list", lambda market, **kwargs: {
        "source": "test-market",
        "data": [
            {"symbol": "600519", "name": "甲", "price": 100, "amount": 5e8, "market_cap": 2e11, "pe": 20, "pb": 3},
            {"symbol": "000858", "name": "乙", "price": 100, "amount": 4e8, "market_cap": 1e11, "pe": 18, "pb": 2},
        ],
    })

    def load(symbols, workers):
        assert workers == 2
        return {
            "000001": _snapshot("000001", -2, 2e8),
            "600519": _snapshot("600519", -10, 5e8),
            "000858": _snapshot("000858", 20, 4e8),
        }, {}

    result = engine.run_screening(
        "cn",
        held_symbols=["000001"],
        configured_symbols=[],
        fallback_symbols=["601318"],
        settings={
            "enabled": True,
            "discovery_limit": 20,
            "snapshot_limit": 10,
            "shortlist_size": 2,
            "min_price": {"cn": 0},
            "min_amount": {"cn": 0},
            "min_market_cap": {"cn": 0},
            "exclude_name_patterns": [],
        },
        autonomous_config={"max_universe_size": 3, "market_data_workers": 2},
        snapshot_loader=load,
        now=NOW,
    )

    assert result.symbols[0] == "000001"
    assert result.audit["status"] == "screened"
    assert result.audit["source"] == "test-market"
    assert result.audit["selected_symbols"][0] == "000858"
    assert result.audit["selected"][0]["score"] > result.audit["selected"][1]["score"]
    assert all(value <= 100 for item in result.audit["selected"] for value in item["factors"].values())
    assert (tmp_path / "latest-cn.json").exists()


def test_configured_universe_is_a_hard_candidate_pool(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "SCREENING_DIR", tmp_path)
    monkeypatch.setattr(
        engine.fetcher,
        "market_list",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not discover")),
    )

    result = engine.run_screening(
        "us",
        held_symbols=[],
        configured_symbols=["AAPL", "MSFT"],
        fallback_symbols=["NVDA"],
        settings={"enabled": True, "snapshot_limit": 5, "shortlist_size": 2},
        autonomous_config={"max_universe_size": 2, "market_data_workers": 1},
        snapshot_loader=lambda symbols, workers: ({
            symbol: _snapshot(symbol, 5 if symbol == "AAPL" else 2, 1e9)
            for symbol in symbols
        }, {}),
        now=NOW,
    )

    assert result.audit["configured_hard_pool"] is True
    assert set(result.symbols) == {"AAPL", "MSFT"}
    assert "NVDA" not in result.symbols


def test_optimizer_uses_recent_screening_shortlist(monkeypatch):
    monkeypatch.setitem(runner.cfg.raw, "screening", {
        "enabled": True,
        "use_for_optimizer": True,
        "optimizer_max_age_minutes": 1440,
    })
    monkeypatch.setattr(runner.account, "account", lambda market: {"holdings": [{"code": "AAPL"}]})
    monkeypatch.setattr(screening, "latest_screening", lambda market, **kwargs: {
        "selected_symbols": ["MSFT", "NVDA"]
    })

    assert runner.resolve_universe("us") == ["AAPL", "MSFT", "NVDA"]


def test_node_fetcher_exposes_bounded_market_list_contract():
    source = (engine.ROOT / "scripts" / "stock-fetcher.js").read_text(encoding="utf-8")
    assert 'command === "market-list"' in source
    assert "Math.min(500" in source
    assert "nasdaq-screener" in source
    assert "sina-market-center" in source


def test_discovery_uses_mongodb_cache_before_json_or_provider(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "SCREENING_DIR", tmp_path)
    expected = {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "market": "cn",
        "source": "test-mongodb",
        "cached": True,
        "cache_backend": "mongodb",
        "data": [{"symbol": "600519", "price": 100}],
    }

    class Store:
        def read_discovery(self, market, **kwargs):
            assert market == "cn"
            assert kwargs["limit"] == 20
            return expected

    monkeypatch.setattr(
        engine.fetcher,
        "market_list",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not fetch")),
    )

    assert engine._discover(
        "cn", {"discovery_limit": 20, "refresh_minutes": 30}, NOW, Store()
    ) == expected


def test_discovery_falls_back_to_json_when_mongodb_read_fails(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "SCREENING_DIR", tmp_path)
    engine._write_json(tmp_path / "discovery-us.json", {
        "generated_at": NOW.isoformat(timespec="seconds"),
        "market": "us",
        "source": "test-json",
        "cached": False,
        "data": [{"symbol": "AAPL", "price": 100}],
    })

    class Store:
        def read_discovery(self, *args, **kwargs):
            raise RuntimeError("mongodb unavailable")

        def write_discovery(self, *args, **kwargs):
            raise RuntimeError("mongodb unavailable")

    result = engine._discover(
        "us", {"discovery_limit": 20, "refresh_minutes": 30}, NOW, Store()
    )

    assert result["source"] == "test-json"
    assert result["cache_backend"] == "json"


def test_screening_store_auto_mode_without_uri_uses_json(monkeypatch):
    monkeypatch.delenv("MONGODB_URI", raising=False)
    assert storage.get_screening_store({"storage": {"backend": "auto"}}) is None


def test_screening_persists_snapshots_factors_and_run_to_store(monkeypatch, tmp_path):
    monkeypatch.setattr(engine, "SCREENING_DIR", tmp_path)
    monkeypatch.setattr(engine.fetcher, "market_list", lambda market, **kwargs: {
        "source": "test-market",
        "data": [{"symbol": "AAPL", "name": "Apple", "price": 100, "amount": 1e9}],
    })

    class Store:
        def __init__(self):
            self.calls = []

        def read_discovery(self, *args, **kwargs):
            return None

        def write_discovery(self, market, payload):
            self.calls.append(("discovery", market, len(payload["data"])))

        def write_snapshots(self, market, snapshots, generated_at):
            self.calls.append(("snapshots", market, sorted(snapshots)))

        def write_factors(self, market, factors, generated_at):
            self.calls.append(("factors", market, [item["symbol"] for item in factors]))

        def write_run(self, audit):
            self.calls.append(("run", audit["market"], audit["storage_backend"]))

    store = Store()
    monkeypatch.setattr(engine, "get_screening_store", lambda settings: store)
    result = engine.run_screening(
        "us",
        held_symbols=[],
        configured_symbols=[],
        fallback_symbols=["MSFT"],
        settings={"enabled": True, "discovery_limit": 20, "snapshot_limit": 5, "shortlist_size": 1},
        autonomous_config={"max_universe_size": 2, "market_data_workers": 1},
        snapshot_loader=lambda symbols, workers: ({"AAPL": _snapshot("AAPL", 5, 1e9)}, {}),
        now=NOW,
    )

    assert result.audit["storage_backend"] == "mongodb+json"
    assert [call[0] for call in store.calls] == ["discovery", "snapshots", "factors", "run"]
