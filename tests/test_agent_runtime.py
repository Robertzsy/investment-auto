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
        lambda **kwargs: TestModel(call_tools=["run_skill"]),
    )
    monkeypatch.setattr(
        "src.manager.skill_runtime.SkillRuntime.run",
        lambda self, *args, **kwargs: {
            "status": "completed",
            "skill": "security-analysis",
            "session_scope": "investment_research",
            "execution_id": "exec-1",
            "user_report": "verified price 123",
            "validation": {"passed": True},
            "error": "",
        },
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
        "name": "run_skill",
        "params": {"request": "a"},
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
            name="run_skill",
            json_args=json.dumps({
                "request": "分析 AAPL",
                "skill_name": "security-analysis",
                "symbols": "AAPL",
            }),
            tool_call_id=f"call-{requests}",
        )}

    calls = []
    monkeypatch.setattr(
        agent_runtime, "resolve_agent_model",
        lambda **kwargs: FunctionModel(stream_function=stream_function),
    )
    monkeypatch.setattr(
        "src.manager.skill_runtime.SkillRuntime.run",
        lambda self, *args, **kwargs: calls.append((args, kwargs)) or {
            "status": "incomplete",
            "skill": "security-analysis",
            "session_scope": "investment_research",
            "execution_id": "exec-1",
            "user_report": "",
            "validation": {"passed": False},
            "error": "gap",
        },
    )

    class RecoveryAgent:
        async def run(self, prompt, **kwargs):
            return SimpleNamespace(output="已停止重复 Skill 调用。")

    monkeypatch.setattr(agent_runtime, "LOOP_RECOVERY_AGENT", RecoveryAgent())

    events = list(agent_runtime.run_agent_events(
        "重复工具测试", history=[], memory="", thinking=False,
        provider=None, model=None, cancel_event=threading.Event(),
    ))

    assert len(calls) == 1
    assert events[-1] == {"type": "result", "content": "已停止重复 Skill 调用。"}


def test_runtime_recovers_tool_loop_with_no_tool_final_answer(monkeypatch):
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    requests = 0

    async def stream_function(messages, agent_info):
        nonlocal requests
        requests += 1
        yield {0: DeltaToolCall(
            name="run_skill",
            json_args=json.dumps({"request": "分析 AAPL", "skill_name": "security-analysis", "symbols": "AAPL"}),
            tool_call_id=f"call-{requests}",
        )}

    recovery_prompts = []

    class RecoveryAgent:
        async def run(self, prompt, **kwargs):
            recovery_prompts.append(prompt)
            return SimpleNamespace(output="已根据现有证据停止重复检查，并给出最终结论。")

    monkeypatch.setattr(
        agent_runtime, "resolve_agent_model",
        lambda **kwargs: FunctionModel(stream_function=stream_function),
    )
    monkeypatch.setattr(agent_runtime, "LOOP_RECOVERY_AGENT", RecoveryAgent())
    monkeypatch.setattr(
        "src.manager.skill_runtime.SkillRuntime.run",
        lambda self, *args, **kwargs: {
            "status": "incomplete",
            "skill": "security-analysis",
            "session_scope": "investment_research",
            "execution_id": "exec-1",
            "user_report": "",
            "validation": {"passed": False},
            "error": "gap",
        },
    )

    events = list(agent_runtime.run_agent_events(
        "重复工具后必须收尾", history=[], memory="", thinking=False,
        provider=None, model=None, cancel_event=threading.Event(),
    ))

    assert events[-1] == {
        "type": "result",
        "content": "已根据现有证据停止重复检查，并给出最终结论。",
    }
    assert len(recovery_prompts) == 1
    assert "循环保护原因" in recovery_prompts[0]
    assert "完全相同" in recovery_prompts[0]


def test_window_model_can_configure_key_while_tool_event_is_redacted(monkeypatch):
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    secret = "sk-" + "window-secret-1234567890"
    requests = 0

    async def stream_function(messages, agent_info):
        nonlocal requests
        requests += 1
        if requests == 1:
            yield {0: DeltaToolCall(
                name="handoff_session",
                json_args=json.dumps({
                    "session": "system_admin",
                    "request": f"配置 DeepSeek API Key: {secret}",
                }),
                tool_call_id="configure-1",
            )}
        else:
            yield "configured"

    saved = {}
    monkeypatch.setattr(
        agent_runtime, "resolve_agent_model",
        lambda **kwargs: FunctionModel(stream_function=stream_function),
    )
    class AdminAgent:
        async def run(self, prompt, **kwargs):
            saved["DEEPSEEK_API_KEY"] = secret
            return SimpleNamespace(output="configured")

    monkeypatch.setattr(agent_runtime, "SYSTEM_ADMIN_AGENT", AdminAgent())

    events = list(agent_runtime.run_agent_events(
        "配置密钥", history=[], memory="", thinking=False,
        provider=None, model=None, cancel_event=threading.Event(),
    ))

    assert saved == {"DEEPSEEK_API_KEY": secret}
    tool_event = next(event for event in events if event["type"] == "tool")
    assert tool_event["name"] == "handoff_session"
    assert secret not in tool_event["params"]["request"]
    assert "********" in tool_event["params"]["request"]
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
                name="handoff_session",
                json_args=json.dumps({
                    "session": "system_admin",
                    "request": f"添加 DeepInfra，API Key: {secret}",
                }),
                tool_call_id="provider-1",
            )}
        else:
            yield "DeepInfra 已添加。"

    saved = {}
    monkeypatch.setattr(
        agent_runtime, "resolve_agent_model",
        lambda **kwargs: FunctionModel(stream_function=stream_function),
    )
    class AdminAgent:
        async def run(self, prompt, **kwargs):
            saved["DEEPINFRA_API_KEY"] = secret
            return SimpleNamespace(output="DeepInfra 已添加。")

    monkeypatch.setattr(agent_runtime, "SYSTEM_ADMIN_AGENT", AdminAgent())

    events = list(agent_runtime.run_agent_events(
        "添加 DeepInfra 供应商", history=[], memory="", thinking=False,
        provider=None, model=None, cancel_event=threading.Event(),
    ))

    assert requests == 2
    assert saved == {"DEEPINFRA_API_KEY": secret}
    tool_event = next(event for event in events if event["type"] == "tool")
    assert tool_event["name"] == "handoff_session"
    assert secret not in tool_event["params"]["request"]
    assert secret not in repr(events)
    assert events[-1]["type"] == "result"
    assert "DeepInfra 已添加" in events[-1]["content"]


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


def test_loop_detector_warns_then_stops_read_only_stagnation():
    detector = agent_runtime._ToolLoopDetector()
    warning = ""
    for index in range(8):
        call_id = f"call-{index}"
        detector.record_call(call_id, "list_skills", {"query": str(index)})
        warning = detector.record_result(
            call_id, "list_skills", {"count": 1, "index": index}
        )
    assert "下一步必须" in warning

    for index in range(8, 11):
        call_id = f"call-{index}"
        detector.record_call(call_id, "list_skills", {"query": str(index)})
        assert detector.record_result(
            call_id, "list_skills", {"count": 1, "index": index}
        ) == ""
    detector.record_call("call-11", "list_skills", {"query": "11"})
    with pytest.raises(agent_runtime.RepeatedToolLoop, match="连续只读"):
        detector.record_result("call-11", "list_skills", {"count": 1, "index": 11})
    assert "list_skills" in detector.recovery_context()


def test_loop_detector_mutation_resets_read_only_streak():
    detector = agent_runtime._ToolLoopDetector()
    for index in range(8):
        call_id = f"read-{index}"
        detector.record_call(call_id, "list_skills", {"query": str(index)})
        detector.record_result(call_id, "list_skills", {"index": index})

    detector.record_call("write-1", "run_skill", {"request": "execute"})
    assert detector.record_result(
        "write-1", "run_skill", {"status": "completed"}
    ) == ""
    detector.record_call("read-after-write", "list_skills", {"query": "after"})
    assert detector.record_result(
        "read-after-write", "list_skills", {"count": 1}
    ) == ""


def test_system_admin_guard_forces_diagnosis_after_three_inspections():
    detector = agent_runtime._ToolLoopDetector()
    warning = ""
    for index in range(3):
        call_id = f"admin-read-{index}"
        detector.record_call(
            call_id, "search_project", {"query": str(index)}, scope="system_admin"
        )
        warning = detector.record_result(
            call_id, "search_project", {"count": index}, scope="system_admin"
        )
    assert "repair_incident" in warning
    detector.record_call(
        "admin-read-3", "inspect_investment_agent_code", {"path": "src/main.py"},
        scope="system_admin",
    )
    with pytest.raises(agent_runtime.RepeatedToolLoop, match="结构化诊断"):
        detector.record_result(
            "admin-read-3", "inspect_investment_agent_code", {"content": "x"},
            scope="system_admin",
        )


def test_system_admin_guard_rejects_second_code_mutation():
    detector = agent_runtime._ToolLoopDetector()
    detector.record_call(
        "mutation-1", "modify_investment_agent_code", {"path": "src/a.py"},
        scope="system_admin",
    )
    detector.record_result(
        "mutation-1", "modify_investment_agent_code", {"status": "ok"},
        scope="system_admin",
    )
    with pytest.raises(agent_runtime.RepeatedToolLoop, match="一个代码变更"):
        detector.record_call(
            "mutation-2", "repair_incident", {"path": "src/b.py", "new_content": "x"},
            scope="system_admin",
        )


def test_nested_system_admin_tool_events_are_forwarded_to_desktop_queue():
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    async def stream():
        yield FunctionToolCallEvent(
            ToolCallPart(tool_name="repair_incident", args={"execution_id": "abc"}, tool_call_id="1")
        )
        yield FunctionToolResultEvent(
            ToolReturnPart(tool_name="repair_incident", content={"status": "diagnosed"}, tool_call_id="1")
        )

    updates = queue.Queue()
    deps = agent_runtime.ChatAgentDeps(
        cancellation_token=agent_runtime.CancellationToken(), event_queue=updates
    )
    asyncio.run(agent_runtime._forward_tool_events(deps, stream(), session="system_admin"))
    assert updates.get_nowait() == {
        "type": "tool",
        "name": "repair_incident",
        "params": {"execution_id": "abc"},
        "session": "system_admin",
    }
    assert deps.completed_tool_calls == ["repair_incident"]


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
        chat_server.append_memory("API_KEY=" + "sk-" + "very-secret-value")


def test_manager_can_configure_api_key_without_echo(monkeypatch):
    saved = {}
    monkeypatch.setattr(agent_runtime.cfg, "llm_model_config", lambda provider: {
        "provider_name": "DeepSeek", "api_key_env": "DEEPSEEK_API_KEY",
    } if provider == "deepseek" else {})
    monkeypatch.setattr("src.secret_store.save_secret", lambda name, value: saved.update({name: value}))
    monkeypatch.delenv("DEEPSEEK_API_KEY", raising=False)
    ctx = SimpleNamespace(deps=SimpleNamespace(authoritative_report=""))
    secret = "sk-" + "test-secret-never-echo"

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


def test_openai_compatible_provider_classifies_402_as_external_billing(monkeypatch):
    class BillingLLM:
        def chat(self, *args, **kwargs):
            raise RuntimeError(
                "Error code: 402 - You need positive balance to do inference; please setup top-up"
            )

    monkeypatch.setattr("src.secret_store.load_secret", lambda name: None)
    monkeypatch.setattr("src.secret_store.save_secret", lambda name, value: None)
    monkeypatch.setattr("src.ui.server._write_config_merged", lambda patch: patch)
    monkeypatch.setattr("src.llm.registry.resolve_llm", lambda **kwargs: BillingLLM())
    ctx = SimpleNamespace(deps=SimpleNamespace(authoritative_report=""))

    result = asyncio.run(agent_runtime.configure_openai_compatible_provider(
        ctx,
        provider="deepinfra",
        api_base="https://api.deepinfra.com/v1/openai",
        api_key="di-test-secret-never-echo",
        model="deepseek-ai/DeepSeek-V4-Flash-0731",
        display_name="DeepInfra",
    ))

    assert result["connection_test"]["category"] == "billing"
    assert result["connection_test"]["requires_user_action"] is True
    assert "本地改代码或新增工具无法修复" in result["user_report"]


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
    assert "run_skill" in instructions
    assert "完成契约" in instructions
    assert "handoff_session" in instructions
    assert "schedule_skill" in instructions
    assert "上一轮失败的操作只能在用户明确要求继续或重试时恢复" in instructions
    assert "内部 Actions 不是给用户或顶层模型选择的工具" in instructions
