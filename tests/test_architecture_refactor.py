from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta
from pathlib import Path

import pytest

from src.investment import mandate
from src.investment.command_bus import InvestmentAgentClient, InvestmentCommandWorker
from src.investment.reflection import InvestmentReflectionService
from src.manager.change_manager import ChangeManager
from src.manager.reflection import ManagerReflectionService
from src.platform.memory_store import StructuredMemoryStore
from src.trading.risk import build_orders


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
        now=__import__("datetime").datetime(2026, 8, 13),
    )
    assert result["orders"][0]["shares"] == 50


def test_reflections_are_separate_and_structured(tmp_path):
    store = StructuredMemoryStore(tmp_path)
    investment = InvestmentReflectionService(store)
    manager = ManagerReflectionService(store=store)

    investment.reflect_cycle(
        {"market": "us", "status": "generated", "autonomous": {"status": "no_trade", "risk": {"rejected": []}}},
        mandate={"profile": "neutral", "risk_policy_version": "neutral-v1"},
        trigger="scheduler",
    )
    manager.record(
        user_goal="恢复交易", outcome="恢复失败", tool_calls=["manage_investment_agent"],
        error="kill switch active", verified=False,
    )

    assert store.recent("investment_reflections", limit=1)[0]["market"] == "us"
    assert store.recent("manager_reflections", limit=1)[0]["error"] == "kill switch active"
    assert store.recent("manager_memories", limit=1)[0]["scope"] == "management"


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


def test_change_manager_rolls_back_failed_change(monkeypatch, tmp_path):
    import src.manager.change_manager as module

    root = tmp_path / "repo"
    target = root / "src" / "investment" / "sample.py"
    target.parent.mkdir(parents=True)
    target.write_text("VALUE = 1\n", encoding="utf-8")
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "BACKUP_DIR", root / "runtime" / "backups")
    monkeypatch.setattr(module, "ALLOWED_ROOTS", (root / "src" / "investment",))
    monkeypatch.setattr(
        ChangeManager,
        "_run_tests",
        staticmethod(lambda commands: [{"command": "python -m pytest -q", "returncode": 1, "stdout": "", "stderr": "failed"}]),
    )
    manager = ChangeManager(StructuredMemoryStore(root / "memory"))
    result = manager.apply_text_change(
        "src/investment/sample.py",
        "VALUE = broken\n",
        reason="test rollback",
        expected_sha256=hashlib.sha256(b"VALUE = 1\n").hexdigest(),
    )

    assert result["status"] == "rolled_back"
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_change_manager_can_modify_any_project_file(monkeypatch, tmp_path):
    import src.manager.change_manager as module

    root = tmp_path / "repo"
    targets = {
        ".env": "TOKEN=changed\n",
        "runtime/data/portfolio.json": '{"accounts": {}}\n',
        "src/ui/chat_server.custom": "manager plane\n",
        "src/manager/change_manager.py": "# self managed\n",
    }
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "BACKUP_DIR", root / "runtime" / "backups")
    monkeypatch.setattr(module, "ALLOWED_ROOTS", (root,))
    monkeypatch.setattr(
        ChangeManager,
        "_run_tests",
        staticmethod(lambda commands: [{"command": "python -m pytest -q", "returncode": 0, "stdout": "", "stderr": ""}]),
    )
    manager = ChangeManager(StructuredMemoryStore(root / "memory"))

    for relative_path, content in targets.items():
        result = manager.apply_text_change(
            relative_path,
            content,
            reason="test unrestricted project write",
            expected_sha256="",
        )
        assert result["status"] == "verified_restart_requested"
        assert (root / relative_path).read_text(encoding="utf-8") == content


def test_change_manager_still_refuses_paths_outside_project(monkeypatch, tmp_path):
    import src.manager.change_manager as module

    root = tmp_path / "repo"
    monkeypatch.setattr(module, "ROOT", root)
    monkeypatch.setattr(module, "ALLOWED_ROOTS", (root,))
    manager = ChangeManager(StructuredMemoryStore(root / "memory"))

    with pytest.raises(ValueError, match="项目内相对路径"):
        manager.inspect("../outside.txt")


def test_queue_transport_refuses_when_worker_is_not_alive(monkeypatch):
    monkeypatch.setenv("INVESTMENT_AGENT_TRANSPORT", "queue")
    monkeypatch.setattr(InvestmentAgentClient, "worker_alive", staticmethod(lambda max_age_seconds=5: False))
    with pytest.raises(RuntimeError, match="独立进程未运行"):
        InvestmentAgentClient().issue("run_cycle", {"market": "us"}, timeout=0.1)


def test_worker_heartbeat_is_independent_from_command_loop(monkeypatch, tmp_path):
    import src.investment.command_bus as module

    heartbeat = tmp_path / "worker.json"
    monkeypatch.setattr(module, "HEARTBEAT", heartbeat)
    worker = InvestmentCommandWorker()
    worker.heartbeat_thread = __import__("threading").Thread(
        target=worker._heartbeat_loop,
        daemon=True,
    )
    worker.heartbeat_thread.start()
    try:
        deadline = __import__("time").monotonic() + 2
        while not heartbeat.exists() and __import__("time").monotonic() < deadline:
            __import__("time").sleep(0.02)
        assert json.loads(heartbeat.read_text(encoding="utf-8"))["status"] == "running"
        assert worker.heartbeat_thread.is_alive()
    finally:
        worker.stop()


def test_chat_server_has_no_semantic_keyword_fast_paths():
    source = (Path(__file__).parents[1] / "src" / "ui" / "chat_server.py").read_text(encoding="utf-8")
    for legacy in (
        "_build_agent_limit_answer", "_handle_autonomy_control_command",
        "_build_autonomy_status_answer", "_build_market_status_answer",
        "_build_optimizer_request", "_build_stock_analysis_context",
    ):
        assert legacy not in source


def test_chat_main_does_not_start_scheduler():
    source = (Path(__file__).parents[1] / "src" / "main.py").read_text(encoding="utf-8")
    chat_branch = source[source.index('if args.command == "chat"'):]
    assert "scheduler = start" not in chat_branch
    assert "management-only" in chat_branch
