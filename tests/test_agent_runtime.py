from __future__ import annotations

import asyncio
import queue
import threading
from types import SimpleNamespace

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
        "src.platform.market_tools.stock_fetcher",
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


def test_complete_investment_cycle_is_a_native_agent_tool(monkeypatch):
    monkeypatch.setenv("INVESTMENT_AGENT_TRANSPORT", "local")
    updates = queue.Queue()
    monkeypatch.setattr("src.scheduler.run_investment_cycle", lambda market, **kwargs: (
        kwargs["progress_callback"]("正在分析") or {
            "status": "generated",
            "market": market,
            "report": "",
            "autonomous": {"status": "no_trade", "fills": []},
            "notification": {"status": "disabled"},
        }
    ))
    ctx = SimpleNamespace(deps=SimpleNamespace(event_queue=updates, authoritative_report=""))

    result = asyncio.run(agent_runtime.run_complete_investment_cycle(ctx, "us"))

    assert result["market"] == "us"
    assert result["autonomous_status"] == "no_trade"
    assert "完整投资轮次" in result["user_report"]
    assert ctx.deps.authoritative_report == result["user_report"]
    assert updates.get_nowait() == {"type": "status", "content": "正在分析"}


def test_memory_is_dynamic_system_context_and_rejects_secrets(monkeypatch, tmp_path):
    monkeypatch.setattr(chat_server, "MEMORY_FILE", tmp_path / "chat_memory.md")
    stored = chat_server.append_memory("默认优先分析美股", source="agent")

    assert stored["status"] == "stored"
    assert "默认优先分析美股" in agent_runtime._memory_instructions(chat_server.load_memory())
    with pytest.raises(ValueError, match="不能保存"):
        chat_server.append_memory("API_KEY=sk-very-secret-value")


def test_manager_system_prompt_defines_semantic_cycle_and_memory_tools():
    instructions = "\n".join(
        value for value in agent_runtime.MANAGER_AGENT._instructions if isinstance(value, str)
    )
    assert "不要依赖固定口令" in instructions
    assert "run_complete_investment_cycle" in instructions
    assert "remember_user_preference" in instructions
    assert "不得要求用户逐步确认" in instructions
