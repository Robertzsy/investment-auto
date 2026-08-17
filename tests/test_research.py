from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from src.research import backtest, loop, sandbox
from src.research.loop import ResearchOutcome, RoundReport
from src.research.workspace import ResearchWorkspace

ROOT = Path(__file__).resolve().parents[1]


# ---------- sandbox ----------

def test_sandbox_rejects_unknown_executables(tmp_path):
    with pytest.raises(ValueError, match="白名单"):
        sandbox.run_command("rm -rf something", workdir=tmp_path, project_root=tmp_path)


def test_sandbox_rejects_writing_git_subcommands(tmp_path):
    with pytest.raises(ValueError, match="只读"):
        sandbox.run_command("git push origin main", workdir=tmp_path, project_root=tmp_path)
    result = sandbox.run_command("git status --short", workdir=ROOT, project_root=ROOT)
    assert result["exit_code"] == 0


def test_sandbox_rejects_path_escape(tmp_path):
    with pytest.raises(ValueError, match="越出"):
        sandbox.run_command("python read_escape.py C:/Windows/System32", workdir=tmp_path, project_root=tmp_path)


def test_sandbox_runs_whitelisted_python(tmp_path):
    result = sandbox.run_command('python -c "print(42)"', workdir=tmp_path, project_root=tmp_path)
    assert result["exit_code"] == 0
    assert "42" in result["stdout"]


def test_sandbox_allows_read_only_ripgrep(tmp_path):
    (tmp_path / "sample.txt").write_text("needle\n", encoding="utf-8")
    result = sandbox.run_command("rg needle sample.txt", workdir=tmp_path, project_root=tmp_path)
    assert result["exit_code"] == 0
    assert "needle" in result["stdout"]


def test_sandbox_ripgrep_rejects_project_root_and_private_paths(tmp_path):
    private = tmp_path / "runtime"
    private.mkdir()
    with pytest.raises(ValueError, match="禁止扫描项目根"):
        sandbox.run_command(f'rg needle "{tmp_path}"', workdir=tmp_path, project_root=tmp_path)
    with pytest.raises(ValueError, match="私密或生成数据"):
        sandbox.run_command(f'rg needle "{private}"', workdir=tmp_path, project_root=tmp_path)


def test_sandbox_times_out(tmp_path):
    result = sandbox.run_command('python -c "import time; time.sleep(5)"', workdir=tmp_path, timeout=1, project_root=tmp_path)
    assert result["timed_out"] is True


# ---------- workspace ----------

def test_workspace_roundtrip_and_path_safety(tmp_path):
    workspace = ResearchWorkspace(tmp_path, run_id="run-1")
    workspace.initialize("目标", "backtest")
    workspace.write_file(1, "notes/result.txt", "hello")
    assert workspace.read_file("notes/result.txt") == "hello"
    assert "round-01/notes/result.txt" in workspace.list_files()
    with pytest.raises(ValueError, match="相对路径"):
        workspace.write_file(2, "../escape.txt", "x")
    reports = workspace.read_round_reports(3)
    assert reports == []


def test_workspace_round_reports(tmp_path):
    workspace = ResearchWorkspace(tmp_path, run_id="run-2")
    workspace.initialize("目标", "backtest")
    workspace.write_round_report(1, {"status": "needs_more_rounds"})
    workspace.write_round_report(2, {"status": "completed"})
    reports = workspace.read_round_reports(3)
    assert [report["status"] for report in reports] == ["needs_more_rounds", "completed"]


# ---------- loop ----------

class _FakeAgent:
    def __init__(self, reports):
        self.reports = list(reports)
        self.prompts = []

    def run_sync(self, prompt, **kwargs):
        self.prompts.append(prompt)
        if not self.reports:
            raise AssertionError("fake agent ran out of reports")
        return SimpleNamespace(output=self.reports.pop(0))


def _factory(reports):
    return lambda extra_tools=None, **kwargs: _FakeAgent(reports)


def test_loop_completes_on_first_round(tmp_path):
    factory = _factory([RoundReport(status="completed", findings=["done"])])
    outcome = loop.run_research_loop(
        "验证动量规则", "backtest", max_rounds=3,
        workspace_root=tmp_path, agent_factory=factory,
    )
    assert isinstance(outcome, ResearchOutcome)
    assert outcome.status == "completed" and outcome.rounds == 1
    assert outcome.final_report["status"] == "completed"


def test_loop_blocks_with_reason(tmp_path):
    factory = _factory([RoundReport(status="blocked", blocker_reason="行情接口超时")])
    outcome = loop.run_research_loop(
        "验证动量规则", "backtest", max_rounds=3,
        workspace_root=tmp_path, agent_factory=factory,
    )
    assert outcome.status == "blocked"
    assert outcome.blocker_reason == "行情接口超时"


def test_loop_exhausts_rounds(tmp_path):
    factory = _factory([
        RoundReport(status="needs_more_rounds"),
        RoundReport(status="needs_more_rounds"),
    ])
    outcome = loop.run_research_loop(
        "验证动量规则", "backtest", max_rounds=2,
        workspace_root=tmp_path, agent_factory=factory,
    )
    assert outcome.status == "rounds_exhausted" and outcome.rounds == 2


def test_loop_prompt_is_fresh_without_history(tmp_path):
    agent = _FakeAgent([RoundReport(status="completed")])
    captured = {}
    factory = lambda extra_tools=None, **kwargs: captured.setdefault("agent", agent) or agent
    loop.run_research_loop(
        "验证动量规则", "backtest", max_rounds=2,
        workspace_root=tmp_path, agent_factory=factory,
    )
    prompt = agent.prompts[0]
    assert "不可变目标：验证动量规则" in prompt
    assert "第 1 轮" in prompt


def test_loop_rejects_unknown_task(tmp_path):
    with pytest.raises(ValueError, match="task 必须是"):
        loop.run_research_loop("目标", "sell_everything", workspace_root=tmp_path)


def test_workspace_tools_resolve_run_context_annotations():
    """The real Pydantic tool builder must resolve nested-tool annotations."""
    tools = loop._workspace_tools(include_shell_tools=True)
    assert [tool.name for tool in tools] == [
        "list_workspace",
        "read_workspace_file",
        "write_workspace_file",
        "read_project_file",
        "run_research_command",
    ]


def test_default_agent_factory_matches_installed_pydantic_api(monkeypatch):
    from pydantic_ai.models.test import TestModel
    from src.llm import agent_model

    monkeypatch.setattr(agent_model, "resolve_agent_model", lambda **kwargs: TestModel())
    agent = loop.default_agent_factory(include_shell_tools=False)
    assert agent.name == "investment_research"


# ---------- backtest ----------

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
