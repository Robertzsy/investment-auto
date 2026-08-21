"""Engine tests for the manual decision execution path (submit_decisions)."""
from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from engine.trading import decision_execution
from engine.investment import service
from engine.investment.contracts import CommandEnvelope, InvestmentCommand

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
    "risk": {
        "single_stock_max_pct": 12,
        "min_cash_reserve_pct": 3,
        "hard_stop_pct": -8,
        "trailing_stop_pct": -5,
        "max_drawdown_pct": -20,
    },
}


@pytest.fixture(autouse=True)
def isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(decision_execution, "AUDIT_DIR", tmp_path / "audit")
    monkeypatch.setattr("engine.scheduler.REPORT_DIR", tmp_path / "reports")
    monkeypatch.setattr("engine.scheduler._deliver_completed_report", lambda *args, **kwargs: {"status": "skipped"})
    monkeypatch.setattr(
        "engine.investment.reflection.InvestmentReflectionService.evaluate_pending",
        lambda *args, **kwargs: [],
    )
    monkeypatch.setattr(
        "engine.investment.reflection.InvestmentReflectionService.reflect_cycle",
        lambda *args, **kwargs: {"status": "recorded"},
    )
    monkeypatch.setattr("engine.trading.control.load_state", lambda: {"paused": False, "kill_switch": False})

    # Isolated portfolio.
    from engine.portfolio import account as account_module

    portfolio = tmp_path / "portfolio.json"
    monkeypatch.setattr(account_module, "_path", lambda: portfolio)
    portfolio.write_text(json.dumps({
        "version": 2,
        "multiMarket": True,
        "accounts": {
            "cn": {"totalCapital": 500000, "cash": 500000, "holdings": [], "tradeHistory": []},
        },
        "fxRates": {},
    }), encoding="utf-8")

    # Screening cache: none -> allowed universe = holdings + optimizer defaults.
    monkeypatch.setattr("engine.screening.latest_screening", lambda *args, **kwargs: None)

    # Stable fake prices for every symbol.
    def fake_snapshot(symbol):
        return {"realtime": {"price": 100, "amount": 2e8, "name": symbol}, "history": [], "indicators": {}}

    monkeypatch.setattr("engine.data.fetcher.snapshot", fake_snapshot)
    monkeypatch.setattr("engine.screening.preview._snapshot_loader", lambda symbols, limit: ({s: fake_snapshot(s) for s in symbols}, {}))
    return tmp_path


def _submit(market="cn", decisions=None, **kwargs):
    return decision_execution.submit_decisions(
        market,
        decisions or [{"symbol": "600519", "action": "BUY", "target_weight": 0.05, "confidence": 0.9, "reason": "测试买入"}],
        now=NOW,
        **kwargs,
    )


def test_submit_decisions_executes_paper_fill_and_writes_artifacts(tmp_path):
    result = _submit(note="手动周期测试")

    assert result["status"] == "generated"
    assert len(result["fills"]) == 1
    assert result["fills"][0]["code"] == "600519"
    assert (tmp_path / "audit").exists()
    assert list((tmp_path / "audit").glob("*.json"))[0].name.endswith("-dsh-manual.json")
    report = list((tmp_path / "reports").glob("*.md"))[0]
    assert "已成交" in report.read_text(encoding="utf-8")
    assert result["report"] == str(report)


def test_submit_decisions_rejects_unknown_symbol(tmp_path):
    with pytest.raises(ValueError, match="超出允许池"):
        _submit(decisions=[{"symbol": "ZZZZZ", "action": "BUY", "target_weight": 0.1, "confidence": 0.9}])


def test_submit_decisions_refuses_when_kill_switch_active(monkeypatch, tmp_path):
    monkeypatch.setattr("engine.trading.control.load_state", lambda: {"paused": True, "kill_switch": True})
    with pytest.raises(RuntimeError, match="紧急停止"):
        _submit()


def test_submit_decisions_refuses_non_paper_mode(monkeypatch, tmp_path):
    monkeypatch.setitem(decision_execution.cfg.raw.setdefault("trading", {}), "mode", "live")
    with pytest.raises(RuntimeError, match="paper"):
        _submit()


def test_submit_decisions_low_confidence_is_rejected_by_risk(tmp_path):
    result = _submit(decisions=[{"symbol": "600519", "action": "BUY", "target_weight": 0.05, "confidence": 0.2}])
    assert result["fills"] == []
    assert any("置信度" in str(item.get("reason", "")) or "confidence" in str(item) for item in result["rejected"])


def test_submit_decisions_rejects_bad_action(tmp_path):
    with pytest.raises(ValueError, match="BUY、SELL 或 HOLD"):
        _submit(decisions=[{"symbol": "600519", "action": "PANIC"}])
    with pytest.raises(ValueError, match="必须是数组"):
        _submit(decisions="600519")


def test_service_dispatch_submit_decisions(tmp_path):
    envelope = CommandEnvelope(
        command_id="t-1",
        command=InvestmentCommand.SUBMIT_DECISIONS,
        payload={"market": "cn", "decisions": [{"symbol": "600519", "action": "BUY", "target_weight": 0.05, "confidence": 0.9}], "label": "svc-test"},
        requested_by="test",
    )
    result = service.InvestmentAgentService().execute_envelope(envelope, write_audit=False)
    assert result["ok"] is True
    assert result["label"] == "svc-test"
    assert len(result["fills"]) == 1


def test_submit_decisions_audit_contains_mandate_and_prices(tmp_path):
    result = _submit()
    audit_path = Path(result["audit_file"])
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["command"] == "submit_decisions"
    assert audit["mandate"]["profile"] in {"conservative", "neutral", "aggressive"}
    assert audit["prices"]["600519"] == 100
    assert audit["requested_by"] == "dsh"
