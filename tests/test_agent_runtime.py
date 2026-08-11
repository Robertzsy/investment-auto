from __future__ import annotations

import threading

import pytest
from pydantic_ai.models.test import TestModel

from src.ui import agent_runtime, chat_server


def test_native_function_call_emits_tool_event_and_final_result(monkeypatch):
    monkeypatch.setattr(
        agent_runtime,
        "resolve_agent_model",
        lambda **kwargs: TestModel(call_tools=["get_security_snapshot"]),
    )
    monkeypatch.setattr(
        chat_server,
        "_stock_fetcher",
        lambda command, value: {"command": command, "code": value, "price": 123.0},
    )

    events = list(
        agent_runtime.run_agent_events(
            "查看测试证券",
            history=[],
            memory="",
            thinking=False,
            provider=None,
            model=None,
            cancel_event=threading.Event(),
        )
    )

    assert events[0] == {
        "type": "tool",
        "name": "get_security_snapshot",
        "params": {"code": "a"},
    }
    assert events[-1]["type"] == "result"
    assert "123" in events[-1]["content"]


def test_loop_detector_stops_second_identical_completed_call():
    detector = agent_runtime._ToolLoopDetector()
    detector.record_call("call-1", "get_security_snapshot", {"code": "AAPL"})
    detector.record_result("call-1", "get_security_snapshot", {"price": 100})
    detector.record_call("call-2", "get_security_snapshot", {"code": "AAPL"})

    with pytest.raises(agent_runtime.RepeatedToolLoop, match="结果没有变化"):
        detector.record_result("call-2", "get_security_snapshot", {"price": 100})


def test_loop_detector_allows_same_tool_when_arguments_or_result_progress():
    detector = agent_runtime._ToolLoopDetector()
    detector.record_call("call-1", "get_security_snapshot", {"code": "AAPL"})
    detector.record_result("call-1", "get_security_snapshot", {"price": 100})
    detector.record_call("call-2", "get_security_snapshot", {"code": "MSFT"})
    detector.record_result("call-2", "get_security_snapshot", {"price": 100})
    detector.record_call("call-3", "get_security_snapshot", {"code": "AAPL"})
    detector.record_result("call-3", "get_security_snapshot", {"price": 101})
