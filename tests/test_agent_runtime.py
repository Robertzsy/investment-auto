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


def test_window_model_can_add_provider_in_one_tool_call_with_redacted_event(monkeypatch):
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    secret = "di-window-secret-1234567890"
    requests = 0

    async def stream_function(messages, agent_info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: DeltaToolCall(
                name="configure_openai_compatible_provider",
                json_args=json.dumps({
                    "provider": "deepinfra",
                    "api_base": "https://api.deepinfra.com/v1/openai",
                    "api_key": secret,
                    "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
                    "display_name": "DeepInfra",
                    "test_connection": False,
                }),
                tool_call_id="provider-1",
            )}
        else:
            yield "DeepInfra 已添加。"

    saved = {}
    patches = []
    monkeypatch.setattr(
        agent_runtime, "resolve_agent_model",
        lambda **kwargs: FunctionModel(stream_function=stream_function),
    )
    monkeypatch.setattr("src.secret_store.load_secret", lambda name: None)
    monkeypatch.setattr("src.secret_store.save_secret", lambda name, value: saved.update({name: value}))
    monkeypatch.setattr("src.ui.server._write_config_merged", lambda patch: patches.append(patch) or patch)

    events = list(agent_runtime.run_agent_events(
        "添加 DeepInfra 供应商", history=[], memory="", thinking=False,
        provider=None, model=None, cancel_event=threading.Event(),
    ))

    assert requests == 2
    assert saved == {"DEEPINFRA_API_KEY": secret}
    assert len(patches) == 1
    tool_event = next(event for event in events if event["type"] == "tool")
    assert tool_event["name"] == "configure_openai_compatible_provider"
    assert tool_event["params"]["api_key"] == "********"
    assert secret not in repr(events)
    assert events[-1]["type"] == "result"
    assert "DeepInfra（deepinfra）已加入模型供应商列表" in events[-1]["content"]


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


def test_manager_can_add_openai_compatible_provider_atomically(monkeypatch):
    saved = {}
    patches = []
    secret = "di-test-secret-never-echo"
    monkeypatch.setattr("src.secret_store.load_secret", lambda name: None)
    monkeypatch.setattr("src.secret_store.save_secret", lambda name, value: saved.update({name: value}))
    monkeypatch.setattr("src.ui.server._write_config_merged", lambda patch: patches.append(patch) or patch)
    monkeypatch.delenv("DEEPINFRA_API_KEY", raising=False)
    ctx = SimpleNamespace(deps=SimpleNamespace(authoritative_report=""))

    result = asyncio.run(agent_runtime.configure_openai_compatible_provider(
        ctx,
        provider="deepinfra",
        api_base="https://api.deepinfra.com/v1/openai/",
        api_key=secret,
        model="deepseek-ai/DeepSeek-V4-Flash-0731",
        variants=["deepseek-ai/DeepSeek-V4-Flash-0731"],
        display_name="DeepInfra",
        test_connection=False,
    ))

    assert saved == {"DEEPINFRA_API_KEY": secret}
    assert patches == [{"llm": {"models": {"deepinfra": {
        "provider_name": "DeepInfra",
        "api_base": "https://api.deepinfra.com/v1/openai",
        "api_key_env": "DEEPINFRA_API_KEY",
        "model": "deepseek-ai/DeepSeek-V4-Flash-0731",
        "variants": ["deepseek-ai/DeepSeek-V4-Flash-0731"],
    }}}}]
    assert result["configured"] is True
    assert result["connection_test"] == {"status": "skipped"}
    assert result["masked"] == "********"
    assert secret not in repr(result)
    assert secret not in repr(patches)
    assert secret not in ctx.deps.authoritative_report


def test_new_provider_config_rolls_back_secret_if_config_write_fails(monkeypatch):
    saved = []
    monkeypatch.setattr("src.secret_store.load_secret", lambda name: "old-secret-value")
    monkeypatch.setattr("src.secret_store.save_secret", lambda name, value: saved.append((name, value)))
    monkeypatch.setattr(
        "src.ui.server._write_config_merged",
        lambda patch: (_ for _ in ()).throw(OSError("disk full")),
    )
    ctx = SimpleNamespace(deps=SimpleNamespace(authoritative_report=""))

    with pytest.raises(OSError, match="disk full"):
        asyncio.run(agent_runtime.configure_openai_compatible_provider(
            ctx,
            provider="deepinfra",
            api_base="https://api.deepinfra.com/v1/openai",
            api_key="new-secret-value",
            model="deepseek-ai/DeepSeek-V4-Flash-0731",
            test_connection=False,
        ))

    assert saved == [
        ("DEEPINFRA_API_KEY", "new-secret-value"),
        ("DEEPINFRA_API_KEY", "old-secret-value"),
    ]


def test_conversation_prompt_does_not_implicitly_resume_failed_task():
    prompt = agent_runtime._conversation_prompt(
        "说话",
        [{"role": "assistant", "content": "Agent 运行失败：连续只读工具调用"}],
        "",
    )

    assert "本轮唯一任务" in prompt
    assert "不得恢复上轮失败操作" in prompt
    assert prompt.endswith("说话")


def test_manager_system_prompt_defines_semantic_cycle_and_memory_tools():
    instructions = "\n".join(
        value for value in agent_runtime.MANAGER_AGENT._instructions if isinstance(value, str)
    )
    assert "不要依赖固定口令" in instructions
    assert "run_complete_investment_cycle" in instructions
    assert "remember_user_preference" in instructions
    assert "configure_llm_api_key" in instructions
    assert "configure_openai_compatible_provider" in instructions
    assert "上一轮失败的操作只能在用户明确要求继续或重试时恢复" in instructions
    assert "不得要求用户逐步确认" in instructions
