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
    assert "appendMessage('user', renderMarkdown(displayText, true));" in html
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


def test_typed_manager_catalog_exposes_versioned_management_not_shell_or_execution():
    tool_names = set(agent_runtime.MANAGER_AGENT._function_toolset.tools)

    assert tool_names == {
        "consult_portfolio_agent",
        "consult_risk_agent",
        "consult_report_agent",
        "consult_ops_agent",
        "search_security",
        "get_security_snapshot",
        "get_stock_screening",
        "run_complete_investment_cycle",
        "manage_investment_agent",
        "run_portfolio_optimizer",
        "inspect_investment_agent_code",
        "modify_investment_agent_code",
        "remember_user_preference",
        "search_project",
        "list_manager_capabilities",
        "get_cycle_evidence",
        "install_manager_skill",
        "load_manager_skill",
        "install_manager_tool",
    }
    assert not ({"run_shell", "write_file", "execute_orders"} & tool_names)


@pytest.mark.parametrize(
    "message",
    ["跑一次美股分析", "进行一次美股分析", "跑一轮美股", "开始美股交易", "跑一轮完整的分析"],
)
def test_investment_language_is_understood_by_agent_not_keyword_router(monkeypatch, message):
    captured = []

    def fake_agent_events(value, **kwargs):
        captured.append(value)
        yield {"type": "result", "content": "由常驻 Agent 理解并处理"}

    monkeypatch.setattr("src.ui.agent_runtime.run_agent_events", fake_agent_events)
    events = list(chat_server.handle_chat_stream(message, request_id="semantic-agent"))

    assert captured == [message]
    assert events[-1]["content"] == "由常驻 Agent 理解并处理"
    assert not hasattr(chat_server, "_full_cycle_request")


def test_button_cycle_stream_runs_directly_without_llm(monkeypatch):
    monkeypatch.setenv("INVESTMENT_AGENT_TRANSPORT", "local")
    monkeypatch.setattr("src.scheduler.run_investment_cycle", lambda market, **kwargs: {
        "status": "generated", "market": market, "report": "",
        "autonomous": {"status": "no_trade", "fills": []},
        "notification": {"status": "disabled"},
    })
    monkeypatch.setattr(
        "src.ui.agent_runtime.run_agent_events",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("LLM must not run")),
    )

    events = list(chat_server.handle_investment_cycle_stream("us", request_id="button-cycle"))

    assert events[0] == {"type": "tool", "name": "run_complete_investment_cycle", "params": {"market": "us"}}
    assert "美股完整投资轮次" in events[-1]["content"]


def test_agent_limit_question_is_understood_by_manager_not_keyword_router(monkeypatch):
    monkeypatch.setattr(
        "src.ui.agent_runtime.run_agent_events",
        lambda *args, **kwargs: iter([{"type": "result", "content": "当前限制由管理 Agent 解释"}]),
    )
    events = list(chat_server.handle_chat_stream("现在的工具请求上限是多少", request_id="agent-limits"))
    assert events[-1]["content"] == "当前限制由管理 Agent 解释"


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


def test_market_status_question_uses_manager_tool_reasoning(monkeypatch):
    monkeypatch.setattr("src.ui.agent_runtime.run_agent_events", lambda *args, **kwargs: iter([
        {"type": "tool", "name": "consult_ops_agent", "params": {}},
        {"type": "result", "content": "北京时间 23:34，美股 23:30 轮次已完成，自主交易已暂停。"},
    ]))

    events = list(
        chat_server.handle_chat_stream("美股开始了吗", request_id="market-status")
    )

    assert events[0]["type"] == "status"
    assert events[1]["name"] == "consult_ops_agent"
    assert events[-1]["type"] == "final"
    assert "23:30 轮次已完成" in events[-1]["content"]


class _AutonomyControlConfig:
    schedule = {"timezone": "Asia/Shanghai"}
    autonomous = {"enabled": True, "auto_execute": True}
    trading = {"mode": "paper"}
    enabled_markets = ["cn", "hk", "us", "etf"]


def test_market_scoped_start_language_does_not_unlock_runtime_pause(
    monkeypatch, tmp_path
):
    from src.trading import control

    control_file = tmp_path / "control.json"
    monkeypatch.setattr(control, "CONTROL_FILE", control_file)
    monkeypatch.setattr(chat_server, "cfg", _AutonomyControlConfig())
    control.set_paused(True, reason="等待人工确认")
    monkeypatch.setattr("src.ui.agent_runtime.run_agent_events", _immediate_agent_events)

    events = list(
        chat_server.handle_chat_stream("开始美股交易", request_id="market-scoped-resume")
    )

    assert events[-1]["type"] == "final"
    assert control.load_state()["paused"] is True


def test_full_cycle_result_does_not_report_paused_round_as_success():
    from src.investment.reporting import format_cycle_result

    answer = format_cycle_result({
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


def test_explicit_global_resume_command_is_delegated_to_manager(monkeypatch, tmp_path):
    from src.trading import control

    control_file = tmp_path / "control.json"
    monkeypatch.setattr(control, "CONTROL_FILE", control_file)
    monkeypatch.setattr(chat_server, "cfg", _AutonomyControlConfig())
    monkeypatch.delenv("AUTONOMOUS_TRADING_ENABLED", raising=False)
    control.set_paused(True, reason="等待人工确认")
    def fake_events(*args, **kwargs):
        control.set_paused(False, reason="管理 Agent 恢复", updated_by="conversation-manager")
        yield {"type": "tool", "name": "manage_investment_agent", "params": {"action": "resume"}}
        yield {"type": "result", "content": "已解除全局暂停"}
    monkeypatch.setattr("src.ui.agent_runtime.run_agent_events", fake_events)

    events = list(
        chat_server.handle_chat_stream(
            "解除全局暂停并恢复自主模拟交易",
            request_id="global-resume",
        )
    )

    assert events[1]["name"] == "manage_investment_agent"
    assert "已解除全局暂停" in events[-1]["content"]
    state = control.load_state()
    assert state["paused"] is False
    assert state["updated_by"] == "conversation-manager"


def test_autonomy_status_is_answered_through_manager_tool(monkeypatch):
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
    monkeypatch.setattr("src.ui.agent_runtime.run_agent_events", lambda *args, **kwargs: iter([
        {"type": "tool", "name": "manage_investment_agent", "params": {"action": "status"}},
        {"type": "result", "content": "配置开关**：已打开\n运行时暂停**：是\n调度和报告继续运行，模拟订单不会提交"},
    ]))

    events = list(
        chat_server.handle_chat_stream(
            "为什么自主交易开关打开了但仍然暂停？",
            request_id="autonomy-status",
        )
    )

    assert events[1]["name"] == "manage_investment_agent"
    assert events[-1]["type"] == "final"
    assert "配置开关**：已打开" in events[-1]["content"]
    assert "运行时暂停**：是" in events[-1]["content"]
    assert "调度和报告继续运行" in events[-1]["content"]
    assert "模拟订单不会提交" in events[-1]["content"]



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
