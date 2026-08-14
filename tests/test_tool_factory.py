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


def test_uninstall_tool_removes_manifest(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    tool_factory.create_manager_tool(
        name="temp_tool", description="临时", code=SAMPLE_CODE, parameters_schema=SAMPLE_SCHEMA,
    )
    assert registry.uninstall_tool("temp_tool")["status"] == "uninstalled"
    assert registry.catalog()["tools"] == []
    assert registry.uninstall_tool("temp_tool")["status"] == "not_found"


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
    assert result["trial_call"]["skipped"] == "未提供 test_args"


def test_create_tool_fulfill_error_is_captured_not_fatal(monkeypatch, tmp_path):
    registry = _isolate(monkeypatch, tmp_path)
    result = tool_factory.create_manager_tool(
        name="fulfill_err_tool",
        description="调用参数错误",
        code=SAMPLE_CODE,
        parameters_schema=SAMPLE_SCHEMA,
        fulfill_args={"missing": "nope"},  # missing required arg raises
    )

    assert result["status"] == "created"  # creation stands
    assert "error" in result["fulfill_result"]



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
