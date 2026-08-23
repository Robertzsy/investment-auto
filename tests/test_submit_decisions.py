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
    monkeypatch.setattr(decision_execution, "IDEMPOTENCY_DIR", tmp_path / "idempotency")
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
    # Isolate the broker's portfolio lock so parallel tests never share it.
    monkeypatch.setattr("engine.trading.broker.PORTFOLIO_LOCK", tmp_path / ".portfolio.lock")

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
    kwargs.setdefault("idempotency_key", "test-key")
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
        _submit(idempotency_key="bad-action-01", decisions=[{"symbol": "600519", "action": "PANIC"}])
    with pytest.raises(ValueError, match="必须是数组"):
        _submit(idempotency_key="bad-action-02", decisions="600519")


def test_service_dispatch_submit_decisions(tmp_path):
    envelope = CommandEnvelope(
        command_id="t-1",
        command=InvestmentCommand.SUBMIT_DECISIONS,
        payload={"market": "cn", "decisions": [{"symbol": "600519", "action": "BUY", "target_weight": 0.05, "confidence": 0.9}], "label": "svc-test", "idempotency_key": "svc-test-key"},
        requested_by="test",
    )
    result = service.InvestmentAgentService().execute_envelope(envelope, write_audit=False)
    assert result["ok"] is True
    assert result["label"] == "svc-test"
    assert len(result["fills"]) == 1


def test_submit_decisions_requires_idempotency_key(tmp_path):
    with pytest.raises(ValueError, match="缺少 idempotency_key"):
        decision_execution.submit_decisions("cn", [{"symbol": "600519", "action": "BUY", "target_weight": 0.05, "confidence": 0.9}], idempotency_key="")


def test_submit_decisions_audit_contains_mandate_and_prices(tmp_path):
    result = _submit()
    audit_path = Path(result["audit_file"])
    audit = json.loads(audit_path.read_text(encoding="utf-8"))
    assert audit["command"] == "submit_decisions"
    assert audit["mandate"]["profile"] in {"conservative", "neutral", "aggressive"}
    assert audit["prices"]["600519"] == 100
    assert audit["requested_by"] == "dsh"


def test_submit_decisions_idempotency_key_replays_without_double_fills(tmp_path):
    key = "20260822-cn-user-0000001"
    first = _submit(idempotency_key=key)
    assert first["status"] == "generated"
    assert len(first["fills"]) == 1
    assert first.get("duplicate") is not True

    second = _submit(idempotency_key=key)
    assert second.get("duplicate") is True
    assert second["audit_file"] == first["audit_file"]
    assert second["fills"] == first["fills"]
    # Exactly one audit file: the replay never re-executed the paper broker.
    assert len(list((tmp_path / "audit").glob("*.json"))) == 1
    # And exactly one idempotency record marked completed.
    records = list((tmp_path / "idempotency").glob("*.json"))
    assert len(records) == 1
    record = json.loads(records[0].read_text(encoding="utf-8"))
    assert record["status"] == "completed"
    assert record["audit_file"] == first["audit_file"]


def test_same_key_with_different_content_is_rejected(tmp_path):
    key = "20260822-cn-user-0000001"
    _submit(idempotency_key=key)
    with pytest.raises(ValueError, match="决策内容"):
        _submit(idempotency_key=key, decisions=[{"symbol": "600519", "action": "BUY", "target_weight": 0.2, "confidence": 0.9}])
    record = json.loads((tmp_path / "idempotency" / f"{key}.json").read_text(encoding="utf-8"))
    assert record["status"] == "completed"  # the original result is untouched


def test_crash_between_fills_and_bookkeeping_recovers_from_broker_receipt(tmp_path):
    """The broker receipt is written in the SAME atomic replace as the fills;
    even when the process dies right after the fills and before the idempotency
    'completed' record, a retry must replay the fills, not execute again."""
    key = "crash-key-0000001"

    first = _submit(idempotency_key=key)
    assert len(first["fills"]) == 1
    # The account-file receipt is the durable proof of the fills.
    portfolio = json.loads((tmp_path / "portfolio.json").read_text(encoding="utf-8"))
    receipts = portfolio["execution_receipts"]
    assert key in receipts
    assert len(receipts[key]["fills"]) == 1

    # Simulate the crash precisely: delete the idempotency completed record,
    # leave an expired in_progress claim, keep the account receipt.
    from engine.trading import decision_execution as module

    record_path = tmp_path / "idempotency" / f"{key}.json"
    record_path.unlink()
    module._write_idempotency(key, {"status": "in_progress", "claimed_at": "2020-01-01T00:00:00+08:00", "fingerprint": first["decision_fingerprint"]})
    second = _submit(idempotency_key=key)
    assert second.get("recovered") is True
    assert second["fills"] == first["fills"]
    # The account must not contain double fills.
    portfolio_after = json.loads((tmp_path / "portfolio.json").read_text(encoding="utf-8"))
    assert len(portfolio_after["accounts"]["cn"]["tradeHistory"]) == 1
    assert portfolio_after["accounts"]["cn"]["cash"] == portfolio["accounts"]["cn"]["cash"]


def test_submit_decisions_in_progress_claim_rejects_live_duplicate(tmp_path):
    from datetime import datetime as dt
    from zoneinfo import ZoneInfo

    key = "20260822-cn-user-0000002"
    first = _submit(idempotency_key=key)
    assert first["status"] == "generated"
    # Simulate a crashed submission: an in_progress claim younger than the
    # lease must block a concurrent duplicate instead of double-filling.
    from engine.trading import decision_execution as module

    record_path = module._idempotency_path(key)
    record = json.loads(record_path.read_text(encoding="utf-8"))
    assert record["status"] == "completed"
    record["status"] = "in_progress"
    record["claimed_at"] = dt.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    with pytest.raises(RuntimeError, match="正在进行中"):
        _submit(idempotency_key=key)
    # A stale claim with the SAME content is reclaimed; the broker receipt
    # then replays the original fills instead of executing again.
    record["claimed_at"] = "2020-01-01T00:00:00+08:00"
    record_path.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    again = _submit(idempotency_key=key)
    assert again.get("recovered") is True or again.get("duplicate") is True
    assert again["fills"] == first["fills"]
    final_record = json.loads(record_path.read_text(encoding="utf-8"))
    assert final_record["status"] == "completed"


def test_pre_broker_failure_releases_claim_for_retry(tmp_path, monkeypatch):
    """Transient pre-broker failures must not leave a blocking claim."""
    key = "transient-key-0000001"
    original = decision_execution._fetch_prices
    calls = {"count": 0}

    def flaky_prices(symbols):
        calls["count"] += 1
        if calls["count"] == 1:
            return {}, {"600519": "行情接口暂时不可用"}, {}
        return original(symbols)

    monkeypatch.setattr(decision_execution, "_fetch_prices", flaky_prices)
    with pytest.raises(ValueError, match="行情缺失"):
        _submit(idempotency_key=key)
    record_path = tmp_path / "idempotency" / f"{key}.json"
    assert not record_path.exists()
    # A later retry (prices restored) executes normally with the same key.
    result = _submit(idempotency_key=key)
    assert result["status"] == "generated"
    assert len(result["fills"]) == 1


def test_deterministic_content_rejection_is_recorded_as_terminal(tmp_path):
    key = "bad-content-key-01"
    with pytest.raises(ValueError, match="超出允许池"):
        _submit(idempotency_key=key, decisions=[{"symbol": "ZZZZZ", "action": "BUY", "target_weight": 0.1, "confidence": 0.9}])
    record = json.loads((tmp_path / "idempotency" / f"{key}.json").read_text(encoding="utf-8"))
    assert record["status"] == "rejected"
    with pytest.raises(RuntimeError, match="终态拒绝"):
        _submit(idempotency_key=key, decisions=[{"symbol": "ZZZZZ", "action": "BUY", "target_weight": 0.1, "confidence": 0.9}])


def test_kill_switch_releases_claim(tmp_path, monkeypatch):
    key = "kill-gate-key-01"
    monkeypatch.setattr("engine.trading.control.load_state", lambda: {"paused": False, "kill_switch": True})
    with pytest.raises(RuntimeError, match="紧急停止"):
        _submit(idempotency_key=key)
    record_path = tmp_path / "idempotency" / f"{key}.json"
    assert not record_path.exists()
    monkeypatch.undo()
    result = _submit(idempotency_key=key)
    assert result["status"] == "generated"


def _ready_run(monkeypatch, tmp_path, cycle_id, decisions):
    """Create an isolated analysis run and advance it to ready_for_execution."""
    from engine import analysis_runs

    monkeypatch.setattr(analysis_runs, "runtime_dir", lambda: tmp_path / "runtime")
    analysis_runs.start_or_resume({
        "cycle_id": cycle_id,
        "market": "cn",
        "label": "user-analysis",
        "symbols": [item["symbol"] for item in decisions],
        "symbols_source": "user",
    })
    ready = analysis_runs.update({
        "cycle_id": cycle_id,
        "stage": "final_decision",
        "event": "execution_ready",
        "decisions": decisions,
    })
    return ready


def test_user_specified_symbols_execute_when_bound_to_ready_run(tmp_path, monkeypatch):
    """分析成功 + 用户批准 → 可执行：用户点名的标的（不在默认池）以该轮
    cycle_id 提交时进入硬风控与撮合。"""
    cycle_id = "req-user-688981-0001"
    decisions = [{"symbol": "688981", "action": "BUY", "target_weight": 0.05, "confidence": 0.9, "reason": "用户批准"}]
    ready = _ready_run(monkeypatch, tmp_path, cycle_id, decisions)
    assert ready["status"] == "ready_for_execution"
    assert ready.get("decision_fingerprint")

    result = _submit(idempotency_key=cycle_id, decisions=decisions)
    assert result["status"] == "generated"
    assert len(result["fills"]) == 1
    assert result["fills"][0]["code"] == "688981"
    # The bound run is finalized with the execution truth.
    from engine import analysis_runs

    run = analysis_runs.get(cycle_id)
    assert run["status"] == "completed"
    assert run["execution"]["fills"][0]["code"] == "688981"


def test_bound_submission_requires_exact_run_decisions(tmp_path, monkeypatch):
    cycle_id = "req-user-688981-0002"
    decisions = [{"symbol": "688981", "action": "BUY", "target_weight": 0.05, "confidence": 0.9}]
    _ready_run(monkeypatch, tmp_path, cycle_id, decisions)
    tampered = [{"symbol": "688981", "action": "BUY", "target_weight": 0.3, "confidence": 0.9}]
    with pytest.raises(ValueError, match="不一致"):
        _submit(idempotency_key=cycle_id, decisions=tampered)
    # The rejection is terminal for this key.
    record = json.loads((tmp_path / "idempotency" / f"{cycle_id}.json").read_text(encoding="utf-8"))
    assert record["status"] == "rejected"


def test_bound_submission_requires_ready_status(tmp_path, monkeypatch):
    from engine import analysis_runs

    monkeypatch.setattr(analysis_runs, "runtime_dir", lambda: tmp_path / "runtime")
    cycle_id = "req-user-running-0003"
    analysis_runs.start_or_resume({
        "cycle_id": cycle_id,
        "market": "cn",
        "symbols": ["688981"],
        "symbols_source": "user",
    })
    decisions = [{"symbol": "688981", "action": "BUY", "target_weight": 0.05, "confidence": 0.9}]
    with pytest.raises(RuntimeError, match="ready_for_execution"):
        _submit(idempotency_key=cycle_id, decisions=decisions)


def test_standalone_submission_without_run_stays_inside_pool(tmp_path):
    decisions = [{"symbol": "688981", "action": "BUY", "target_weight": 0.05, "confidence": 0.9}]
    with pytest.raises(ValueError, match="超出允许池"):
        _submit(idempotency_key="standalone-0001", decisions=decisions)
