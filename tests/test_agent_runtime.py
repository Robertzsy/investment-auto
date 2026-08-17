from __future__ import annotations

import asyncio
import json
import queue
import threading
from types import SimpleNamespace

import pytest
from pydantic_ai.models.test import TestModel

from src.ui import agent_runtime, chat_server


@pytest.fixture(autouse=True)
def isolated_investment_command_audit(monkeypatch, tmp_path):
    from src.investment import service

    monkeypatch.setattr(service, "COMMAND_DIR", tmp_path / "commands")


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


def test_runtime_stops_duplicate_tool_before_second_side_effect(monkeypatch):
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    requests = 0

    async def stream_function(messages, agent_info):
        nonlocal requests
        requests += 1
        yield {0: DeltaToolCall(
            name="get_security_snapshot",
            json_args=json.dumps({"code": "AAPL"}),
            tool_call_id=f"call-{requests}",
        )}

    calls = []
    monkeypatch.setattr(
        agent_runtime, "resolve_agent_model",
        lambda **kwargs: FunctionModel(stream_function=stream_function),
    )
    monkeypatch.setattr(
        "src.platform.market_tools.stock_fetcher",
        lambda command, value: calls.append((command, value)) or {"price": 100},
    )

    events = list(agent_runtime.run_agent_events(
        "重复工具测试", history=[], memory="", thinking=False,
        provider=None, model=None, cancel_event=threading.Event(),
    ))

    assert calls == [("snapshot", "AAPL")]
    assert events[-1]["type"] == "error"
    assert "完全相同" in events[-1]["content"]


def test_window_model_can_configure_key_while_tool_event_is_redacted(monkeypatch):
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    secret = "sk-window-secret-1234567890"
    requests = 0

    async def stream_function(messages, agent_info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: DeltaToolCall(
                name="configure_llm_api_key",
                json_args=json.dumps({"provider": "deepseek", "api_key": secret}),
                tool_call_id="configure-1",
            )}
        else:
            yield "configured"

    saved = {}
    monkeypatch.setattr(
        agent_runtime, "resolve_agent_model",
        lambda **kwargs: FunctionModel(stream_function=stream_function),
    )
    monkeypatch.setattr(agent_runtime.cfg, "llm_model_config", lambda provider: {
        "provider_name": "DeepSeek", "api_key_env": "DEEPSEEK_API_KEY",
    })
    monkeypatch.setattr("src.secret_store.save_secret", lambda name, value: saved.update({name: value}))

    events = list(agent_runtime.run_agent_events(
        "配置密钥", history=[], memory="", thinking=False,
        provider=None, model=None, cancel_event=threading.Event(),
    ))

    assert saved == {"DEEPSEEK_API_KEY": secret}
    tool_event = next(event for event in events if event["type"] == "tool")
    assert tool_event["params"]["api_key"] == "********"
    assert secret not in repr(events)
    assert events[-1]["type"] == "result"


def test_loop_detector_stops_identical_call_before_second_execution():
    detector = agent_runtime._ToolLoopDetector()
    detector.record_call("call-1", "get_security_snapshot", {"code": "AAPL"})
    detector.record_result("call-1", "get_security_snapshot", {"price": 100})

    with pytest.raises(agent_runtime.RepeatedToolLoop, match="重复执行前"):
        detector.record_call("call-2", "get_security_snapshot", {"code": "AAPL"})


def test_loop_detector_allows_same_tool_when_arguments_or_result_progress():
    detector = agent_runtime._ToolLoopDetector()
    detector.record_call("call-1", "get_security_snapshot", {"code": "AAPL"})
    detector.record_result("call-1", "get_security_snapshot", {"price": 100})
    detector.record_call("call-2", "get_security_snapshot", {"code": "MSFT"})
    detector.record_result("call-2", "get_security_snapshot", {"price": 100})


def test_loop_detector_stops_read_only_stagnation():
    detector = agent_runtime._ToolLoopDetector()
    for index in range(9):
        call_id = f"call-{index}"
        detector.record_call(call_id, "search_project", {"query": str(index)})
        detector.record_result(call_id, "search_project", {"count": 1, "index": index})
    detector.record_call("call-9", "search_project", {"query": "9"})
    with pytest.raises(agent_runtime.RepeatedToolLoop, match="连续只读"):
        detector.record_result("call-9", "search_project", {"count": 1, "index": 9})


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


def test_manager_can_configure_api_key_without_echo(monkeypatch):
    saved = {}
    monkeypatch.setattr(agent_runtime.cfg, "llm_model_config", lambda provider: {
        "provider_name": "DeepSeek", "api_key_env": "DEEPSEEK_API_KEY",
    } if provider == "deepseek" else {})
    monkeypatch.setattr("src.secret_store.save_secret", lambda name, value: saved.update({name: value}))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    ctx = SimpleNamespace(deps=SimpleNamespace(authoritative_report=""))
    secret = "sk-test-secret-never-echo"

    result = asyncio.run(agent_runtime.configure_llm_api_key(ctx, "deepseek", secret))

    assert saved == {"DEEPSEEK_API_KEY": secret}
    assert result["configured"] is True
    assert result["masked"] == "********"
    assert secret not in repr(result)
    assert secret not in ctx.deps.authoritative_report


def test_manager_system_prompt_defines_semantic_cycle_and_memory_tools():
    instructions = "\n".join(
        value for value in agent_runtime.MANAGER_AGENT._instructions if isinstance(value, str)
    )
    assert "不要依赖固定口令" in instructions
    assert "run_complete_investment_cycle" in instructions
    assert "remember_user_preference" in instructions
    assert "configure_llm_api_key" in instructions
    assert "不得要求用户逐步确认" in instructions
