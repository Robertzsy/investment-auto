from __future__ import annotations

import json

import pytest

from src.manager import tool_factory
from src.manager.capabilities import CapabilityRegistry, CAPABILITY_DIR

SAMPLE_CODE = '''"""demo tool for tests"""
from typing import Dict


def run(symbol: str = "") -> Dict:
    return {"symbol": symbol.upper(), "count": 1}
'''

SAMPLE_SCHEMA = {
    "type": "object",
    "properties": {"symbol": {"type": "string"}},
    "required": ["symbol"],
}


def _isolate(monkeypatch, tmp_path):
    monkeypatch.setattr(tool_factory, "TOOL_MODULE_DIR", tmp_path / "tools")
    monkeypatch.setattr("src.manager.capabilities.CAPABILITY_DIR", tmp_path / "capabilities")
    (tmp_path / "tools").mkdir(parents=True, exist_ok=True)
    (tmp_path / "tools" / "__init__.py").write_text("", encoding="utf-8")
    return CapabilityRegistry(tmp_path / "capabilities")


def test_create_tool_full_pipeline_succeeds(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    result = tool_factory.create_manager_tool(
        name="demo_tool",
        description="演示工具",
        code=SAMPLE_CODE,
        parameters_schema=SAMPLE_SCHEMA,
        test_args={"symbol": "sh600519"},
    )

    assert result["status"] == "created"
    assert result["registered"] is True
    assert result["trial_call"]["result"]["symbol"] == "SH600519"
    assert (tmp_path / "tools" / "demo_tool.py").is_file()
    manifest = registry.catalog()["tools"]
    assert [item["name"] for item in manifest] == ["demo_tool"]


def test_create_tool_rolls_back_on_broken_code(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    result = tool_factory.create_manager_tool(
        name="broken_tool",
        description="坏代码",
        code="x = 1\ndef run(:\n",  # valid pre-flight, broken Python on import
        parameters_schema=SAMPLE_SCHEMA,
    )

    assert result["status"] == "rolled_back"
    assert "write_module" in result["steps"] or "tests" in result["steps"] or "import_verify" in result["steps"]
    assert not (tmp_path / "tools" / "broken_tool.py").exists()
    assert registry.catalog()["tools"] == []


def test_create_tool_rejects_code_without_function(monkeypatch, tmp_path):
    # The entry-function check is pre-flight validation: nothing is written,
    # so there is nothing to roll back - a clean ValueError is correct.
    registry = _isolate(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="def run"):
        tool_factory.create_manager_tool(
            name="no_fn_tool",
            description="缺少函数",
            code='"""no run function"""\nanswer = 42\n',
            parameters_schema=SAMPLE_SCHEMA,
        )
    assert not (tmp_path / "tools" / "no_fn_tool.py").exists()
    assert registry.catalog()["tools"] == []


def test_create_tool_rolls_back_when_tests_fail(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    result = tool_factory.create_manager_tool(
        name="fail_test_tool",
        description="测试失败",
        code=SAMPLE_CODE,
        parameters_schema=SAMPLE_SCHEMA,
        tests=["python -m pytest -q tests/does_not_exist.py"],
    )

    assert result["status"] == "rolled_back"
    assert result["stage"] == "tests"
    assert not (tmp_path / "tools" / "fail_test_tool.py").exists()
    assert registry.catalog()["tools"] == []


def test_create_tool_rejects_invalid_schema_before_writing(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="schema"):
        tool_factory.create_manager_tool(
            name="bad_schema_tool",
            description="坏 schema",
            code=SAMPLE_CODE,
            parameters_schema={"type": "string"},
        )
    assert not (tmp_path / "tools").exists() or not (tmp_path / "tools" / "bad_schema_tool.py").exists()
    assert registry.catalog()["tools"] == []


def test_create_tool_rejects_reserved_and_existing_names(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    with pytest.raises(ValueError, match="内置工具"):
        tool_factory.create_manager_tool(
            name="search_project",
            description="冲突",
            code=SAMPLE_CODE,
            parameters_schema=SAMPLE_SCHEMA,
            reserved_names={"search_project"},
        )
    tool_factory.create_manager_tool(
        name="first_tool", description="先注册", code=SAMPLE_CODE, parameters_schema=SAMPLE_SCHEMA,
    )
    with pytest.raises(ValueError, match="已注册"):
        tool_factory.create_manager_tool(
            name="first_tool", description="重复", code=SAMPLE_CODE, parameters_schema=SAMPLE_SCHEMA,
        )


def test_create_tool_verifies_required_signature_parameters(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    result = tool_factory.create_manager_tool(
        name="sig_mismatch",
        description="签名不符",
        code=SAMPLE_CODE,
        parameters_schema={
            "type": "object",
            "properties": {"missing_arg": {"type": "string"}},
            "required": ["missing_arg"],
        },
    )

    assert result["status"] == "rolled_back"
    assert result["stage"] == "import_verify"
    assert not (tmp_path / "tools" / "sig_mismatch.py").exists()
    assert registry.catalog()["tools"] == []


def test_uninstall_removes_source_and_allows_recreation(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    tool_factory.create_manager_tool(
        name="temp_tool", description="临时", code=SAMPLE_CODE,
        parameters_schema=SAMPLE_SCHEMA, test_args={"symbol": "a"},
    )
    removed = tool_factory.uninstall_manager_tool_complete("temp_tool")
    assert removed["status"] == "uninstalled"
    assert removed["source_removed"] is True
    assert not (tmp_path / "tools" / "temp_tool.py").exists()
    assert registry.catalog()["tools"] == []
    # The same name can now be created again.
    again = tool_factory.create_manager_tool(
        name="temp_tool", description="重建", code=SAMPLE_CODE,
        parameters_schema=SAMPLE_SCHEMA, test_args={"symbol": "b"},
    )
    assert again["status"] == "created"
    assert tool_factory.uninstall_manager_tool_complete("temp_tool")["status"] == "uninstalled"
    assert tool_factory.uninstall_manager_tool_complete("temp_tool")["status"] == "not_found"


def test_chat_regression_catalog_updated_for_new_tools():
    # The manager toolset grew by create_manager_tool / uninstall_manager_tool;
    # both are management-plane tools, still no shell or execution.
    from src.ui import agent_runtime

    tools = set(agent_runtime.MANAGER_AGENT._function_toolset.tools)
    assert {"create_manager_tool", "uninstall_manager_tool"} <= tools
    assert not ({"run_shell", "write_file", "execute_orders"} & tools)



def test_create_tool_fulfills_current_request_in_one_turn(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    result = tool_factory.create_manager_tool(
        name="fulfill_tool",
        description="单轮闭环",
        code=SAMPLE_CODE,
        parameters_schema=SAMPLE_SCHEMA,
        fulfill_args={"symbol": "hk00700"},
    )

    assert result["status"] == "created"
    # The user's current request is answered inside this same call:
    # no second message is needed before the result is usable.
    assert result["fulfill_result"]["result"]["symbol"] == "HK00700"
    assert result["fulfill_result"]["result"]["count"] == 1
    # Empty test_args still runs a real trial (SAMPLE_CODE defaults symbol).
    assert result["trial_call"]["result"]["count"] == 1


def test_create_tool_fulfill_error_is_captured_not_fatal(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    result = tool_factory.create_manager_tool(
        name="fulfill_err_tool",
        description="调用参数错误",
        code=SAMPLE_CODE,
        parameters_schema=SAMPLE_SCHEMA,
        test_args={"symbol": "ok"},  # a passing trial
        fulfill_args={"missing": "nope"},  # bad fulfill keeps the tool, flagged
    )

    assert result["status"] == "created_fulfill_failed"  # creation stands, clearly flagged
    assert "error" in result["fulfill_result"]
    # Without a separate valid trial, bad fulfill args would have been
    # the trial itself and rolled the creation back (see the contract tests).
    bad_only = tool_factory.create_manager_tool(
        name="fulfill_bad_only",
        description="只有坏 fulfill",
        code=SAMPLE_CODE,
        parameters_schema=SAMPLE_SCHEMA,
        fulfill_args={"missing": "nope"},
    )
    assert bad_only["status"] == "rolled_back"
    assert bad_only["stage"] == "trial_call"



def test_create_tool_rejects_forbidden_imports(monkeypatch, tmp_path):
    # The manager plane has no trading/account-write/shell power, and a
    # fulfill call executes the new code immediately.
    registry = _isolate(monkeypatch, tmp_path)
    malicious = """
from src.trading.broker import execute_orders


def run() -> dict:
    return {}
"""
    with pytest.raises(ValueError, match="执行相关模块"):
        tool_factory.create_manager_tool(
            name="evil_tool",
            description="越权",
            code=malicious,
            parameters_schema={"type": "object", "properties": {}},
        )
    assert not (tmp_path / "tools" / "evil_tool.py").exists()
    assert registry.catalog()["tools"] == []

    shell_code = """
import subprocess


def run() -> dict:
    return {}
"""
    with pytest.raises(ValueError, match="执行相关模块"):
        tool_factory.create_manager_tool(
            name="shell_tool",
            description="越权 shell",
            code=shell_code,
            parameters_schema={"type": "object", "properties": {}},
        )
    assert registry.catalog()["tools"] == []


def test_create_tool_def_match_is_exact(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    code = """
def runner() -> dict:
    return {}
"""
    with pytest.raises(ValueError, match="def run"):
        tool_factory.create_manager_tool(
            name="substr_tool",
            description="子串误匹配",
            code=code,
            parameters_schema={"type": "object", "properties": {}},
        )
    assert registry.catalog()["tools"] == []


def test_concurrent_creation_never_corrupts(monkeypatch, tmp_path):
    import threading

    registry = _isolate(monkeypatch, tmp_path)
    outcomes = []

    def attempt():
        try:
            result = tool_factory.create_manager_tool(
                name="race_tool",
                description="并发创建",
                code=SAMPLE_CODE,
                parameters_schema=SAMPLE_SCHEMA,
            )
            outcomes.append(result["status"])
        except ValueError:
            outcomes.append("conflict")

    threads = [threading.Thread(target=attempt) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()

    assert outcomes.count("created") == 1
    assert outcomes.count("conflict") == 3
    manifests = registry.catalog()["tools"]
    assert [item["name"] for item in manifests] == ["race_tool"]
    module = (tmp_path / "tools" / "race_tool.py").read_text(encoding="utf-8")
    assert "def run" in module  # not truncated or interleaved



def test_trial_call_failure_rolls_back(monkeypatch, tmp_path):
    # A tool that fails its own verification case must never stay registered.
    registry = _isolate(monkeypatch, tmp_path)
    bad = """
def run(symbol: str) -> dict:
    raise RuntimeError("boom")
"""
    result = tool_factory.create_manager_tool(
        name="trial_fail", description="试调用失败", code=bad,
        parameters_schema={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
        test_args={"symbol": "x"},
    )
    assert result["status"] == "rolled_back"
    assert result["stage"] == "trial_call"
    assert not (tmp_path / "tools" / "trial_fail.py").exists()
    assert registry.catalog()["tools"] == []


def test_unserializable_result_rolls_back(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    code = """
def run() -> object:
    return lambda: 1
"""
    result = tool_factory.create_manager_tool(
        name="unserial", description="不可序列化", code=code,
        parameters_schema={"type": "object", "properties": {}},
    )
    assert result["status"] == "rolled_back"
    assert result["stage"] == "trial_call"


def test_async_tool_function_rejected(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    code = """
async def run() -> dict:
    return {}
"""
    result = tool_factory.create_manager_tool(
        name="async_tool", description="异步", code=code,
        parameters_schema={"type": "object", "properties": {}},
    )
    assert result["status"] == "rolled_back"
    assert result["stage"] == "import_verify"
    assert registry.catalog()["tools"] == []


def test_mandatory_trial_call_without_args(monkeypatch, tmp_path):
    # Neither test nor fulfill args, but the function has a required
    # parameter: a clear preflight error is raised BEFORE anything is
    # written to disk.
    registry = _isolate(monkeypatch, tmp_path)
    needs_arg = """
def run(symbol: str) -> dict:
    return {}
"""
    with pytest.raises(ValueError, match="请提供 test_args_json"):
        tool_factory.create_manager_tool(
            name="needs_arg", description="缺参数", code=needs_arg,
            parameters_schema={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
        )
    assert not (tmp_path / "tools" / "needs_arg.py").exists()
    assert registry.catalog()["tools"] == []



def test_agent_level_creates_tool_and_answers_in_one_turn(monkeypatch, tmp_path):
    """Execution-chain proof, not autonomy proof: the scripted fake model
    unconditionally returns a create_manager_tool call, so this verifies the
    real MANAGER_AGENT run loop executes the tool, registers it, and
    round-trips the fulfill result in the same turn.  Whether the model
    DECIDES to create a tool for a missing capability is governed by the
    instructions and remains covered by the optional live-API eval script.
    """
    import asyncio
    import queue as _queue
    from datetime import datetime

    from pydantic_ai import CancellationToken, UsageLimits
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from pydantic_ai.models import Model

    registry = _isolate(monkeypatch, tmp_path)

    class ScriptedModel(Model):
        def __init__(self, responses):
            super().__init__()
            self._responses = list(responses)
            self.seen_messages = []

        @property
        def model_name(self) -> str:
            return "scripted-model"

        @property
        def system(self) -> str:
            return "scripted"

        @property
        def provider(self):
            return None

        async def request(self, messages, model_settings, model_request_parameters):
            self.seen_messages.append(messages)
            if not self._responses:
                raise AssertionError("scripted model exhausted")
            return self._responses.pop(0)

    tool_code = """
def run(market: str) -> dict:
    return {"market": market, "rows": [{"code": "600519", "amount": 1.2}]}
"""
    schema = {"type": "object", "properties": {"market": {"type": "string"}}, "required": ["market"]}
    create_args = {
        "name": "amount_ranking",
        "description": "查询市场成交额排名",
        "code": tool_code,
        "function_name": "run",
        "parameters_schema_json": json.dumps(schema),
        "test_args_json": json.dumps({"market": "cn"}),
        "fulfill_args_json": json.dumps({"market": "cn"}),
    }
    model = ScriptedModel([
        ModelResponse(
            parts=[ToolCallPart(tool_name="create_manager_tool", args=create_args)],
            timestamp=datetime.now(),
        ),
        ModelResponse(
            parts=[TextPart(content="已创建工具并查询：600519 排名第一")],
            timestamp=datetime.now(),
        ),
    ])

    async def scenario():
        from src.manager.capabilities import CapabilityRegistry
        from src.ui import agent_runtime

        deps = agent_runtime.ChatAgentDeps(
            cancellation_token=CancellationToken(),
            event_queue=_queue.Queue(),
        )
        reserved = set(agent_runtime.MANAGER_AGENT._function_toolset.tools)
        result = await agent_runtime.MANAGER_AGENT.run(
            "帮我查一下 A 股今天成交额排名",
            model=model,
            deps=deps,
            usage_limits=UsageLimits(request_limit=8, tool_calls_limit=16, total_tokens_limit=50000),
            toolsets=[CapabilityRegistry().toolset(reserved)],
        )
        return str(result.output)

    output = asyncio.run(scenario())

    assert "600519" in output
    # The tool really got created through the Agent call, with source+manifest.
    manifests = registry.catalog()["tools"]
    assert [item["name"] for item in manifests] == ["amount_ranking"]
    assert (tmp_path / "tools" / "amount_ranking.py").is_file()
    # The same-turn fulfill result reached the model as a tool return message.
    all_parts = [
        part for messages in model.seen_messages for message in messages for part in message.parts
    ]
    tool_return_contents = [
        getattr(part, "content", "") for part in all_parts
        if getattr(part, "part_kind", "") == "tool-return"
    ]
    assert any("600519" in str(content) for content in tool_return_contents)



def test_fulfill_only_executes_once(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    counting = """
calls = []


def run(symbol: str) -> dict:
    calls.append(symbol)
    return {"calls": len(calls), "symbol": symbol}
"""
    result = tool_factory.create_manager_tool(
        name="once_tool", description="只执行一次", code=counting,
        parameters_schema={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
        fulfill_args={"symbol": "cn"},
    )
    assert result["status"] == "created"
    assert result["fulfill_result"]["result"]["calls"] == 1
    assert result["fulfill_result"]["shared_with_trial"] is True
    assert result["trial_call"]["result"]["calls"] == 1


def test_identical_test_and_fulfill_execute_once(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    counting = """
calls = []


def run(symbol: str) -> dict:
    calls.append(symbol)
    return {"calls": len(calls)}
"""
    result = tool_factory.create_manager_tool(
        name="same_args", description="相同参数", code=counting,
        parameters_schema={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
        test_args={"symbol": "cn"}, fulfill_args={"symbol": "cn"},
    )
    assert result["fulfill_result"]["shared_with_trial"] is True
    assert result["fulfill_result"]["result"]["calls"] == 1


def test_distinct_test_and_fulfill_execute_twice(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    counting = """
calls = []


def run(symbol: str) -> dict:
    calls.append(symbol)
    return {"calls": len(calls), "symbol": symbol}
"""
    result = tool_factory.create_manager_tool(
        name="two_calls", description="不同参数", code=counting,
        parameters_schema={"type": "object", "properties": {"symbol": {"type": "string"}}, "required": ["symbol"]},
        test_args={"symbol": "verify"}, fulfill_args={"symbol": "cn"},
    )
    assert result["status"] == "created"
    assert "shared_with_trial" not in result["fulfill_result"]
    assert result["trial_call"]["result"]["symbol"] == "verify"
    assert result["fulfill_result"]["result"]["symbol"] == "cn"
    assert result["fulfill_result"]["result"]["calls"] == 2


def test_top_level_def_is_accepted(monkeypatch, tmp_path):
    # A module whose FIRST line is the def must not be misjudged.
    registry = _isolate(monkeypatch, tmp_path)
    code = """def run() -> dict:
    return {"ok": True}
"""
    result = tool_factory.create_manager_tool(
        name="top_def", description="顶格 def", code=code,
        parameters_schema={"type": "object", "properties": {}},
    )
    assert result["status"] == "created"
    assert result["trial_call"]["result"]["ok"] is True


def test_stale_file_lock_is_reaped(monkeypatch, tmp_path):
    import time as _time

    monkeypatch.setattr(tool_factory, "ROOT", tmp_path)
    lock_dir = tmp_path / "runtime" / "manager" / "locks"
    lock_dir.mkdir(parents=True)
    lock_path = lock_dir / "old.lock"
    lock_path.write_text("99999:deadbeef", encoding="ascii")
    stale = _time.time() - 3600
    import os as _os
    _os.utime(lock_path, (stale, stale))

    with tool_factory._file_lock("old"):
        assert lock_path.exists()  # reaped and re-acquired
    assert not lock_path.exists()  # owner released it


def test_create_uninstall_race_keeps_invariant(monkeypatch, tmp_path):
    import threading

    registry = _isolate(monkeypatch, tmp_path)
    stop = threading.Event()
    errors = []

    def creator():
        while not stop.is_set():
            try:
                tool_factory.create_manager_tool(
                    name="race_io", description="竞争", code=SAMPLE_CODE,
                    parameters_schema=SAMPLE_SCHEMA, test_args={"symbol": "x"},
                )
            except Exception as exc:
                errors.append(str(exc)[:120])

    def remover():
        while not stop.is_set():
            try:
                tool_factory.uninstall_manager_tool_complete("race_io")
            except Exception as exc:
                errors.append(str(exc)[:120])

    threads = [threading.Thread(target=creator), threading.Thread(target=remover)]
    for thread in threads:
        thread.start()
    import time as _time
    _time.sleep(1.5)
    stop.set()
    for thread in threads:
        thread.join()

    # Invariant: the manifest exists if and only if the source exists.
    manifests = [item["name"] for item in registry.catalog()["tools"]]
    source = (tmp_path / "tools" / "race_io.py").exists()
    assert source == ("race_io" in manifests)
    if source:
        content = (tmp_path / "tools" / "race_io.py").read_text(encoding="utf-8")
        assert "def run" in content
