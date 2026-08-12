"""
AI Chat backend for investment-auto.

Features:
- Persistent conversation history and management memory
- A single typed management Agent for semantic requests
- A direct UI command crossing the same investment-Agent boundary
- Streaming-friendly generator for SSE endpoint
- Per-Agent least-privilege tool access
"""

from __future__ import annotations

import json
import logging
import queue
import re
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Mapping, Optional

from src.config import cfg

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_DIR = PROJECT_ROOT / "runtime"
HISTORY_FILE = RUNTIME_DIR / "chat_history.json"
MEMORY_FILE = RUNTIME_DIR / "chat_memory.md"

_history_lock = threading.RLock()
_memory_lock = threading.RLock()
_cancel_lock = threading.RLock()
_cancel_events: "OrderedDict[str, threading.Event]" = OrderedDict()
_active_request_ids: List[str] = []
_MAX_CANCEL_EVENTS = 512

logger = logging.getLogger("investment-auto.chat")

_LEGACY_TOOL_NAMES = {
    "run_shell", "read_file", "write_file", "list_dir",
    "stock_search", "stock_snapshot", "optimizer",
}


# ── history/memory ───────────────────────────────────
def load_history(limit: int = 30) -> List[Dict[str, Any]]:
    with _history_lock:
        if not HISTORY_FILE.exists():
            return []
        try:
            data = json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
            return data[-limit:]
        except Exception:
            return []


def save_history(history: List[Dict[str, Any]]) -> None:
    with _history_lock:
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        HISTORY_FILE.write_text(json.dumps(history[-200:], ensure_ascii=False, indent=2), encoding="utf-8")


def append_history(role: str, content: str) -> None:
    with _history_lock:
        h = load_history(limit=200)
        h.append({"role": role, "content": content, "time": datetime.now().isoformat(timespec="seconds")})
        save_history(h)


def clear_history() -> None:
    save_history([])


def load_memory() -> str:
    with _memory_lock:
        if not MEMORY_FILE.exists():
            return ""
        return MEMORY_FILE.read_text(encoding="utf-8")[-12000:]


def append_memory(note: str, *, source: str = "user") -> Dict[str, Any]:
    """Persist a bounded, non-sensitive operating preference for future chats."""
    cleaned = re.sub(r"\s+", " ", str(note or "")).strip()[:800]
    if not cleaned:
        raise ValueError("记忆内容不能为空")
    if re.search(
        r"api[_ -]?key|token|secret|password|passwd|webhook|mongodb(?:\+srv)?://|"
        r"(?:sk|key)-[A-Za-z0-9_-]{12,}|https?://[^\s]+@",
        cleaned,
        re.I,
    ):
        raise ValueError("记忆中不能保存密钥、令牌、密码、Webhook 或数据库连接地址")
    safe_source = re.sub(r"[^A-Za-z0-9_-]", "", str(source))[:24] or "user"
    with _memory_lock:
        existing = load_memory()
        normalized = cleaned.casefold()
        if any(normalized == line.split("] ", 1)[-1].strip().casefold() for line in existing.splitlines() if line.startswith("- ")):
            return {"status": "exists", "memory": cleaned}
        RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
        lines = [line for line in existing.splitlines() if line.strip()]
        lines.append(f"- {datetime.now().isoformat(timespec='seconds')} [{safe_source}] {cleaned}")
        content = "# Investment-Auto 长期记忆\n\n" + "\n".join(
            line for line in lines if not line.startswith("# ")
        )[-11500:] + "\n"
        temporary = MEMORY_FILE.with_suffix(MEMORY_FILE.suffix + ".tmp")
        temporary.write_text(content, encoding="utf-8")
        temporary.replace(MEMORY_FILE)
    return {"status": "stored", "memory": cleaned}


def handle_investment_cycle_stream(
    market: str,
    *,
    request_id: Optional[str] = None,
    label: str = "button",
) -> Generator[Dict[str, Any], None, None]:
    """Run one complete paper-investment cycle without natural-language routing."""
    normalized = str(market or "").strip().lower()
    if normalized not in {"cn", "hk", "us", "etf"}:
        yield {"type": "error", "content": "market 必须是 cn、hk、us 或 etf"}
        return
    key, cancel_event = _register_cancel_event(request_id)
    names = {"cn": "A 股", "hk": "港股", "us": "美股", "etf": "ETF"}
    append_history("user", f"一键执行{names[normalized]}完整投资轮次")
    try:
        yield from _investment_cycle_events(normalized, label=label, cancel_event=cancel_event)
    finally:
        _unregister_cancel_event(key)


def _investment_cycle_events(
    market: str,
    *,
    label: str,
    cancel_event: threading.Event,
) -> Generator[Dict[str, Any], None, None]:
    yield {"type": "tool", "name": "run_complete_investment_cycle", "params": {"market": market}}
    updates: "queue.Queue[Dict[str, Any]]" = queue.Queue()

    def run_complete_cycle() -> None:
        try:
            from src.investment.command_bus import InvestmentAgentClient

            result = InvestmentAgentClient().issue(
                "run_cycle",
                {"market": market, "label": label},
                requested_by="chat-button",
                progress_callback=lambda value: updates.put({"type": "status", "content": value}),
            )
            updates.put({"type": "result", "value": result})
        except Exception as exc:
            logger.exception("Complete investment cycle failed")
            updates.put({"type": "error", "value": str(exc)[:1000]})

    worker = threading.Thread(target=run_complete_cycle, name=f"investment-cycle-{market}", daemon=True)
    worker.start()
    result: Dict[str, Any] = {}
    error = ""
    while worker.is_alive() or not updates.empty():
        try:
            update = updates.get(timeout=0.5)
        except queue.Empty:
            continue
        if update["type"] == "status":
            yield update
        elif update["type"] == "result":
            result = update["value"]
        elif update["type"] == "error":
            error = update["value"]
    from src.investment.reporting import format_cycle_result

    final_answer = format_cycle_result(result) if result else f"## ❌ 完整投资轮次失败\n\n{error or '未知错误'}"
    emitted = ""
    for chunk in _chunk_text(final_answer, 18):
        emitted += chunk
        yield {"type": "token", "content": chunk}
    append_history("assistant", final_answer)
    yield {"type": "final", "content": final_answer}

# ── public handlers ─────────────────────────────────
def _trim_cancel_events() -> None:
    while len(_cancel_events) > _MAX_CANCEL_EVENTS:
        key = next(iter(_cancel_events))
        if key in _active_request_ids:
            _cancel_events.move_to_end(key)
            if all(item in _active_request_ids for item in _cancel_events):
                break
            continue
        _cancel_events.pop(key, None)


def _register_cancel_event(request_id: Optional[str]) -> tuple[str, threading.Event]:
    key = str(request_id).strip() if request_id else f"legacy:{uuid.uuid4().hex}"
    with _cancel_lock:
        event = _cancel_events.get(key)
        if event is None:
            event = threading.Event()
            _cancel_events[key] = event
        else:
            _cancel_events.move_to_end(key)
        _active_request_ids.append(key)
        _trim_cancel_events()
        return key, event


def _unregister_cancel_event(key: str) -> None:
    with _cancel_lock:
        try:
            _active_request_ids.remove(key)
        except ValueError:
            pass
        _cancel_events.pop(key, None)


def request_cancel(request_id: Optional[str] = None) -> bool:
    """Cancel one request; without an id, target the newest active request."""
    with _cancel_lock:
        if request_id:
            key = str(request_id).strip()
            event = _cancel_events.get(key)
            if event is None:
                # Preserve an early cancellation until that request registers.
                event = threading.Event()
                _cancel_events[key] = event
            _cancel_events.move_to_end(key)
        elif _active_request_ids:
            key = _active_request_ids[-1]
            event = _cancel_events[key]
        else:
            return False
        event.set()
        _trim_cancel_events()
        return True


def clear_cancel(request_id: Optional[str] = None) -> None:
    """Compatibility helper; request startup deliberately never calls this."""
    with _cancel_lock:
        if request_id:
            _cancel_events.pop(str(request_id).strip(), None)


def is_cancelled(request_id: Optional[str] = None) -> bool:
    with _cancel_lock:
        if request_id:
            event = _cancel_events.get(str(request_id).strip())
        elif _active_request_ids:
            event = _cancel_events.get(_active_request_ids[-1])
        else:
            event = None
        return bool(event and event.is_set())


def handle_chat(message: str, thinking: bool = False, provider: Optional[str] = None, model: Optional[str] = None, request_id: Optional[str] = None) -> str:
    parts = []
    for ev in handle_chat_stream(message, thinking, provider=provider, model=model, request_id=request_id):
        if ev.get("type") == "token":
            parts.append(ev.get("content", ""))
        elif ev.get("type") == "final":
            # final already emitted as tokens; ignore
            pass
    return "".join(parts)


def handle_chat_stream(message: str, thinking: bool = False, provider: Optional[str] = None, model: Optional[str] = None, request_id: Optional[str] = None) -> Generator[Dict[str, Any], None, None]:
    """Yield events: token/status/tool/final/error."""
    key, cancel_event = _register_cancel_event(request_id)
    try:
        yield from _handle_chat_stream(message, thinking, provider, model, cancel_event)
    finally:
        _unregister_cancel_event(key)


def _handle_chat_stream(message: str, thinking: bool, provider: Optional[str], model: Optional[str], cancel_event: threading.Event) -> Generator[Dict[str, Any], None, None]:
    append_history("user", message)
    if cancel_event.is_set():
        yield from _emit_cancelled("")
        return
    history = load_history(limit=20)
    memory = load_memory()

    clean_history: List[Dict[str, Any]] = []
    # Exclude the freshly appended user and legacy provider envelopes from context.
    for item in history[:-1]:
        if item.get("role") in ("user", "assistant"):
            content = item.get("content", "")
            if item["role"] == "assistant" and _parse_tool_call(content):
                continue
            clean_history.append({"role": item["role"], "content": content})

    from src.ui.agent_runtime import run_agent_events

    yield {"type": "status", "content": "Agent 正在分析..."}
    final_answer = ""
    try:
        for event in run_agent_events(
            message,
            history=clean_history,
            memory=memory,
            thinking=thinking,
            provider=provider,
            model=model,
            cancel_event=cancel_event,
        ):
            event_type = event.get("type")
            if event_type == "result":
                final_answer = str(event.get("content", "")).strip()
                break
            if event_type == "cancelled":
                yield from _emit_cancelled("")
                return
            if event_type == "error":
                error = str(event.get("content", "Agent 运行失败"))
                append_history("assistant", f"Agent 运行失败：{error}")
                yield {"type": "error", "content": error}
                return
            yield event
    except Exception as exc:
        logger.exception("Typed Agent runtime failed")
        yield {"type": "error", "content": str(exc)}
        return

    if not final_answer:
        error = "Agent 没有产生最终回答。"
        append_history("assistant", error)
        yield {"type": "error", "content": error}
        return
    emitted = ""
    for chunk in _chunk_text(final_answer, 18):
        if cancel_event.is_set():
            yield from _emit_cancelled(emitted)
            return
        emitted += chunk
        yield {"type": "token", "content": chunk}
    append_history("assistant", final_answer)
    yield {"type": "final", "content": final_answer}


def _emit_cancelled(partial_answer: str) -> Generator[Dict[str, Any], None, None]:
    saved = partial_answer.rstrip()
    if saved:
        saved += "\n\n（用户已停止生成）"
    else:
        saved = "（用户已停止生成）"
    append_history("assistant", saved)
    yield {"type": "cancelled", "content": "已停止生成"}


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
        if name in _LEGACY_TOOL_NAMES:
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
    if not isinstance(data, dict):
        return None
    name = data.get("tool") or data.get("tool_name") or data.get("name")
    if name not in _LEGACY_TOOL_NAMES:
        return None
    params = data.get("params", data.get("arguments", data.get("input", {})))
    if isinstance(params, str):
        try:
            params = json.loads(params)
        except json.JSONDecodeError:
            return None
    if not isinstance(params, dict):
        return None
    return {"tool": name, "params": _normalize_params(params)}


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


def _chunk_text(text: str, size: int = 20) -> Generator[str, None, None]:
    for i in range(0, len(text), size):
        yield text[i:i+size]
