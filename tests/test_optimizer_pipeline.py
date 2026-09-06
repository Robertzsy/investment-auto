"""Integration-level tests for the executable optimizer pipeline."""
from __future__ import annotations

from datetime import date, timedelta

import pytest

from engine.optimizer import runner


def _history(symbol: str, **_: object) -> dict:
    start = date(2026, 1, 1)
    bias = (sum(ord(ch) for ch in symbol) % 7) / 1000
    rows = []
    price = 10 + (sum(ord(ch) for ch in symbol) % 20)
    for index in range(80):
        price *= 1 + 0.0008 + bias + ((index % 5) - 2) * 0.001
        rows.append({"date": (start + timedelta(days=index)).isoformat(), "close": round(price, 6)})
    return {"code": symbol, "data": rows}


def test_optimizer_fetches_aligns_saves_and_returns_all_schemes(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.fetcher, "history", _history)
    monkeypatch.setattr(runner.account, "account", lambda market: {"holdings": []})

    result = runner.run_optimizer(
        market="cn",
        symbols=["600519", "000858", "601318"],
        lookback_days=60,
        samples=120,
        output_dir=tmp_path,
    )

    assert result["symbols"] == ["600519", "000858", "601318"]
    assert result["observations"] == 60
    assert result["recommended_scheme"] in {"mean_variance", "black_litterman", "risk_parity", "cost_adjusted"}
    for name in ("mean_variance", "black_litterman", "risk_parity", "cost_adjusted"):
        assert abs(sum(result[name]["weights"].values()) - 1) < 1e-6
        assert abs(sum(result[name]["portfolio_weights"].values()) - 1) < 1e-6
        assert max(value for key, value in result[name]["portfolio_weights"].items() if key != "CASH") <= 0.16000001
        assert "sharpe" in result[name]["metrics"]
        assert "net_sharpe" in result[name]["net_metrics"]
        assert result[name]["net_metrics"]["rebalance_cost"]["turnover_buy"] > 0
        assert "var95" in result[name]["stress"]
    saved = __import__("json").loads(__import__("pathlib").Path(result["output_file"]).read_text(encoding="utf-8"))
    assert saved["output_file"] == result["output_file"]


def test_small_universe_reduces_exposure_without_breaking_single_asset_cap(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.fetcher, "history", _history)
    monkeypatch.setattr(runner.account, "account", lambda market: {"cash": 500000, "holdings": []})
    result = runner.run_optimizer(
        market="cn", symbols=["600519", "000858"], lookback_days=60,
        samples=100, output_dir=tmp_path,
    )
    assert result["constraints"]["effective_exposure"] == pytest.approx(0.32)
    assert result["constraints"]["cash_weight"] == pytest.approx(0.68)
    for scheme in ("mean_variance", "black_litterman", "risk_parity", "cost_adjusted"):
        weights = result[scheme]["portfolio_weights"]
        assert weights["600519"] <= 0.16000001
        assert weights["000858"] <= 0.16000001
        assert weights["CASH"] == pytest.approx(0.68)


def test_hk_both_side_stamp_tax_is_included_in_buy_cost(monkeypatch, tmp_path):
    monkeypatch.setattr(runner.fetcher, "history", _history)
    monkeypatch.setattr(runner.account, "account", lambda market: {"cash": 500000, "holdings": []})
    result = runner.run_optimizer(
        market="hk", symbols=["00700", "09988", "03690"], lookback_days=60,
        samples=100, output_dir=tmp_path,
    )
    cost = result["mean_variance"]["net_metrics"]["rebalance_cost"]["cost_ratio"]
    # 42.9% effective exposure × (0.025% commission + 0.15% slippage + 0.1% stamp tax).
    assert cost == pytest.approx(0.429 * 0.00275, rel=1e-6)


def test_symbol_normalization_does_not_corrupt_valid_us_tickers():
    assert runner._symbol_key("USB") == "USB"
    assert runner._symbol_key("SHOP") == "SHOP"
    assert runner._symbol_key("HKD") == "HKD"
    assert runner._symbol_key("sh600519") == "600519"
    assert runner._symbol_key("hk00700") == "00700"
    assert runner._symbol_key("usAAPL") == "AAPL"


def test_optimizer_requires_two_explicit_symbols():
    with pytest.raises(ValueError, match="至少需要两个"):
        runner.resolve_universe("cn", ["600519"])


def test_optimizer_lock_rejects_parallel_run(monkeypatch, tmp_path):
    lock = tmp_path / ".cn-optimizer.lock"
    lock.write_text("busy", encoding="utf-8")
    monkeypatch.setattr(runner.fetcher, "history", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("must not fetch")))
    with pytest.raises(RuntimeError, match="已有任务正在运行"):
        runner.run_optimizer(market="cn", output_dir=tmp_path)
