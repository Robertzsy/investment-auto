"""Deterministic regressions for chat cancellation and stock routing."""
from __future__ import annotations

import sys
import threading
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, ".")

from src.llm.adapter import GenericOpenAILLM
from src.ui import chat_server


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


class _ToolThenAnswerLLM:
    def __init__(self):
        self.round = 0
        self.messages = []

    def chat_stream(self, messages, *, cancel_event, **kwargs):
        self.messages.append([dict(item) for item in messages])
        if self.round == 0:
            self.round += 1
            yield (
                "好的，我来检查。\n\n<tool_call>\n"
                '{"tool_name":"run_shell","params":{"cmd":"Get-Date","timeout":5}}'
                "\n</tool_call>"
            )
            return
        assert "[工具执行结果]" in messages[-1]["content"]
        yield "美股调度器正在运行，状态检查已完成。"


def test_request_cancellation_is_isolated_and_saves_only_emitted_partial(monkeypatch):
    monkeypatch.setattr("src.llm.registry.resolve_llm", lambda **kwargs: _ImmediateLLM())
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
    monkeypatch.setattr("src.llm.registry.resolve_llm", lambda **kwargs: _ImmediateLLM())
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


def test_tool_name_envelope_executes_and_continues_to_final_answer(monkeypatch):
    llm = _ToolThenAnswerLLM()
    executions = []
    monkeypatch.setattr("src.llm.registry.resolve_llm", lambda **kwargs: llm)
    monkeypatch.setitem(
        chat_server.TOOLS,
        "run_shell",
        {"fn": lambda **params: executions.append(params) or {"stdout": "23:18:00", "returncode": 0}},
    )
    chat_server.append_history("user", "旧问题")
    chat_server.append_history(
        "assistant",
        '<tool_call>{"tool_name":"run_shell","params":{"cmd":"stale"}}</tool_call>',
    )

    events = list(
        chat_server.handle_chat_stream("美股开始操作了吗", request_id="tool-alias-request")
    )

    assert executions == [{"cmd": "Get-Date", "timeout": 5}]
    assert [event["type"] for event in events].count("tool") == 1
    assert events[-1] == {
        "type": "final",
        "content": "美股调度器正在运行，状态检查已完成。",
    }
    assert all("tool_name" not in event.get("content", "") for event in events if event["type"] == "token")
    assert all(
        "tool_name" not in item["content"]
        for item in llm.messages[0]
        if item["role"] == "assistant"
    )
    assert chat_server.load_history(10)[-1]["content"] == "美股调度器正在运行，状态检查已完成。"


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
