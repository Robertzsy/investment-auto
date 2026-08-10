"""
AI Chat backend for investment-auto.

Features:
- Persistent conversation history and memory
- Tool execution loop with JSON/DSML/raw JSON parsing
- Streaming-friendly generator for SSE endpoint
- Full project permissions, restricted to project root paths for file IO
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
import traceback
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional

from src.config import cfg

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_DIR = PROJECT_ROOT / "runtime"
HISTORY_FILE = RUNTIME_DIR / "chat_history.json"
MEMORY_FILE = RUNTIME_DIR / "chat_memory.md"
CANCEL_FILE = RUNTIME_DIR / "chat_cancel.flag"

logger = logging.getLogger("investment-auto.chat")

SYSTEM_PROMPT = """你是 Investment-Auto 的 AI 操作助手，拥有对项目的完全读写和执行权限。

你的能力：
1. 读写文件：读取/修改 config/config.yaml、config/market/*.yaml 等配置
2. 执行命令：在项目目录下运行 Python、Node.js、测试、优化器等命令
3. 管理调度：查看/启动/停止调度器、检查日志
4. 分析数据：调用 stock-fetcher 获取行情，调用 optimizer 生成优化报告
5. 修改配置：调整风控、市场开关、轮次时间、模型选择等

重要：如果需要执行操作，必须使用以下 JSON 工具调用格式，不要只把 JSON 当作普通文本回复：

```tool
{"tool": "read_file", "params": {"path_str": "config/config.yaml"}}
```

可用工具：
- read_file: {"path_str": "文件路径"}
- write_file: {"path_str": "文件路径", "content": "内容"}
- run_shell: {"cmd": "命令", "timeout": 60}
- list_dir: {"path_str": "目录路径"}

工具调用规则：
- 要读文件/跑命令/改配置时，先用工具，不要假装已经做了
- 工具返回后，根据结果继续回答
- 如果一次需要多个操作，分多轮工具调用
- 最终回复必须是中文 Markdown，不要再包含工具调用 JSON

安全边界：文件读写限制在项目根目录内。"""

# ── tools ─────────────────────────────────────────────
def run_shell(cmd: str, cwd: Optional[str] = None, timeout: int = 60) -> Dict[str, Any]:
    workdir = cwd or str(PROJECT_ROOT)
    try:
        p = subprocess.run(cmd, shell=True, cwd=workdir, capture_output=True, text=True, timeout=int(timeout))
        return {"stdout": p.stdout[:20000], "stderr": p.stderr[:8000], "returncode": p.returncode}
    except subprocess.TimeoutExpired:
        return {"error": f"命令超时 ({timeout}s)"}
    except Exception as e:
        return {"error": str(e)}


def read_file(path_str: str) -> Dict[str, Any]:
    p = _safe_project_path(path_str)
    if not p or not p.exists():
        return {"error": f"路径不安全或不存在: {path_str}"}
    try:
        content = p.read_text(encoding="utf-8")
        return {"path": str(p.relative_to(PROJECT_ROOT)), "content": content[:30000], "truncated": len(content) > 30000}
    except Exception as e:
        return {"error": str(e)}


def write_file(path_str: str, content: str) -> Dict[str, Any]:
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
    p = _safe_project_path(path_str)
    if not p or not p.exists():
        return {"error": f"路径不存在: {path_str}"}
    try:
        items = []
        for entry in sorted(p.iterdir()):
            items.append({"name": entry.name, "type": "dir" if entry.is_dir() else "file", "size": entry.stat().st_size if entry.is_file() else None})
        return {"path": str(p.relative_to(PROJECT_ROOT)), "items": items}
    except Exception as e:
        return {"error": str(e)}


def _safe_project_path(path_str: str) -> Optional[Path]:
    p = Path(path_str)
    if not p.is_absolute():
        p = PROJECT_ROOT / p
    p = p.resolve()
    try:
        p.relative_to(PROJECT_ROOT)
        return p
    except ValueError:
        return None

TOOLS = {
    "run_shell": {"fn": run_shell},
    "read_file": {"fn": read_file},
    "write_file": {"fn": write_file},
    "list_dir": {"fn": list_dir},
}

# ── history/memory ───────────────────────────────────
def load_history(limit: int = 30) -> List[Dict[str, Any]]:
    if not HISTORY_FILE.exists():
        return []
    try:
        data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        return data[-limit:]
    except Exception:
        return []


def save_history(history: List[Dict[str, Any]]) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    HISTORY_FILE.write_text(json.dumps(history[-200:], ensure_ascii=False, indent=2), encoding="utf-8")


def append_history(role: str, content: str) -> None:
    h = load_history(limit=200)
    h.append({"role": role, "content": content, "time": datetime.now().isoformat(timespec="seconds")})
    save_history(h)


def clear_history() -> None:
    save_history([])


def load_memory() -> str:
    if not MEMORY_FILE.exists():
        return ""
    return MEMORY_FILE.read_text(encoding="utf-8")[:8000]


def append_memory(note: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n- {datetime.now().isoformat(timespec='seconds')}: {note}\n")

# ── public handlers ─────────────────────────────────
def request_cancel() -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    CANCEL_FILE.write_text(datetime.now().isoformat(), encoding="utf-8")


def clear_cancel() -> None:
    if CANCEL_FILE.exists():
        CANCEL_FILE.unlink()


def is_cancelled() -> bool:
    return CANCEL_FILE.exists()


def handle_chat(message: str, thinking: bool = False, provider: Optional[str] = None, model: Optional[str] = None) -> str:
    parts = []
    for ev in handle_chat_stream(message, thinking, provider=provider, model=model):
        if ev.get("type") == "token":
            parts.append(ev.get("content", ""))
        elif ev.get("type") == "final":
            # final already emitted as tokens; ignore
            pass
    return "".join(parts)


def handle_chat_stream(message: str, thinking: bool = False, provider: Optional[str] = None, model: Optional[str] = None) -> Generator[Dict[str, Any], None, None]:
    """Yield events: token/status/tool/final/error."""
    from src.llm.registry import resolve_llm

    clear_cancel()
    append_history("user", message)
    history = load_history(limit=20)
    memory = load_memory()

    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if memory:
        messages.append({"role": "system", "content": f"以下是长期记忆/历史偏好：\n{memory}"})
    # Add recent history except the freshly appended user duplicated later
    for item in history[:-1]:
        if item.get("role") in ("user", "assistant"):
            messages.append({"role": item["role"], "content": item.get("content", "")})
    messages.append({"role": "user", "content": _build_user_message(message)})

    llm = resolve_llm(provider=provider, model=model) if provider else resolve_llm(role="chat")
    final_answer = ""

    last_tool_result = None
    for round_idx in range(8):
        if is_cancelled():
            yield {"type": "cancelled", "content": "已停止生成"}
            append_history("assistant", "（用户已停止生成）")
            clear_cancel()
            return
        yield {"type": "status", "content": "思考中..." if round_idx == 0 else "继续处理工具结果..."}
        kwargs: Dict[str, Any] = {}
        if thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        try:
            resp = llm.chat(messages, temperature=0.25, max_tokens=4096, **kwargs)
        except Exception as e:
            yield {"type": "error", "content": str(e)}
            return

        tool_call = _parse_tool_call(resp)
        if not tool_call:
            final_answer = _strip_tool_blocks(resp)
            # stream by chunks for UI responsiveness
            for chunk in _chunk_text(final_answer, 18):
                if is_cancelled():
                    yield {"type": "cancelled", "content": "已停止生成"}
                    append_history("assistant", raw if 'raw' in locals() else "（用户已停止生成）")
                    clear_cancel()
                    return
                yield {"type": "token", "content": chunk}
            append_history("assistant", final_answer)
            # primitive memory extraction
            if "记住" in message or "remember" in message.lower():
                append_memory(message)
            yield {"type": "final", "content": final_answer}
            return

        # Do not surface raw tool JSON as assistant answer; show tool status instead.
        yield {"type": "tool", "name": tool_call.get("tool"), "params": tool_call.get("params", {})}
        messages.append({"role": "assistant", "content": resp})
        tool_result = _execute_tool(tool_call)
        last_tool_result = tool_result
        messages.append({"role": "user", "content": f"[工具执行结果]\n{json.dumps(tool_result, ensure_ascii=False, indent=2)}\n\n请基于工具结果继续。如果需要更多工具，再调用工具；否则给出最终回答，不要重复工具JSON。"})

    if last_tool_result is not None:
        final_answer = "工具调用轮次较多，已停止继续调用工具。以下是最后一次工具执行结果摘要：\n\n```json\n" + json.dumps(last_tool_result, ensure_ascii=False, indent=2)[:4000] + "\n```"
    else:
        final_answer = "工具调用轮次过多，已停止。请拆分任务或查看日志。"
    for chunk in _chunk_text(final_answer, 18):
        yield {"type": "token", "content": chunk}
    append_history("assistant", final_answer)
    yield {"type": "final", "content": final_answer}


def _build_user_message(user_text: str) -> str:
    return f"{user_text}\n\n项目根目录: {PROJECT_ROOT}\n如需操作，请使用工具。"

# ── parsing ──────────────────────────────────────────
def _parse_tool_call(text: str) -> Optional[Dict[str, Any]]:
    # 1. fenced tool block
    m = re.search(r"```tool\s*\n(.*?)\n\s*```", text, re.DOTALL)
    if m:
        tc = _loads_tool_json(m.group(1).strip())
        if tc:
            return tc

    # 2. DeepSeek DSML
    dsml = re.search(r'<｜｜DSML｜｜invoke\s+name="(\w+)"[^>]*>(.*?)</｜｜DSML｜｜invoke>', text, re.DOTALL)
    if dsml:
        name, body = dsml.group(1), dsml.group(2)
        if name in TOOLS:
            params: Dict[str, Any] = {}
            for pname, pvalue in re.findall(r'<｜｜DSML｜｜parameter\s+name="(\w+)"[^>]*>(.*?)</｜｜DSML｜｜parameter>', body, re.DOTALL):
                params[pname] = pvalue.strip()
            return {"tool": name, "params": _normalize_params(params)}

    # 3. raw JSON object in assistant output
    for obj in _extract_json_objects(text):
        tc = _loads_tool_json(obj)
        if tc:
            return tc
    return None


def _loads_tool_json(text: str) -> Optional[Dict[str, Any]]:
    try:
        data = json.loads(text)
    except Exception:
        return None
    if isinstance(data, dict) and data.get("tool") in TOOLS:
        return {"tool": data["tool"], "params": _normalize_params(data.get("params", {}))}
    return None


def _extract_json_objects(text: str) -> List[str]:
    objs: List[str] = []
    stack = 0
    start = None
    in_str = False
    esc = False
    for i, ch in enumerate(text):
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == "{":
            if stack == 0:
                start = i
            stack += 1
        elif ch == "}":
            if stack:
                stack -= 1
                if stack == 0 and start is not None:
                    objs.append(text[start:i+1])
                    start = None
    return objs


def _normalize_params(params: Dict[str, Any]) -> Dict[str, Any]:
    out = {}
    for k, v in (params or {}).items():
        if k in ("filePath", "filepath", "path", "dirPath", "dirpath"):
            out["path_str"] = v
        elif k in ("command",):
            out["cmd"] = v
        else:
            out[k] = v
    return out


def _strip_tool_blocks(text: str) -> str:
    text = re.sub(r"```tool\s*\n.*?\n\s*```", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>", "", text, flags=re.DOTALL).strip()
    return text


def _chunk_text(text: str, size: int = 20) -> Generator[str, None, None]:
    for i in range(0, len(text), size):
        yield text[i:i+size]


def _execute_tool(tool_call: Dict[str, Any]) -> Dict[str, Any]:
    name = tool_call.get("tool")
    params = _normalize_params(tool_call.get("params", {}))
    fn = TOOLS[name]["fn"]
    try:
        return fn(**params)
    except Exception as e:
        return {"error": str(e), "traceback": traceback.format_exc()}
