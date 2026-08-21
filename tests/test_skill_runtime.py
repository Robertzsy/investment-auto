from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path

import pytest

from src.manager.action_registry import ActionRegistry
from src.manager.execution_trace import ExecutionTrace
from src.manager.session_store import SessionStore
from src.manager.skill_builder import SkillBuilder
from src.manager.skill_registry import SkillRegistry
from src.manager.skill_runtime import SkillRuntime
from src.manager.skill_scheduler import SkillScheduler
from src.manager.skill_selector import SkillSelector


def _install_test_skill(registry: SkillRegistry, *, output_path: str = "report.user_report") -> None:
    registry.install(
        manifest={
            "name": "test-research",
            "description": "测试证券研究工作流",
            "version": "1.0.0",
            "intents": ["test_research"],
            "triggers": ["测试研究"],
            "session_scope": "investment_research",
            "side_effect_level": "read_only",
            "allowed_actions": ["test.resolve", "test.report"],
            "priority": 10,
        },
        instructions="# Test\n\nAlways use verified test evidence.",
        workflow={
            "steps": [
                {"id": "resolve", "action": "test.resolve", "required": True},
                {"id": "report", "action": "test.report", "required": True},
            ]
        },
        completion_contract={
            "required_steps": ["resolve", "report"],
            "required_outputs": ["resolve.entities", output_path],
        },
    )


def test_builtin_skill_catalog_and_selector_cover_core_requests():
    registry = SkillRegistry()
    names = {item.manifest.name for item in registry.catalog()}
    assert {
        "security-analysis",
        "stock-screening",
        "portfolio-review",
        "portfolio-optimization",
        "complete-investment-cycle",
        "scheduled-market-cycle",
        "account-management",
        "system-administration",
        "market-overview",
        "incident-repair",
    } <= names
    selector = SkillSelector(registry)
    assert selector.select("分析一下贵州茅台").manifest.name == "security-analysis"
    assert selector.select("今天有什么值得关注的股票").manifest.name == "stock-screening"
    assert selector.select("帮我检查美股持仓风险").manifest.name == "portfolio-review"
    assert selector.select("运行一次美股完整投资").manifest.name == "complete-investment-cycle"
    assert selector.select("今天的A股行情怎么样").manifest.name == "market-overview"


def test_runtime_executes_workflow_validates_and_persists_session(tmp_path):
    registry = SkillRegistry(builtin_root=tmp_path / "empty", custom_root=tmp_path / "skills")
    _install_test_skill(registry)
    actions = ActionRegistry(include_dynamic=False)
    actions.register(
        "test.resolve",
        lambda inputs, context: {"entities": ["AAPL"]},
        "test resolver",
        session_scopes={"investment_research"},
        side_effect_level="read_only",
    )
    actions.register(
        "test.report",
        lambda inputs, context: {
            "entities": context["state"]["outputs"]["resolve"]["entities"],
            "user_report": "verified AAPL report",
        },
        "test report",
        session_scopes={"investment_research"},
        side_effect_level="read_only",
    )
    sessions = SessionStore(root=tmp_path / "sessions")
    result = SkillRuntime(registry=registry, actions=actions, sessions=sessions).run(
        "测试研究 AAPL", skill_name="test-research", inputs={"symbols": "AAPL"}
    )
    assert result["status"] == "completed"
    assert result["validation"]["passed"] is True
    assert result["user_report"] == "verified AAPL report"
    assert Path(result["trace_path"]).is_file()
    assert sessions.load("investment_research")["last_skill"] == "test-research"
    assert sessions.load("investment_research")["entities"] == ["AAPL"]


def test_runtime_does_not_equate_action_call_with_verification(tmp_path):
    registry = SkillRegistry(builtin_root=tmp_path / "empty", custom_root=tmp_path / "skills")
    _install_test_skill(registry, output_path="report.missing_contract_field")
    actions = ActionRegistry(include_dynamic=False)
    actions.register(
        "test.resolve",
        lambda inputs, context: {"entities": ["AAPL"]},
        "test resolver",
        session_scopes={"investment_research"},
        side_effect_level="read_only",
    )
    actions.register(
        "test.report",
        lambda inputs, context: {"user_report": "report exists but contract does not pass"},
        "test report",
        session_scopes={"investment_research"},
        side_effect_level="read_only",
    )
    result = SkillRuntime(
        registry=registry,
        actions=actions,
        sessions=SessionStore(root=tmp_path / "sessions"),
    ).run("测试研究 AAPL", skill_name="test-research")
    assert result["status"] == "incomplete"
    assert result["validation"]["passed"] is False
    assert result["validation"]["missing_outputs"] == ["report.missing_contract_field"]


def test_one_sentence_security_request_runs_full_evidence_skill(monkeypatch, tmp_path):
    calls = []

    def stock_fetcher(command, value):
        calls.append((command, value))
        if command == "search":
            return {"results": [{"code": "600519", "name": "贵州茅台", "market": "cn"}]}
        return {
            "realtime": {
                "code": "sh600519",
                "time": datetime.now().strftime("%Y%m%d%H%M%S"),
                "price": 1688.0,
                "changePercent": 1.2,
                "pe": 24.0,
            },
            "indicators": {"ma20": 1650.0},
        }

    monkeypatch.setattr("src.platform.market_tools.stock_fetcher", stock_fetcher)
    monkeypatch.setattr(
        "src.data.research.fetch_research_packet",
        lambda market, symbol: {
            "market": market,
            "symbol": symbol,
            "fundamentals": {"roe_ttm": 31.2},
            "news": [],
            "sentiment": {},
            "sources": ["licensed:test"],
            "errors": {},
        },
    )
    class Analyst:
        provider_name = "test"

        def chat(self, messages, **kwargs):
            return "贵州茅台当前估值判断需要结合增长持续性；主要风险是增长放缓与估值压缩。"

    monkeypatch.setattr("src.llm.registry.resolve_llm", lambda **kwargs: Analyst())
    result = SkillRuntime(sessions=SessionStore(root=tmp_path / "sessions")).run("分析一下贵州茅台")
    assert result["status"] == "completed"
    assert result["skill"] == "security-analysis"
    assert result["validation"]["passed"] is True
    assert "贵州茅台（600519）" in result["user_report"]
    assert "1,688" in result["user_report"]
    assert "估值压缩" in result["user_report"]
    assert calls[0] == ("search", "贵州茅台")
    assert calls[1] == ("snapshot", "sh600519")


def test_security_identity_mismatch_never_completes(monkeypatch, tmp_path):
    calls = []

    def stock_fetcher(command, value):
        calls.append((command, value))
        if command == "search":
            return {
                "results": [{
                    "code": "000002",
                    "symbol": "sh000002",
                    "name": r"A\u80a1\u6307\u6570",
                    "market": "沪市",
                    "type": "ZS",
                }]
            }
        return {
            "realtime": {
                "code": "sz000002",
                "name": "万科A",
                "time": datetime.now().strftime("%Y%m%d%H%M%S"),
                "price": 3.14,
            },
            "history": [],
            "indicators": {},
        }

    monkeypatch.setattr("src.platform.market_tools.stock_fetcher", stock_fetcher)
    result = SkillRuntime(sessions=SessionStore(root=tmp_path / "sessions")).run(
        "分析A股指数", skill_name="security-analysis"
    )
    assert result["status"] == "incomplete"
    assert result["validation"]["passed"] is False
    assert result["user_report"] == ""
    assert calls[1:] == [("snapshot", "sh000002"), ("snapshot", "sh000002")]
    resolved = result["outputs"]["resolve"]["securities"][0]
    assert resolved["name"] == "A股指数"
    assert resolved["provider_symbol"] == "sh000002"
    assert resolved["asset_type"] == "index"


def test_market_overview_requires_indexes_breadth_and_freshness(monkeypatch, tmp_path):
    now = datetime.now().strftime("%Y%m%d%H%M%S")

    def stock_fetcher(command, value, option=""):
        if command == "realtime":
            return {
                "code": value,
                "name": value,
                "time": now,
                "price": 3000.0,
                "change_pct": 1.0,
                "amount": 100.0,
            }
        if command == "market-list":
            return {
                "source": "test-market",
                "scope": "bounded",
                "data": [
                    {"change_pct": 1.0, "amount": 50.0},
                    {"change_pct": -1.0, "amount": 40.0},
                ],
            }
        raise AssertionError((command, value, option))

    monkeypatch.setattr("src.platform.market_tools.stock_fetcher", stock_fetcher)
    result = SkillRuntime(sessions=SessionStore(root=tmp_path / "sessions")).run(
        "今天的A股行情怎么样"
    )
    assert result["skill"] == "market-overview"
    assert result["status"] == "completed"
    assert result["validation"]["passed"] is True
    assert result["outputs"]["quality"]["coverage_ratio"] == 0.8
    assert "上涨 / 下跌 / 平盘：1 / 1 / 0" in result["user_report"]
    assert "万科A" not in result["user_report"]


def test_runtime_rejects_skill_action_permission_escalation(tmp_path):
    registry = SkillRegistry(builtin_root=tmp_path / "empty", custom_root=tmp_path / "skills")
    registry.install(
        manifest={
            "name": "unsafe-research",
            "description": "invalid escalation",
            "version": "1.0.0",
            "intents": ["unsafe"],
            "triggers": ["unsafe"],
            "session_scope": "investment_research",
            "side_effect_level": "read_only",
            "allowed_actions": ["test.execute"],
        },
        instructions="# Unsafe",
        workflow={"steps": [{"id": "execute", "action": "test.execute"}]},
        completion_contract={"required_steps": ["execute"], "required_outputs": []},
    )
    actions = ActionRegistry(include_dynamic=False)
    actions.register(
        "test.execute",
        lambda inputs, context: {"ok": True},
        "execution action",
        session_scopes={"investment_research"},
        side_effect_level="investment_execution",
    )
    with pytest.raises(PermissionError, match="副作用等级"):
        SkillRuntime(registry=registry, actions=actions).run(
            "unsafe", skill_name="unsafe-research"
        )


def test_skill_builder_installs_tests_and_fulfills_existing_actions(tmp_path):
    registry = SkillRegistry(custom_root=tmp_path / "skills")
    result = SkillBuilder(registry).create(
        manifest={
            "name": "capability-audit",
            "description": "列出 Harness 能力并形成报告",
            "version": "1.0.0",
            "intents": ["capability_audit"],
            "triggers": ["审计能力"],
            "session_scope": "system_admin",
            "side_effect_level": "read_only",
            "allowed_actions": ["system.capabilities", "report.system"],
        },
        instructions="# Capability Audit\n\nRead the catalog and report it.",
        workflow={
            "steps": [
                {"id": "catalog", "action": "system.capabilities"},
                {"id": "report", "action": "report.system"},
            ]
        },
        completion_contract={
            "required_steps": ["catalog", "report"],
            "required_outputs": ["catalog.skills", "report.user_report"],
        },
        test_inputs={},
        fulfill_request="审计能力",
        fulfill_inputs={},
    )
    assert result["status"] == "created_and_fulfilled"
    assert result["test_result"]["validation"]["passed"] is True
    assert result["fulfill_result"]["status"] == "completed"
    assert registry.get("capability-audit").manifest.session_scope == "system_admin"


def test_skill_scheduler_persists_structured_inputs_and_pinned_version(tmp_path, monkeypatch):
    monkeypatch.setattr("src.scheduler.refresh_skill_schedules", lambda: 0)
    scheduler = SkillScheduler(root=tmp_path / "schedules")
    result = scheduler.create(
        skill_name="scheduled-market-cycle",
        cron="0 9 * * 1-5",
        inputs={"market": "us", "cycle_type": "intraday", "account": "accounts.us"},
        schedule_id="us-market-open",
        timezone="Asia/Shanghai",
    )
    assert result["status"] == "scheduled"
    saved = scheduler.get("us-market-open")
    assert saved["skill_version"] == SkillRegistry().get("scheduled-market-cycle").manifest.version
    assert saved["inputs"]["account"] == "accounts.us"
    assert "request" not in saved["inputs"]
    research = scheduler.create(
        skill_name="portfolio-review",
        cron="30 18 * * 1-5",
        inputs={"market": "us"},
        schedule_id="us-portfolio-risk",
        timezone="Asia/Shanghai",
    )
    assert research["skill_name"] == "portfolio-review"
    disabled = scheduler.set_enabled("us-portfolio-risk", False)
    assert disabled["status"] == "disabled"
    assert scheduler.get("us-portfolio-risk")["enabled"] is False
    enabled = scheduler.set_enabled("us-portfolio-risk", True)
    assert enabled["status"] == "enabled"
    assert scheduler.get("us-portfolio-risk")["enabled"] is True
    deleted = scheduler.delete("us-portfolio-risk")
    assert deleted == {"status": "deleted", "schedule_id": "us-portfolio-risk"}
    with pytest.raises(FileNotFoundError):
        scheduler.get("us-portfolio-risk")
    with pytest.raises(ValueError, match="不得保存密钥"):
        scheduler.create(
            skill_name="portfolio-review",
            cron="0 20 * * 1-5",
            inputs={"market": "us", "api_key": "sk-" + "secret-value-123456"},
            schedule_id="unsafe-secret-job",
        )


def test_execution_trace_redacts_secret_shaped_inputs(tmp_path):
    secret = "sk-" + "trace-secret-123456789"
    trace = ExecutionTrace(
        request=f"配置 API Key: {secret}",
        skill="test-skill",
        version="1.0.0",
        session_scope="system_admin",
        inputs={"api_key": secret},
        root=tmp_path / "traces",
    )
    trace.finish(status="incomplete", validation={"passed": False}, result={"error": secret})
    raw = trace.path.read_text(encoding="utf-8")
    assert secret not in raw
    assert "********" in raw
    recent = ExecutionTrace.recent(root=tmp_path / "traces")
    assert recent[0]["execution_id"] == trace.execution_id
    assert recent[0]["status"] == "incomplete"
    loaded = ExecutionTrace.load(trace.execution_id, root=tmp_path / "traces")
    assert loaded["validation"]["passed"] is False
    with pytest.raises(ValueError, match="execution_id"):
        ExecutionTrace.load("../unsafe", root=tmp_path / "traces")
