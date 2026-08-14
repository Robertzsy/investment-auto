"""Transactional one-shot tool creation for the manager plane.

The manager says "add me a tool that does X" and create_manager_tool runs
the whole pipeline in one atomic call:

    validate inputs (names, schema, forbidden imports, async ban)
      -> write the tool module (atomic; existing tool names are rejected)
      -> import + verify signature/schema/async/serializability
      -> run compile/tests (whitelisted commands only)
      -> register the tool manifest
      -> trial call with mandatory test arguments (failure rolls back)
      -> optional fulfill call for the user's CURRENT request
      -> any failure rolls the code file AND the manifest back together,
         and the rollback verifies both are really gone (rollback_failed
         plus manifest disabling when they are not)

Concurrent creations of the same name serialize on a per-name thread lock
plus a cross-process file lock.
"""

from __future__ import annotations

import ast
import importlib
import inspect
import json
import os
import re
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from pydantic_core import to_jsonable_python

ROOT = Path(__file__).resolve().parents[2]
TOOL_MODULE_DIR = ROOT / "src" / "manager" / "tools"
TIMEZONE = ZoneInfo("Asia/Shanghai")

_NAME_RE = re.compile(r"^[a-z][a-z0-9_]{1,63}$")
_FUNCTION_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

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

_name_locks: Dict[str, threading.Lock] = {}
_name_locks_guard = threading.Lock()
_FILE_LOCK_STALE_SECONDS = 60


def _now() -> str:
    return datetime.now(TIMEZONE).isoformat(timespec="seconds")


def _clean_name(value: str) -> str:
    name = re.sub(r"[^a-z0-9_]+", "_", str(value or "").strip().lower()).strip("_")
    if not _NAME_RE.fullmatch(name):
        raise ValueError("工具名必须是 2-64 位小写字母、数字或下划线，且以字母开头")
    return name


def _per_name_lock(name: str) -> threading.Lock:
    with _name_locks_guard:
        return _name_locks.setdefault(name, threading.Lock())


@contextmanager
def _file_lock(name: str) -> Iterator[None]:
    """Cross-process exclusive lock; stale files (crashed holder) are reaped."""
    directory = ROOT / "runtime" / "manager" / "locks"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{name}.lock"
    acquired = False
    for _attempt in range(50):  # up to ~5s
        try:
            descriptor = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            os.write(descriptor, b"1")
            os.close(descriptor)
            acquired = True
            break
        except FileExistsError:
            try:
                if time.time() - path.stat().st_mtime > _FILE_LOCK_STALE_SECONDS:
                    path.unlink(missing_ok=True)
            except OSError:
                pass
            time.sleep(0.1)
    if not acquired:
        raise RuntimeError(f"工具创建锁获取超时（{name}），可能存在并发进程冲突")
    try:
        yield
    finally:
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


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
    if inspect.iscoroutinefunction(function):
        raise ValueError(f"工具函数 {function_name} 不能是 async 函数")
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


def _trial_invoke(function: Callable[..., Any], arguments: Mapping[str, Any]) -> Any:
    """Invoke and require a JSON-serializable result."""
    value = function(**dict(arguments))
    return to_jsonable_python(value)


def uninstall_manager_tool_complete(name: str) -> Dict[str, Any]:
    """Remove manifest, source module and the import cache entry together.

    This lets a tool with the same name be recreated afterwards.
    """
    from src.manager.capabilities import CapabilityRegistry

    capability = _clean_name(name)
    registry = CapabilityRegistry()
    manifest = registry.uninstall_tool(capability)
    file_path = TOOL_MODULE_DIR / f"{capability}.py"
    source_removed = False
    if file_path.exists():
        file_path.unlink()
        source_removed = True
    sys.modules.pop(f"src.manager.tools.{capability}", None)
    return {
        "status": manifest["status"],
        "name": capability,
        "source_removed": source_removed,
    }


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

    The trial call is mandatory (empty args count as a valid trial for
    parameterless tools) and a failed trial rolls the whole creation back.
    A failed fulfill keeps the tool but reports created_fulfill_failed.
    Rollback verifies that both the module and the manifest really
    disappeared; otherwise it reports rollback_failed and disables the
    manifest.
    """
    capability = _clean_name(name)
    with _per_name_lock(capability):
        with _file_lock(capability):
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
    if not re.search(rf"\n(?:async\s+)?def\s+{re.escape(function_name)}\s*\(", code):
        raise ValueError(f"code 中必须定义函数 def {function_name}(...)")

    module_path = f"src.manager.tools.{capability}"
    file_path = TOOL_MODULE_DIR / f"{capability}.py"
    manifest_path = registry.tool_dir / f"{capability}.json"
    steps: list[str] = []
    created_file = False
    registered = False

    def rollback(stage: str, error: Exception) -> Dict[str, Any]:
        # Code and manifest roll back together, then verify both are gone.
        problems: list[str] = []
        if created_file:
            try:
                if file_path.exists():
                    file_path.unlink()
            except Exception as exc:
                problems.append(f"源码删除失败: {str(exc)[:200]}")
            if file_path.exists():
                problems.append("源码删除后仍存在")
        if registered:
            try:
                registry.uninstall_tool(capability)
            except Exception as exc:
                problems.append(f"清单删除失败: {str(exc)[:200]}")
            if manifest_path.exists():
                # Last resort: disable the manifest so the toolset never loads it.
                try:
                    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
                    if isinstance(payload, dict):
                        payload["enabled"] = False
                        temporary = manifest_path.with_suffix(".json.tmp")
                        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
                        temporary.replace(manifest_path)
                except Exception as exc:
                    problems.append(f"清单禁用失败: {str(exc)[:200]}")
                problems.append("清单删除失败，已禁用")
        if problems:
            return {
                "status": "rollback_failed",
                "name": capability,
                "stage": stage,
                "error": str(error)[:2000],
                "problems": problems,
                "steps": steps,
            }
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
        # A trial call is mandatory: no verification means no registration.
        trial_arguments = dict(test_args) if isinstance(test_args, Mapping) else {}
        try:
            trial = {"result": _trial_invoke(function, trial_arguments)}
        except Exception as exc:
            raise ValueError(f"试调用失败: {str(exc)[:800]}") from exc

        steps.append("fulfill")
        fulfill: Optional[Dict[str, Any]] = None
        if isinstance(fulfill_args, Mapping):
            try:
                fulfill = {"result": _trial_invoke(function, dict(fulfill_args))}
            except Exception as exc:
                fulfill = {"error": str(exc)[:1000]}

        status = "created"
        if fulfill is not None and "error" in fulfill:
            status = "created_fulfill_failed"

        return {
            "status": status,
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
