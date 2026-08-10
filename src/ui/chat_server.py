"""
AI Chat Web Server for investment-auto.

Serves the chat UI at / and a backend JSON API at /api/chat.
The AI has full permission to read/modify project files, run optimizer,
manage scheduler, and execute safe shell commands.
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional

from src.config import cfg

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent

logger = logging.getLogger("investment-auto.chat")

# ── system prompt (full access) ───────────────────────
SYSTEM_PROMPT = """你是 Investment-Auto 的 AI 操作助手，拥有对项目的完全读写和执行权限。

你的能力：
1. **读写文件**：可以读取、修改 config/config.yaml、config/market/*.yaml 等配置文件
2. **执行命令**：可以在项目目录下执行 Python 脚本（如 optimizer）、Node.js 脚本（stock-fetcher）、系统命令
3. **管理调度**：可以启停调度器，查看运行日志
4. **分析数据**：可以调用 stock-fetcher 获取行情，调用 optimizer 生成优化报告
5. **修改配置**：可以调整风控参数、市场开关、轮次时间、模型选择等

项目路径：/app（Docker）或 Python sys.path[0] 所在目录

**重要：工具调用格式**
当需要执行操作时，使用以下 JSON 格式（不要用 DSML 或其他格式）：

```tool
{"tool": "read_file", "params": {"path_str": "config/config.yaml"}}
```

可用工具：
- read_file: 读取文件，参数 {"path_str": "文件路径"}
- write_file: 写入文件，参数 {"path_str": "文件路径", "content": "内容"}
- run_shell: 执行命令，参数 {"cmd": "命令", "timeout": 60}
- list_dir: 列出目录，参数 {"path_str": "目录路径"}

回复要求：
- 使用中文 Markdown 格式，可以用表格、代码块、列表等
- 执行操作时，先在回复中说明将要做什么，然后调用工具执行
- 执行结果用代码块展示或表格总结
- 如果操作有风险（如修改风控参数），提醒用户确认

项目文件结构：
- config/config.yaml: 主配置
- config/market/cn.yaml, hk.yaml, us.yaml, etf.yaml: 市场风控
- src/optimizer/engine.py: 组合优化引擎
- scripts/stock-fetcher.js: 跨市场行情采集
- runtime/data/portfolio.json: 模拟账户
- runtime/reports/: 投资报告"""

# ── tool definitions ───────────────────────────────
def run_shell(cmd: str, cwd: Optional[str] = None, timeout: int = 60) -> Dict[str, Any]:
    """Execute a shell command in the project directory. Returns stdout, stderr, returncode."""
    workdir = cwd or str(PROJECT_ROOT)
    try:
        p = subprocess.run(
            cmd, shell=True, cwd=workdir,
            capture_output=True, text=True, timeout=timeout
        )
        return {"stdout": p.stdout[:10000], "stderr": p.stderr[:4000], "returncode": p.returncode}
    except subprocess.TimeoutExpired:
        return {"error": f"命令超时 ({timeout}s)"}
    except Exception as e:
        return {"error": str(e)}

def read_file(path_str: str) -> Dict[str, Any]:
    """Read a file under the project directory."""
    p = _safe_project_path(path_str)
    if not p:
        return {"error": f"路径不安全或不存在: {path_str}"}
    try:
        content = p.read_text(encoding="utf-8")
        return {"path": str(p.relative_to(PROJECT_ROOT)), "content": content[:20000], "truncated": len(content) > 20000}
    except Exception as e:
        return {"error": str(e)}

def write_file(path_str: str, content: str) -> Dict[str, Any]:
    """Write content to a file under the project directory. Creates parent dirs if needed."""
    p = _safe_project_path(path_str)
    if not p:
        return {"error": f"路径不安全: {path_str}"}
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
        return {"path": str(p.relative_to(PROJECT_ROOT)), "written": len(content)}
    except Exception as e:
        return {"error": str(e)}

def list_dir(path_str: str = ".") -> Dict[str, Any]:
    """List files in a project directory."""
    p = _safe_project_path(path_str)
    if not p or not p.exists():
        return {"error": f"路径不存在: {path_str}"}
    try:
        items = []
        for entry in sorted(p.iterdir()):
            items.append({"name": entry.name, "type": "dir" if entry.is_dir() else "file",
                          "size": entry.stat().st_size if entry.is_file() else None})
        return {"path": str(p.relative_to(PROJECT_ROOT)), "items": items}
    except Exception as e:
        return {"error": str(e)}

def _safe_project_path(path_str: str) -> Optional[Path]:
    """Resolve path to absolute, reject anything outside project root."""
    p = (PROJECT_ROOT / path_str).resolve()
    try:
        p.relative_to(PROJECT_ROOT)
        return p
    except ValueError:
        return None

TOOLS = {
    "run_shell": {"fn": run_shell, "desc": "执行 shell 命令", "params": {"cmd": "string", "cwd": "optional string", "timeout": "int"}},
    "read_file": {"fn": read_file, "desc": "读取项目文件", "params": {"path_str": "string", "filePath": "alias for path_str"}},
    "write_file": {"fn": write_file, "desc": "写入项目文件", "params": {"path_str": "string", "content": "string", "filePath": "alias for path_str"}},
    "list_dir": {"fn": list_dir, "desc": "列出目录文件", "params": {"path_str": "string default='.'", "dirPath": "alias for path_str"}},
}

# ── chat handler ────────────────────────────────
def handle_chat(message: str, thinking: bool = False) -> str:
    """Main entry: send user message to LLM, return reply."""
    from src.llm.registry import resolve_llm

    llm = resolve_llm(role="chat")  # can be overridden in config

    messages: List[Dict[str, str]] = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": _build_user_message(message)},
    ]

    # simple tool-calling loop (max 5 rounds)
    for _round in range(5):
        kwargs: Dict[str, Any] = {}
        if thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        # For glm / kimi which may use "enable_thinking"
        resp = llm.chat(messages, temperature=0.3, max_tokens=4096, **kwargs)
        messages.append({"role": "assistant", "content": resp})

        # check if LLM wants to call a tool
        tool_call = _parse_tool_call(resp)
        if not tool_call:
            # no tool call → final reply
            return resp

        # execute tool
        tool_result = _execute_tool(tool_call)
        messages.append({
            "role": "user",
            "content": f"[工具执行结果]\n{json.dumps(tool_result, ensure_ascii=False, indent=2)}"
        })

    return resp  # fallback return last response

def _build_user_message(user_text: str) -> str:
    return (
        f"{user_text}\n\n"
        f"你可以使用以下工具来完成任务：read_file, write_file, run_shell, list_dir。\n"
        f"如果不需要执行操作，直接回复我即可。\n"
        f"项目路径: {PROJECT_ROOT}"
    )

def _parse_tool_call(text: str) -> Optional[Dict[str, Any]]:
    """Try to extract a tool-call from LLM output.
    Supports multiple formats:
    1. ```tool\n{"tool": "...", "params": {...}}\n```
    2. DSML format: <｜｜DSML｜｜tool_calls>...<｜｜DSML｜｜invoke name="read_file">...
    3. OpenAI function calling format
    """
    import re
    
    # Format 1: ```tool JSON block
    m = re.search(r'```tool\s*\n(.*?)\n\s*```', text, re.DOTALL)
    if m:
        try:
            data = json.loads(m.group(1))
            if data.get("tool") in TOOLS:
                return data
        except json.JSONDecodeError:
            pass
    
    # Format 2: DSML format (DeepSeek)
    # <｜｜DSML｜｜invoke name="read_file">
    # <｜｜DSML｜｜parameter name="filePath" string="true">/path/to/file</｜｜DSML｜｜parameter>
    # </｜｜DSML｜｜invoke>
    dsml_match = re.search(r'<｜｜DSML｜｜invoke\s+name="(\w+)"[^>]*>(.*?)</｜｜DSML｜｜invoke>', text, re.DOTALL)
    if dsml_match:
        tool_name = dsml_match.group(1)
        params_text = dsml_match.group(2)
        if tool_name in TOOLS:
            # Extract parameters
            params = {}
            param_matches = re.findall(r'<｜｜DSML｜｜parameter\s+name="(\w+)"[^>]*>([^<]*)</｜｜DSML｜｜parameter>', params_text)
            for pname, pvalue in param_matches:
                params[pname] = pvalue
            return {"tool": tool_name, "params": params}
    
    # Format 3: Simple JSON in text
    # {"tool": "read_file", "params": {"path_str": "..."}}
    json_match = re.search(r'\{[^{}]*"tool"\s*:\s*"(\w+)"[^{}]*"params"\s*:\s*\{([^{}]*)\}[^{}]*\}', text)
    if json_match:
        try:
            tool_name = json_match.group(1)
            if tool_name in TOOLS:
                # Try to parse the full JSON
                full_match = re.search(r'\{[^{}]*"tool"[^{}]*\}', text)
                if full_match:
                    data = json.loads(full_match.group(0))
                    if data.get("tool") in TOOLS:
                        return data
        except json.JSONDecodeError:
            pass
    
    return None

def _execute_tool(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    tool_name = tool_call["tool"]
    params = tool_call.get("params", {})
    fn = TOOLS[tool_name]["fn"]
    
    # Normalize parameter names (handle aliases)
    normalized = {}
    for k, v in params.items():
        if k in ("filePath", "filepath"):
            normalized["path_str"] = v
        elif k in ("dirPath", "dirpath"):
            normalized["path_str"] = v
        else:
            normalized[k] = v
    
    try:
        result = fn(**normalized)
        return result
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}
