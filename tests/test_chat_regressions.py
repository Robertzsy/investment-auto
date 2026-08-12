"""Deterministic regressions for chat cancellation and stock routing."""
from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, ".")

from src.llm.adapter import GenericOpenAILLM
from src.ui import agent_runtime, chat_server


def test_chat_ui_sanitizes_all_markdown_before_inner_html():
    html = (Path(__file__).parents[1] / "src" / "ui" / "index.html").read_text(encoding="utf-8")
    assert "dompurify@" in html.lower()
    assert "return sanitizeHtml(rendered);" in html
    assert "body.innerHTML = sanitizeHtml(html);" in html
    assert "appendMessage('user', renderMarkdown(text, true));" in html
    assert "appendMessage(m.role, renderMarkdown(m.content || ''));" in html

    marked_lines = [line.strip() for line in html.splitlines() if "marked.parse" in line]
    assert marked_lines == [
        "const rendered = inline ? marked.parseInline(String(text ?? '')) : marked.parse(String(text ?? ''));"
    ]
    assert "body.innerHTML = marked." not in html


@pytest.fixture(autouse=True)
def isolated_chat_state(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_server, "HISTORY_FILE", tmp_path / "chat_history.json")
    monkeypatch.setattr(chat_server, "MEMORY_FILE", tmp_path / "chat_memory.md")
    with chat_server._cancel_lock:
        chat_server._cancel_events.clear()
        chat_server._active_request_ids.clear()
    yield
    with chat_server._cancel_lock:
        chat_server._cancel_events.clear()
        chat_server._active_request_ids.clear()


class _ImmediateLLM:
    def chat_stream(self, messages, *, cancel_event, **kwargs):
        if cancel_event.is_set():
            raise InterruptedError("cancelled")
        yield "x" * 40


def _immediate_agent_events(message, *, cancel_event, **kwargs):
    if cancel_event.is_set():
        yield {"type": "cancelled"}
        return
    yield {"type": "result", "content": "x" * 40}


def test_request_cancellation_is_isolated_and_saves_only_emitted_partial(monkeypatch):
    monkeypatch.setattr("src.ui.agent_runtime.run_agent_events", _immediate_agent_events)
    first = chat_server.handle_chat_stream("普通问题一", request_id="request-a")
    second = chat_server.handle_chat_stream("普通问题二", request_id="request-b")

    assert next(first)["type"] == "status"
    first_token = next(first)
    assert first_token == {"type": "token", "content": "x" * 18}
    assert next(second)["type"] == "status"
    assert chat_server.request_cancel("request-a") is True

    assert next(first)["type"] == "cancelled"
    token = next(second)
    assert token == {"type": "token", "content": "x" * 18}
    assert next(second)["type"] == "token"
    assert next(second)["type"] == "token"
    assert next(second)["type"] == "final"

    first.close()
    second.close()
    assistants = [item["content"] for item in chat_server.load_history(20) if item["role"] == "assistant"]
    assert assistants == ["x" * 18 + "\n\n（用户已停止生成）", "x" * 40]


def test_cancel_before_request_registration_is_not_cleared(monkeypatch):
    called = False

    def unexpected_resolve(**kwargs):
        nonlocal called
        called = True
        return _ImmediateLLM()

    monkeypatch.setattr("src.llm.registry.resolve_llm", unexpected_resolve)
    assert chat_server.request_cancel("early-request") is True
    events = list(chat_server.handle_chat_stream("不会调用模型", request_id="early-request"))

    assert [event["type"] for event in events] == ["cancelled"]
    assert called is False
    history = chat_server.load_history(10)
    assert [item["role"] for item in history] == ["user", "assistant"]
    assert history[-1]["content"] == "（用户已停止生成）"


def test_missing_request_id_keeps_legacy_cancel_working(monkeypatch):
    monkeypatch.setattr("src.ui.agent_runtime.run_agent_events", _immediate_agent_events)
    events = chat_server.handle_chat_stream("旧客户端请求")
    assert next(events)["type"] == "status"
    assert chat_server.request_cancel() is True
    assert next(events)["type"] == "cancelled"
    events.close()


@pytest.mark.parametrize(
    "payload",
    [
        '{"tool_name":"run_shell","params":{"cmd":"Get-Date","timeout":5}}',
        '{"name":"run_shell","arguments":{"command":"Get-Date","timeout":5}}',
    ],
)
def test_provider_specific_tool_call_aliases_are_normalized(payload):
    parsed = chat_server._parse_tool_call("<tool_call>" + payload + "</tool_call>")

    assert parsed == {"tool": "run_shell", "params": {"cmd": "Get-Date", "timeout": 5}}


def test_legacy_tool_envelope_is_filtered_and_never_executed(monkeypatch):
    captured_history = []

    def fake_agent_events(message, *, history, **kwargs):
        captured_history.extend(history)
        yield {"type": "result", "content": "已使用新的类型化 Agent 处理。"}

    monkeypatch.setattr("src.ui.agent_runtime.run_agent_events", fake_agent_events)
    chat_server.append_history("user", "旧问题")
    chat_server.append_history(
        "assistant",
        '<tool_call>{"tool_name":"run_shell","params":{"cmd":"stale"}}</tool_call>',
    )

    events = list(chat_server.handle_chat_stream("继续回答", request_id="typed-agent-request"))

    assert events[-1] == {
        "type": "final",
        "content": "已使用新的类型化 Agent 处理。",
    }
    assert all(
        "tool_name" not in item["content"]
        for item in captured_history
        if item["role"] == "assistant"
    )
    assert chat_server.load_history(10)[-1]["content"] == "已使用新的类型化 Agent 处理。"


def test_typed_agent_catalog_excludes_shell_file_write_and_trading_execution():
    tool_names = set(agent_runtime.MANAGER_AGENT._function_toolset.tools)

    assert tool_names == {
        "consult_portfolio_agent",
        "consult_risk_agent",
        "consult_report_agent",
        "consult_ops_agent",
        "search_security",
        "get_security_snapshot",
        "get_stock_screening",
    }
    assert not ({"run_shell", "write_file", "execute_orders"} & tool_names)


def test_run_one_market_round_routes_to_complete_investment_cycle(monkeypatch):
    captured = {}

    def fake_cycle(market, *, label, progress_callback=None):
        captured.update({"market": market, "label": label})
        if progress_callback:
            progress_callback("正在分析")
        return {
            "status": "generated", "market": market, "report": "/tmp/report.md",
            "autonomous": {"fills": []}, "notification": {"status": "disabled"},
        }

    monkeypatch.setattr("src.scheduler.run_investment_cycle", fake_cycle)
    events = list(chat_server.handle_chat_stream("跑一轮美股", request_id="complete-cycle"))

    assert captured == {"market": "us", "label": "chat"}
    assert events[0] == {"type": "tool", "name": "full_investment_cycle", "params": {"market": "us"}}
    assert any(event == {"type": "status", "content": "正在分析"} for event in events)
    assert "全市场选股" in events[-1]["content"]


def test_run_one_us_analysis_routes_to_complete_investment_cycle(monkeypatch):
    monkeypatch.setattr("src.scheduler.run_investment_cycle", lambda market, **kwargs: {
        "status": "generated", "market": market, "report": "",
        "autonomous": {"status": "no_trade", "fills": []},
        "notification": {"status": "disabled"},
    })

    events = list(chat_server.handle_chat_stream("跑一次美股分析", request_id="one-us-analysis"))

    assert events[0] == {"type": "tool", "name": "full_investment_cycle", "params": {"market": "us"}}
    assert "完整投资轮次" in events[-1]["content"]


def test_conduct_one_us_analysis_is_complete_cycle_intent():
    assert chat_server._full_cycle_request("进行一次美股分析") == {"market": "us"}


def test_complete_analysis_without_market_reuses_recent_user_market(monkeypatch):
    monkeypatch.setattr(chat_server, "load_history", lambda limit=20: [
        {"role": "user", "content": "跑一次美股分析"},
        {"role": "assistant", "content": "上次结果"},
        {"role": "user", "content": "跑一轮完整的分析"},
    ])
    monkeypatch.setattr("src.scheduler.run_investment_cycle", lambda market, **kwargs: {
        "status": "generated", "market": market, "report": "",
        "autonomous": {"status": "no_trade", "fills": []},
        "notification": {"status": "disabled"},
    })

    events = list(chat_server.handle_chat_stream("跑一轮完整的分析", request_id="context-market"))

    assert events[0]["params"] == {"market": "us"}


def test_complete_analysis_without_any_market_asks_instead_of_running_agent(monkeypatch):
    monkeypatch.setattr(chat_server, "load_history", lambda limit=20: [
        {"role": "user", "content": "跑一轮完整的分析"},
    ])
    monkeypatch.setattr(
        "src.ui.agent_runtime.run_agent_events",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("generic agent must not run")),
    )

    events = list(chat_server.handle_chat_stream("跑一轮完整的分析", request_id="missing-market"))

    assert events[-1]["type"] == "final"
    assert "请指定" in events[-1]["content"]


def test_agent_limit_question_has_deterministic_answer():
    events = list(chat_server.handle_chat_stream("现在的工具请求上限是多少", request_id="agent-limits"))

    assert events[0]["name"] == "agent_limits"
    assert "模型请求**：每轮最多 6 次" in events[-1]["content"]
    assert "工具调用**：每轮最多 8 次" in events[-1]["content"]
    assert "32,000" in events[-1]["content"]


def test_explicit_screen_only_request_does_not_trigger_complete_cycle():
    assert chat_server._full_cycle_request("只筛选一轮美股") is None


def test_start_market_trading_means_one_complete_round_not_permission_change():
    assert chat_server._full_cycle_request("开始美股交易") == {"market": "us"}
    assert chat_server._full_cycle_request("美股开始了吗") is None


class _MarketStatusConfig:
    schedule = {
        "timezone": "Asia/Shanghai",
        "weekdays_only": True,
        "us_early_morning_days": "1-5",
    }
    autonomous = {"auto_execute": True}
    enabled_markets = ["us"]

    @staticmethod
    def market_config(market):
        assert market == "us"
        return {
            "trading": {
                "session": [
                    {"start": "21:30", "end": "04:00", "note": "Beijing time"}
                ]
            }
        }

    @staticmethod
    def intraday_times(market):
        assert market == "us"
        return ["21:35", "23:30", "01:00"]

    @staticmethod
    def close_time(market):
        assert market == "us"
        return "04:10"


def test_market_status_uses_current_clock_actual_report_and_pause_state(
    monkeypatch, tmp_path
):
    from src import scheduler
    from src.trading import control, controller

    fake_cfg = _MarketStatusConfig()
    monkeypatch.setattr(chat_server, "cfg", fake_cfg)
    monkeypatch.setattr(scheduler, "cfg", fake_cfg)
    monkeypatch.setattr(scheduler, "REPORT_DIR", tmp_path)
    monkeypatch.setattr(
        control,
        "load_state",
        lambda: {
            "paused": True,
            "kill_switch": False,
            "reason": "等待 dry-run 验证",
        },
    )
    monkeypatch.setattr(controller, "autonomous_enabled", lambda: True)
    (tmp_path / "20260811-us-2330.md").write_text(
        "# US 2330 轮次报告\n\n"
        "> 生成时间：2026-08-11T23:30:00+08:00 | 模式：定时运行\n",
        encoding="utf-8",
    )
    current = datetime(2026, 8, 11, 23, 34, 45, tzinfo=ZoneInfo("Asia/Shanghai"))

    answer = chat_server._build_market_status_answer("美股开始了吗", now=current)

    assert answer is not None
    assert "2026-08-11 23:34:45" in answer
    assert "Asia/Shanghai，UTC+08:00" in answer
    assert "当前是否在交易时段**：是" in answer
    assert "23:30 已完成" in answer
    assert "2026-08-12 01:00:00" in answer
    assert "2026-08-12 04:10:00" in answer
    assert "自主交易控制**：已暂停" in answer
    assert "不会提交任何模拟订单" in answer
    assert "15:10" not in answer


def test_us_cross_midnight_session_is_active(monkeypatch):
    from src import scheduler

    fake_cfg = _MarketStatusConfig()
    monkeypatch.setattr(chat_server, "cfg", fake_cfg)
    monkeypatch.setattr(scheduler, "cfg", fake_cfg)
    sessions = fake_cfg.market_config("us")["trading"]["session"]

    current = datetime(2026, 8, 12, 1, 15, tzinfo=ZoneInfo("Asia/Shanghai"))

    assert chat_server._session_is_active("us", current, sessions) is True


def test_market_status_question_bypasses_llm(monkeypatch):
    monkeypatch.setattr(
        chat_server,
        "_build_market_status_answer",
        lambda message: "北京时间 23:34，美股 23:30 轮次已完成，自主交易已暂停。",
    )
    monkeypatch.setattr(
        "src.llm.registry.resolve_llm",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("LLM must not be called")),
    )

    events = list(
        chat_server.handle_chat_stream("美股开始了吗", request_id="market-status")
    )

    assert events[0] == {"type": "tool", "name": "market_status", "params": {}}
    assert events[-1]["type"] == "final"
    assert "23:30 轮次已完成" in events[-1]["content"]


class _AutonomyControlConfig:
    schedule = {"timezone": "Asia/Shanghai"}
    autonomous = {"enabled": True, "auto_execute": True}
    trading = {"mode": "paper"}
    enabled_markets = ["cn", "hk", "us", "etf"]


def test_market_scoped_start_command_runs_complete_cycle_without_unlocking_pause(
    monkeypatch, tmp_path
):
    from src.trading import control

    control_file = tmp_path / "control.json"
    monkeypatch.setattr(control, "CONTROL_FILE", control_file)
    monkeypatch.setattr(chat_server, "cfg", _AutonomyControlConfig())
    control.set_paused(True, reason="等待人工确认")
    monkeypatch.setattr("src.scheduler.run_investment_cycle", lambda market, **kwargs: {
        "status": "generated", "market": market, "report": "",
        "autonomous": {"status": "paused", "fills": []},
        "notification": {"status": "disabled"},
    })

    events = list(
        chat_server.handle_chat_stream("开始美股交易", request_id="market-scoped-resume")
    )

    assert events[0] == {"type": "tool", "name": "full_investment_cycle", "params": {"market": "us"}}
    assert events[-1]["type"] == "final"
    assert "完整投资轮次" in events[-1]["content"]
    assert control.load_state()["paused"] is True


def test_full_cycle_result_does_not_report_paused_round_as_success():
    answer = chat_server._format_full_cycle_result({
        "status": "generated",
        "market": "us",
        "report": "",
        "notification": {"status": "skipped", "reason": "missing webhook"},
        "autonomous": {
            "status": "paused",
            "control": {"reason": "人工暂停"},
            "fills": [],
        },
    })
    assert "⚠️" in answer
    assert "运行时安全暂停" in answer
    assert "人工暂停" in answer


def test_explicit_global_resume_command_clears_runtime_pause(monkeypatch, tmp_path):
    from src.trading import control

    control_file = tmp_path / "control.json"
    monkeypatch.setattr(control, "CONTROL_FILE", control_file)
    monkeypatch.setattr(chat_server, "cfg", _AutonomyControlConfig())
    monkeypatch.delenv("AUTONOMOUS_TRADING_ENABLED", raising=False)
    control.set_paused(True, reason="等待人工确认")
    monkeypatch.setattr(
        "src.llm.registry.resolve_llm",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("LLM must not be called")),
    )

    events = list(
        chat_server.handle_chat_stream(
            "解除全局暂停并恢复自主模拟交易",
            request_id="global-resume",
        )
    )

    assert events[0] == {"type": "tool", "name": "autonomy_control", "params": {}}
    assert "已解除全局暂停" in events[-1]["content"]
    assert "A 股、港股、美股、ETF" in events[-1]["content"]
    state = control.load_state()
    assert state["paused"] is False
    assert state["updated_by"] == "human"


def test_market_status_question_is_not_misclassified_as_control_command():
    assert chat_server._autonomy_control_request("美股开始了吗") is None


def test_autonomy_status_is_deterministic_and_explains_pause_semantics(monkeypatch):
    from src.trading import control, controller

    monkeypatch.setattr(chat_server, "cfg", _AutonomyControlConfig())
    monkeypatch.setattr(controller, "autonomous_enabled", lambda config=None: True)
    monkeypatch.setattr(
        control,
        "load_state",
        lambda: {
            "paused": True,
            "kill_switch": False,
            "reason": "等待 dry-run 验证",
        },
    )
    monkeypatch.setattr(
        "src.ui.agent_runtime.run_agent_events",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("Agent must not run")),
    )

    events = list(
        chat_server.handle_chat_stream(
            "为什么自主交易开关打开了但仍然暂停？",
            request_id="autonomy-status",
        )
    )

    assert events[0] == {"type": "tool", "name": "autonomy_status", "params": {}}
    assert events[-1]["type"] == "final"
    assert "配置开关**：已打开" in events[-1]["content"]
    assert "运行时暂停**：是" in events[-1]["content"]
    assert "调度和报告继续运行" in events[-1]["content"]
    assert "模拟订单不会提交" in events[-1]["content"]


@pytest.mark.parametrize("text", ["分析 A 股市场", "分析项目代码", "看看配置"])
def test_generic_analysis_requests_do_not_enter_stock_route(monkeypatch, text):
    calls = []
    monkeypatch.setattr(chat_server, "_stock_fetcher", lambda command, value: calls.append((command, value)))
    assert chat_server._build_stock_analysis_context(text) is None
    assert calls == []


def test_unknown_chinese_stock_search_does_not_fall_through_to_snapshot(monkeypatch):
    calls = []

    def fake_fetch(command, value):
        calls.append((command, value))
        return {"count": 0, "stocks": []}

    monkeypatch.setattr(chat_server, "_stock_fetcher", fake_fetch)
    assert chat_server._build_stock_analysis_context("分析火星股份走势") is None
    assert calls == [("search", "火星股份")]


@pytest.mark.parametrize(
    ("text", "expected_query", "expected_code", "expects_search"),
    [
        ("分析茅台", "茅台", "sh600519", True),
        ("AAPL走势", "AAPL", "AAPL", False),
        ("hk00700", "hk00700", "hk00700", False),
        ("00700", "00700", "00700", False),
    ],
)
def test_security_stock_requests_still_resolve(monkeypatch, text, expected_query, expected_code, expects_search):
    calls = []

    def fake_fetch(command, value):
        calls.append((command, value))
        if command == "search":
            return {"count": 1, "stocks": [{"symbol": "sh600519", "name": "贵州茅台"}]}
        return {"code": value, "price": 123.0}

    monkeypatch.setattr(chat_server, "_stock_fetcher", fake_fetch)
    context = chat_server._build_stock_analysis_context(text)
    assert context is not None
    assert context["query"] == expected_query
    assert context["resolved_code"] == expected_code
    assert calls[-1] == ("snapshot", expected_code)
    assert any(command == "search" for command, _ in calls) is expects_search


class _BlockingStream:
    def __init__(self):
        self.entered = threading.Event()
        self.closed = threading.Event()

    def __iter__(self):
        return self

    def __next__(self):
        self.entered.set()
        if not self.closed.wait(2):
            raise AssertionError("cancellation did not close the HTTP stream")
        raise RuntimeError("transport closed")

    def close(self):
        self.closed.set()


class _FakeCompletions:
    def __init__(self, stream):
        self.stream = stream
        self.params = None

    def create(self, **params):
        self.params = params
        return self.stream


def test_generic_openai_stream_closes_inflight_http_read_on_cancel():
    transport = _BlockingStream()
    completions = _FakeCompletions(transport)
    llm = GenericOpenAILLM.__new__(GenericOpenAILLM)
    llm._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    llm._model = "test-model"
    llm._provider_name = "test"
    cancel_event = threading.Event()

    def cancel_after_read_starts():
        assert transport.entered.wait(1)
        cancel_event.set()

    canceller = threading.Thread(target=cancel_after_read_starts, daemon=True)
    canceller.start()
    with pytest.raises(InterruptedError):
        list(llm.chat_stream([{"role": "user", "content": "hi"}], cancel_event=cancel_event))
    canceller.join(timeout=1)

    assert transport.closed.is_set()
    assert completions.params["stream"] is True
