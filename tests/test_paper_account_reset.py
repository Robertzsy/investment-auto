from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from src.investment.contracts import CommandEnvelope, InvestmentCommand
from src.investment.service import InvestmentAgentService
from src.portfolio import account as account_store
from src.ui import agent_runtime


def _command(market: str = "cn") -> CommandEnvelope:
    return CommandEnvelope(
        command_id="reset-test",
        command=InvestmentCommand.RESET_PAPER_ACCOUNT,
        payload={"market": market, "reason": "test reset"},
        requested_by="test",
        created_at="2026-08-15T00:30:00+08:00",
    )


def _portfolio():
    data = account_store._default_portfolio()
    data["accounts"]["cn"].update({
        "cash": 123456,
        "holdings": [{"code": "600233", "shares": 100}],
        "tradeHistory": [{"code": "600233", "action": "BUY"}],
    })
    data["accounts"]["us"].update({
        "cash": 456789,
        "holdings": [{"code": "NVDA", "shares": 2}],
    })
    return data


def test_reset_command_resets_only_selected_paper_market_and_creates_backup(monkeypatch, tmp_path):
    from src.investment import service

    monkeypatch.setattr(account_store, "RUNTIME", tmp_path / "runtime" / "data")
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(service.cfg, "_data", {
        "schedule": {"timezone": "Asia/Shanghai"},
        "trading": {"mode": "paper"},
    })
    original = _portfolio()
    account_store.save(original)

    result = InvestmentAgentService().execute_envelope(_command(), write_audit=False)
    saved = account_store.load()

    assert result["ok"] is True
    assert result["status"] == "reset"
    assert result["previous"]["holdings"] == 1
    assert saved["accounts"]["cn"] == account_store.DEFAULTS["cn"]
    assert saved["accounts"]["us"] == original["accounts"]["us"]
    assert (tmp_path / "runtime" / "backups" / "portfolio").glob("*-before-reset-cn.json")
    backups = list((tmp_path / "runtime" / "backups" / "portfolio").glob("*-before-reset-cn.json"))
    assert len(backups) == 1
    assert "600233" in backups[0].read_text(encoding="utf-8")


def test_reset_command_refuses_non_paper_mode_without_mutation(monkeypatch, tmp_path):
    from src.investment import service

    monkeypatch.setattr(account_store, "RUNTIME", tmp_path / "runtime" / "data")
    monkeypatch.setattr(service, "ROOT", tmp_path)
    monkeypatch.setattr(service.cfg, "_data", {
        "schedule": {"timezone": "Asia/Shanghai"},
        "trading": {"mode": "live"},
    })
    original = _portfolio()
    account_store.save(original)

    with pytest.raises(RuntimeError, match="只允许 trading.mode=paper"):
        InvestmentAgentService().execute_envelope(_command(), write_audit=False)

    assert account_store.load() == original
    assert not (tmp_path / "runtime" / "backups").exists()


def test_chat_reset_tool_uses_one_authoritative_command(monkeypatch):
    calls = []

    def fake_issue(self, command, payload, **kwargs):
        calls.append((command, payload, kwargs))
        return {
            "ok": True,
            "status": "reset",
            "market": "cn",
            "backup": "runtime/backups/portfolio/test.json",
            "previous": {"holdings": 2, "trades": 5},
            "account": {"totalCapital": 500000, "cash": 500000},
        }

    monkeypatch.setattr("src.investment.command_bus.InvestmentAgentClient.issue", fake_issue)
    deps = SimpleNamespace(authoritative_report="")
    ctx = SimpleNamespace(deps=deps)

    result = asyncio.run(agent_runtime.reset_paper_account(ctx, "cn", "测试结束"))

    assert len(calls) == 1
    assert calls[0][0] == "reset_paper_account"
    assert calls[0][1] == {"market": "cn", "reason": "测试结束"}
    assert "CN 模拟账户已重置" in result["user_report"]
    assert deps.authoritative_report == result["user_report"]
