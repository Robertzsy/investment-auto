"""Transactional one-shot tool creation for the manager plane.

The manager says "add me a tool that does X" and create_manager_tool runs
the whole pipeline in one atomic call:

    validate inputs
      -> write the tool module (atomic; existing tool names are rejected)
      -> import + verify the function signature against the JSON schema
      -> run compile/tests (whitelisted commands only)
      -> register the tool manifest
      -> make one verification call with caller-provided test arguments
      -> any failure rolls back the code file AND the manifest together

Registration lands in the runtime capability catalog; the conversation
runtime rebuilds its toolset on every request, so the new tool is available
from the next message.  The verification call inside this tool closes the
loop without needing the model to invoke the new tool in the same turn.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import re
import sys
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[2]
TOOL_MODULE_DIR = ROOT / "src" / "manager" / "tools"
TIMEZONE = ZoneInfo("Asia/Shanghai")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_FUNCTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_creation_lock = threading.RLock()

# Tool code may read data and compute, but must never reach execution
# paths: the manager plane has no trading, account-write or shell power,
# and a fulfill call runs the new code immediately.
_FORBIDDEN_IMPORTS = (
    "src.trading",
    "src.portfolio",
    "src.investment",
    "src.research.sandbox",
    "subprocess",
)


def _reject_forbidden_imports(code: str) -> None:
    """Reject tool source that imports execution-related modules."""
    try:
        tree = ast.parse(code)
    except SyntaxError:
        return  # surfaced later by the import/tests steps
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                _check_forbidden(alias.name)
        elif isinstance(node, ast.ImportFrom):
            _check_forbidden(str(node.module or ""))


def _check_forbidden(module_name: str) -> None:
    name = str(module_name or "")
    if any(name == banned or name.startswith(banned + ".") for banned in _FORBIDDEN_IMPORTS):
        raise ValueError(
            f"工具代码不允许导入执行相关模块: {name}。工具只能读取数据、查询与计算，"
            "不得触达交易、账户写入或命令执行。"
        )


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _clean_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    if not _NAME_RE.fullmatch(name):
        raise ValueError("工具名必须是 2-64 位小写字母、数字或下划线，且以字母开头")
    return name


def _validate_schema(schema: Any) -> Dict[str, Any]:
    if isinstance(schema, str):
        try:
            schema = json.loads(schema)
        except json.JSONDecodeError as exc:
            raise ValueError(f"parameters_schema 不是合法 JSON: {exc}") from exc
    if not isinstance(schema, Mapping):
        raise ValueError("parameters_schema 必须是 JSON 对象")
    if schema.get("type") != "object" or not isinstance(schema.get("properties"), Mapping):
        raise ValueError("parameters_schema 必须是 type=object 且包含 properties 的 JSON schema")
    return dict(schema)


def _verify_function(schema: Mapping[str, Any], function: Callable[..., Any], function_name: str) -> None:
    signature = inspect.signature(function)
    properties = schema.get("properties", {})
    if not isinstance(properties, Mapping):
        return
    required = set(schema.get("required", []) or [])
    parameters = set(signature.parameters)
    missing = sorted(
        name for name in required
        if isinstance(name, str) and name not in parameters
    )
    if missing:
        raise ValueError(
            f"函数 {function_name} 缺少 schema 中 required 的参数: {', '.join(missing)}"
        )


def create_manager_tool(
    *,
    name: str,
    description: str,
    code: str,
    function_name: str = "run",
    parameters_schema: Any = None,
    test_args: Optional[Mapping[str, Any]] = None,
    fulfill_args: Optional[Mapping[str, Any]] = None,
    reason: str = "manager-requested-tool",
    tests: Sequence[str] = (),

    reserved_names: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Create, verify, register and trial-run one manager tool atomically.

    On any failure the freshly written module and the freshly registered
    manifest are both removed, so a half-installed tool can never linger.
    Concurrent conversations serialize on the creation lock so two
    sessions cannot race the same tool name.
    """
    with _creation_lock:
        return _create_manager_tool_locked(
            name=name,
            description=description,
            code=code,
            function_name=function_name,
            parameters_schema=parameters_schema,
            test_args=test_args,
            fulfill_args=fulfill_args,
            reason=reason,
            tests=tests,
            reserved_names=reserved_names,
        )


def _create_manager_tool_locked(
    *,
    name: str,
    description: str,
    code: str,
    function_name: str = "run",
    parameters_schema: Any = None,
    test_args: Optional[Mapping[str, Any]] = None,
    fulfill_args: Optional[Mapping[str, Any]] = None,
    reason: str = "manager-requested-tool",
    tests: Sequence[str] = (),
    reserved_names: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Locked implementation; see create_manager_tool for the contract."""
    from src.manager.capabilities import CapabilityRegistry

    registry = CapabilityRegistry()
    capability = _clean_name(name)
    description = re.sub(r"\s+", " ", str(description or "").strip())[:1000]
    if not description:
        raise ValueError("description 不能为空")
    function_name = str(function_name or "run").strip()
    if not _FUNCTION_RE.fullmatch(function_name):
        raise ValueError("function_name 必须是合法的 Python 函数名")
    schema = _validate_schema(parameters_schema)
    if reserved_names and capability in reserved_names:
        raise ValueError(f"工具名 {capability} 与内置工具冲突，请换一个名字")
    existing = {item.get("name") for item in registry.catalog().get("tools", [])}
    if capability in existing:
        raise ValueError(f"工具 {capability} 已注册；请先卸载或换名")

    code = str(code or "")
    if not code.strip():
        raise ValueError("code 不能为空")
    _reject_forbidden_imports(code)
    if not re.search(rf"\ndef\s+{re.escape(function_name)}\s*\(", code):
        raise ValueError(f"code 中必须定义函数 def {function_name}(...)")

    module_path = f"src.manager.tools.{capability}"
    file_path = TOOL_MODULE_DIR / f"{capability}.py"
    steps: list[str] = []
    created_file = False
    registered = False

    def rollback(stage: str, error: Exception) -> Dict[str, Any]:
        # Code and manifest roll back together.  Existing tools are never
        # overwritten, so removing the fresh file restores the prior state.
        if created_file:
            try:
                if file_path.exists():
                    file_path.unlink()
            except Exception:
                pass
        if registered:
            try:
                registry.uninstall_tool(capability)
            except Exception:
                pass
        return {
            "status": "rolled_back",
            "name": capability,
            "stage": stage,
            "error": str(error)[:2000],
            "steps": steps,
        }

    try:
        steps.append("validate")
        if file_path.exists():
            raise ValueError(f"工具模块 {module_path} 已存在；工具名不可覆盖，请换名")

        steps.append("write_module")
        file_path.parent.mkdir(parents=True, exist_ok=True)
        temporary = file_path.with_suffix(".py.tmp")
        temporary.write_text(code, encoding="utf-8")
        temporary.replace(file_path)
        created_file = True

        steps.append("import_verify")
        importlib.invalidate_caches()
        # Load straight from the file so the tool directory may be
        # redirected (tests) without depending on sys.path resolution.
        spec = importlib.util.spec_from_file_location(module_path, file_path)
        if spec is None or spec.loader is None:
            raise ValueError(f"无法从文件加载模块: {file_path}")
        sys.modules.pop(module_path, None)
        module = importlib.util.module_from_spec(spec)
        sys.modules[module_path] = module
        spec.loader.exec_module(module)
        function = getattr(module, function_name, None)
        if not callable(function):
            raise ValueError(f"模块 {module_path} 中没有可调用函数 {function_name}")
        _verify_function(schema, function, function_name)

        steps.append("tests")
        from src.manager.change_manager import ChangeManager

        test_commands = list(tests) or ["python -m compileall src"]
        test_results = ChangeManager._run_tests(test_commands)
        failed = [item for item in test_results if item["returncode"] != 0]
        if failed:
            raise ValueError(
                "测试未通过: "
                + "; ".join(f"{item['command']} -> {item['returncode']}" for item in failed)[:800]
            )

        steps.append("register")
        registry.install_tool(
            capability,
            description,
            module_path,
            function_name,
            json.dumps(schema, ensure_ascii=False),
        )
        registered = True

        steps.append("trial_call")
        trial: Optional[Dict[str, Any]] = None
        if isinstance(test_args, Mapping):
            try:
                trial = {"result": function(**dict(test_args))}
            except Exception as exc:
                trial = {"error": str(exc)[:1000]}
        else:
            trial = {"skipped": "未提供 test_args"}

        steps.append("fulfill")
        fulfill: Optional[Dict[str, Any]] = None
        if isinstance(fulfill_args, Mapping):
            try:
                fulfill = {"result": function(**dict(fulfill_args))}
            except Exception as exc:
                fulfill = {"error": str(exc)[:1000]}
        else:
            fulfill = None

        return {
            "status": "created",
            "name": capability,
            "module": module_path,
            "function": function_name,
            "description": description,
            "registered": True,
            "available_from": "next_message",
            "trial_call": trial,
            "fulfill_result": fulfill,
            "tests": test_results,
            "steps": steps,
            "created_at": _now(),
        }
    except Exception as exc:
        return rollback(steps[-1] if steps else "unknown", exc)
