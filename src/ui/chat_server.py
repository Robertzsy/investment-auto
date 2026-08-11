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
import threading
import traceback
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Optional
from zoneinfo import ZoneInfo

from src.config import cfg
from src.subprocess_utils import decode_subprocess_output

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
RUNTIME_DIR = PROJECT_ROOT / "runtime"
HISTORY_FILE = RUNTIME_DIR / "chat_history.json"
MEMORY_FILE = RUNTIME_DIR / "chat_memory.md"

_history_lock = threading.RLock()
_cancel_lock = threading.RLock()
_cancel_events: "OrderedDict[str, threading.Event]" = OrderedDict()
_active_request_ids: List[str] = []
_MAX_CANCEL_EVENTS = 512
_MAX_TOOL_ROUNDS = 12

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
- stock_search: {"query": "股票名称或代码"}
- stock_snapshot: {"code": "股票代码，如 600519 / sh600519 / hk00700 / AAPL"}
- optimizer: {"market": "cn", "symbols": ["600519", "000858"]}；symbols 可省略，使用持仓+默认标的

工具调用规则：
- 要读文件/跑命令/改配置时，先用工具，不要假装已经做了
- 分析股票时优先使用 stock_search / stock_snapshot，不要用 run_shell 试探 node/python 版本
- 工具返回后，根据结果继续回答
- 如果一次需要多个操作，分多轮工具调用
- 最终回复必须是中文 Markdown，不要再包含工具调用 JSON

安全边界：文件读写限制在项目根目录内。"""

# ── tools ─────────────────────────────────────────────
def run_shell(cmd: str, cwd: Optional[str] = None, timeout: int = 60) -> Dict[str, Any]:
    workdir = cwd or str(PROJECT_ROOT)
    try:
        p = subprocess.run(cmd, shell=True, cwd=workdir, capture_output=True, timeout=int(timeout))
        stdout = decode_subprocess_output(p.stdout)
        stderr = decode_subprocess_output(p.stderr)
        return {"stdout": stdout[:20000], "stderr": stderr[:8000], "returncode": p.returncode}
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

def _optimizer_tool(market: str = "cn", symbols: Any = None, cancel_event: Optional[threading.Event] = None) -> Dict[str, Any]:
    from src.optimizer.runner import compact_result, parse_symbols, run_optimizer

    if isinstance(symbols, str):
        parsed = parse_symbols(symbols)
    elif isinstance(symbols, (list, tuple)):
        parsed = [str(item) for item in symbols]
    elif symbols is None:
        parsed = None
    else:
        return {"error": "symbols 必须是逗号分隔字符串或数组"}
    try:
        return compact_result(run_optimizer(market=str(market or "cn"), symbols=parsed, cancel_event=cancel_event))
    except Exception as exc:
        return {"error": str(exc)}


TOOLS = {
    "run_shell": {"fn": run_shell},
    "read_file": {"fn": read_file},
    "write_file": {"fn": write_file},
    "list_dir": {"fn": list_dir},
    "stock_search": {"fn": lambda query: _stock_fetcher("search", query)},
    "stock_snapshot": {"fn": lambda code: _stock_fetcher("snapshot", code)},
    "optimizer": {"fn": _optimizer_tool},
}


def _stock_fetcher(command: str, value: str) -> Dict[str, Any]:
    """Call bundled stock-fetcher.js and return parsed JSON."""
    script = PROJECT_ROOT / "scripts" / "stock-fetcher.js"
    try:
        p = subprocess.run(
            ["node", str(script), command, str(value)],
            cwd=str(PROJECT_ROOT),
            capture_output=True,
            timeout=45,
        )
        text = decode_subprocess_output(p.stdout).strip()
        stderr = decode_subprocess_output(p.stderr)
        try:
            data = json.loads(text) if text else {}
        except Exception:
            data = {"stdout": text[:8000]}
        if p.returncode != 0:
            data["stderr"] = stderr[:4000]
            data["returncode"] = p.returncode
        return data
    except Exception as e:
        return {"error": str(e)}

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
    if not MEMORY_FILE.exists():
        return ""
    return MEMORY_FILE.read_text(encoding="utf-8")[:8000]


def append_memory(note: str) -> None:
    RUNTIME_DIR.mkdir(parents=True, exist_ok=True)
    with MEMORY_FILE.open("a", encoding="utf-8") as f:
        f.write(f"\n- {datetime.now().isoformat(timespec='seconds')}: {note}\n")

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
    from src.llm.registry import resolve_llm

    append_history("user", message)
    if cancel_event.is_set():
        yield from _emit_cancelled("")
        return
    history = load_history(limit=20)
    memory = load_memory()

    messages: List[Dict[str, str]] = [{"role": "system", "content": SYSTEM_PROMPT}]
    if memory:
        messages.append({"role": "system", "content": f"以下是长期记忆/历史偏好：\n{memory}"})
    # Add recent history except the freshly appended user duplicated later
    for item in history[:-1]:
        if item.get("role") in ("user", "assistant"):
            content = item.get("content", "")
            # Older versions could save provider-specific tool envelopes as if
            # they were final answers. Do not teach the model to repeat them.
            if item["role"] == "assistant" and _parse_tool_call(content):
                continue
            messages.append({"role": item["role"], "content": content})
    messages.append({"role": "user", "content": _build_user_message(message)})

    market_status_answer = _build_market_status_answer(message)
    if market_status_answer is not None:
        yield {"type": "tool", "name": "market_status", "params": {}}
        emitted = ""
        for chunk in _chunk_text(market_status_answer, 18):
            if cancel_event.is_set():
                yield from _emit_cancelled(emitted)
                return
            emitted += chunk
            yield {"type": "token", "content": chunk}
        append_history("assistant", market_status_answer)
        yield {"type": "final", "content": market_status_answer}
        return

    optimizer_request = _build_optimizer_request(message)
    if optimizer_request is not None:
        yield {"type": "tool", "name": "optimizer", "params": optimizer_request}
        result = _optimizer_tool(**optimizer_request, cancel_event=cancel_event)
        final_answer = _format_optimizer_result(result)
        emitted = ""
        for chunk in _chunk_text(final_answer, 18):
            if cancel_event.is_set():
                yield from _emit_cancelled(emitted)
                return
            emitted += chunk
            yield {"type": "token", "content": chunk}
        append_history("assistant", final_answer)
        yield {"type": "final", "content": final_answer}
        return

    llm = resolve_llm(provider=provider, model=model) if provider else resolve_llm(role="chat")
    stock_context = _build_stock_analysis_context(message)
    if stock_context:
        yield {"type": "tool", "name": "stock_snapshot", "params": {"query": stock_context.get("query")}}
        final_messages = [
            {"role": "system", "content": "你是专业但谨慎的中文投资分析助手。只能基于用户给出的行情快照分析，不要再调用任何工具；输出要包含：行情概况、技术面、风险点、操作参考。必须提示不构成投资建议。"},
            {"role": "user", "content": f"用户问题：{message}\n\n股票数据快照：\n{json.dumps(stock_context, ensure_ascii=False, indent=2)[:18000]}"},
        ]
        response_parts: List[str] = []
        try:
            for delta in _stream_llm(llm, final_messages, cancel_event, temperature=0.25, max_tokens=1800):
                response_parts.append(delta)
            resp = "".join(response_parts)
        except InterruptedError:
            yield from _emit_cancelled("")
            return
        except Exception as e:
            yield {"type": "error", "content": str(e)}
            return
        final_answer = _strip_tool_blocks(resp).strip()
        emitted = ""
        for chunk in _chunk_text(final_answer, 18):
            if cancel_event.is_set():
                yield from _emit_cancelled(emitted)
                return
            emitted += chunk
            yield {"type": "token", "content": chunk}
        append_history("assistant", final_answer)
        yield {"type": "final", "content": final_answer}
        return

    final_answer = ""

    last_tool_result = None
    for round_idx in range(_MAX_TOOL_ROUNDS):
        if cancel_event.is_set():
            yield from _emit_cancelled("")
            return
        yield {"type": "status", "content": "思考中..." if round_idx == 0 else "继续处理工具结果..."}
        kwargs: Dict[str, Any] = {}
        if thinking:
            kwargs["extra_body"] = {"thinking": {"type": "enabled"}}
        response_parts = []
        try:
            for delta in _stream_llm(llm, messages, cancel_event, temperature=0.25, max_tokens=4096, **kwargs):
                response_parts.append(delta)
            resp = "".join(response_parts)
        except InterruptedError:
            yield from _emit_cancelled("")
            return
        except Exception as e:
            yield {"type": "error", "content": str(e)}
            return

        tool_call = _parse_tool_call(resp)
        if not tool_call:
            final_answer = _strip_tool_blocks(resp)
            # stream by chunks for UI responsiveness
            emitted = ""
            for chunk in _chunk_text(final_answer, 18):
                if cancel_event.is_set():
                    yield from _emit_cancelled(emitted)
                    return
                emitted += chunk
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
        logger.info("Executing chat tool: %s", tool_call.get("tool"))
        tool_result = _execute_tool(tool_call)
        last_tool_result = tool_result
        messages.append({"role": "user", "content": f"[工具执行结果]\n{json.dumps(tool_result, ensure_ascii=False, indent=2)}\n\n请基于工具结果继续。如果需要更多工具，再调用工具；否则给出最终回答，不要重复工具JSON。"})

    if last_tool_result is not None:
        final_answer = "工具调用轮次较多，已停止继续调用工具。以下是最后一次工具执行结果摘要：\n\n```json\n" + json.dumps(last_tool_result, ensure_ascii=False, indent=2)[:4000] + "\n```"
    else:
        final_answer = "工具调用轮次过多，已停止。请拆分任务或查看日志。"
    emitted = ""
    for chunk in _chunk_text(final_answer, 18):
        if cancel_event.is_set():
            yield from _emit_cancelled(emitted)
            return
        emitted += chunk
        yield {"type": "token", "content": chunk}
    append_history("assistant", final_answer)
    yield {"type": "final", "content": final_answer}


def _stream_llm(llm: Any, messages: List[Dict[str, str]], cancel_event: threading.Event, **kwargs: Any) -> Generator[str, None, None]:
    """Use streaming providers while retaining compatibility with simple test/provider doubles."""
    stream_fn = getattr(llm, "chat_stream", None)
    if callable(stream_fn):
        yield from stream_fn(messages, cancel_event=cancel_event, **kwargs)
        return
    if cancel_event.is_set():
        raise InterruptedError("chat completion cancelled")
    text = llm.chat(messages, **kwargs)
    if cancel_event.is_set():
        raise InterruptedError("chat completion cancelled")
    if text:
        yield text


def _emit_cancelled(partial_answer: str) -> Generator[Dict[str, Any], None, None]:
    saved = partial_answer.rstrip()
    if saved:
        saved += "\n\n（用户已停止生成）"
    else:
        saved = "（用户已停止生成）"
    append_history("assistant", saved)
    yield {"type": "cancelled", "content": "已停止生成"}


def _build_user_message(user_text: str) -> str:
    return f"{user_text}\n\n项目根目录: {PROJECT_ROOT}\n如需操作，请使用工具。"


def _market_status_request(user_text: str) -> Optional[str]:
    if not re.search(r"开始了吗|开始操作|开始交易|开盘了吗|交易了吗|运行状态|调度状态|当前状态|现在.*(?:运行|交易|操作)", user_text, re.I):
        return None
    if re.search(r"美股|美国股市|(?<![A-Za-z])US(?![A-Za-z])", user_text, re.I):
        return "us"
    if re.search(r"港股|香港股市|(?<![A-Za-z])HK(?![A-Za-z])", user_text, re.I):
        return "hk"
    if re.search(r"ETF|场内基金", user_text, re.I):
        return "etf"
    if re.search(r"A股|沪深|中国股市|(?<![A-Za-z])CN(?![A-Za-z])", user_text, re.I):
        return "cn"
    return None


def _status_now(value: Optional[datetime] = None) -> datetime:
    timezone = ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai"))
    if value is None:
        return datetime.now(timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _session_is_active(market: str, current: datetime, sessions: List[Dict[str, Any]]) -> bool:
    from src.scheduler import _day_of_week, _expand_days

    if current.weekday() not in _expand_days(_day_of_week(market, current.strftime("%H:%M"))):
        return False
    current_minute = current.hour * 60 + current.minute
    for session in sessions:
        try:
            start_hour, start_minute = (int(value) for value in str(session.get("start", "")).split(":", 1))
            end_hour, end_minute = (int(value) for value in str(session.get("end", "")).split(":", 1))
        except (TypeError, ValueError):
            continue
        start = start_hour * 60 + start_minute
        end = end_hour * 60 + end_minute
        if start <= end and start <= current_minute <= end:
            return True
        if start > end and (current_minute >= start or current_minute <= end):
            return True
    return False


def _format_session(sessions: List[Dict[str, Any]]) -> str:
    parts = []
    for session in sessions:
        start = str(session.get("start", ""))
        end = str(session.get("end", ""))
        if start and end:
            suffix = "（跨午夜）" if start > end else ""
            parts.append(f"{start}–{end}{suffix}")
    return " / ".join(parts) or "未配置"


def _next_scheduled_time(market: str, time_values: List[str], current: datetime) -> Optional[datetime]:
    from src.scheduler import _cron_trigger

    candidates = []
    for time_value in time_values:
        fire_time = _cron_trigger(market, str(time_value)).get_next_fire_time(None, current)
        if fire_time is not None:
            candidates.append(fire_time)
    return min(candidates) if candidates else None


def _latest_market_report(market: str) -> Optional[Dict[str, str]]:
    from src.scheduler import REPORT_DIR

    files = sorted(
        REPORT_DIR.glob(f"*-{market}-*.md"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if REPORT_DIR.exists() else []
    if not files:
        return None
    path = files[0]
    try:
        prefix = path.read_text(encoding="utf-8")[:800]
    except OSError:
        prefix = ""
    generated = re.search(r"生成时间：([^\s|]+)", prefix)
    label = path.stem.rsplit("-", 1)[-1]
    if re.fullmatch(r"\d{4}", label):
        label = label[:2] + ":" + label[2:]
    return {
        "file": path.name,
        "label": label,
        "generated_at": generated.group(1) if generated else datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
    }


def _build_market_status_answer(user_text: str, *, now: Optional[datetime] = None) -> Optional[str]:
    market = _market_status_request(user_text)
    if market is None:
        return None

    from src.trading.control import load_state
    from src.trading.controller import autonomous_enabled

    names = {"cn": "A 股", "hk": "港股", "us": "美股", "etf": "ETF"}
    current = _status_now(now)
    market_config = cfg.market_config(market)
    sessions = list(market_config.get("trading", {}).get("session", []) or [])
    session_active = _session_is_active(market, current, sessions)
    rounds = [str(value) for value in (cfg.intraday_times(market) or [])]
    next_round = _next_scheduled_time(market, rounds, current)
    close_time = str(cfg.close_time(market) or "")
    next_close = _next_scheduled_time(market, [close_time], current) if close_time else None
    latest_report = _latest_market_report(market)
    control = load_state()
    market_enabled = market in cfg.enabled_markets
    ai_enabled = autonomous_enabled()
    auto_execute = bool(cfg.autonomous.get("auto_execute", False))

    lines = [
        f"## 📊 {names[market]}实时运行状态",
        "",
        f"- **当前北京时间**：{current.strftime('%Y-%m-%d %H:%M:%S')}（{current.tzname() or 'Asia/Shanghai'}）",
        f"- **配置交易时段**：{_format_session(sessions)}",
        f"- **当前是否在交易时段**：{'是' if session_active else '否'}",
        f"- **市场开关**：{'已启用' if market_enabled else '已停用'}",
    ]
    if latest_report:
        lines.append(
            f"- **最近实际轮次**：{latest_report['label']} 已完成"
            f"（{latest_report['generated_at']}，{latest_report['file']}）"
        )
    else:
        lines.append("- **最近实际轮次**：尚未找到报告文件")
    lines.extend([
        f"- **下一构建轮次**：{next_round.strftime('%Y-%m-%d %H:%M:%S') if next_round else '未配置'}",
        f"- **下一收盘轮次**：{next_close.strftime('%Y-%m-%d %H:%M:%S') if next_close else '未配置'}",
        f"- **自主交易控制**：{'紧急停止' if control.get('kill_switch') else '已暂停' if control.get('paused') else '运行中'}",
    ])
    if control.get("reason"):
        lines.append(f"- **控制原因**：{control['reason']}")

    lines.extend(["", "### 结论", ""])
    if not market_enabled:
        lines.append(f"{names[market]}当前未启用，不会运行分析或提交订单。")
    elif control.get("kill_switch"):
        lines.append(f"{names[market]}调度信息可读取，但紧急停止开关已生效，不会提交订单。")
    elif control.get("paused"):
        detail = f"最近 {latest_report['label']} 轮次已经完成" if latest_report else "当前没有已完成轮次记录"
        lines.append(
            f"{names[market]}{'已经进入' if session_active else '当前不在'}配置交易时段，{detail}；"
            "**但自主交易处于暂停状态，因此不会提交任何模拟订单。**"
        )
    elif not ai_enabled:
        lines.append(f"{names[market]}调度可运行，但 AI 自主交易总开关未启用，不会自动下单。")
    elif not auto_execute:
        lines.append(f"{names[market]}会生成 AI 决策，但自动执行已关闭，不会提交模拟订单。")
    elif session_active:
        lines.append(f"{names[market]}已经进入交易时段，自主模拟交易处于运行状态。")
    else:
        lines.append(f"{names[market]}尚未进入配置交易时段，将等待下一轮自动触发。")
    return "\n".join(lines)


def _build_optimizer_request(user_text: str) -> Optional[Dict[str, Any]]:
    subject = r"优化器|组合优化|权重优化|风险平价|马科维茨|Black.?Litterman"
    if not re.search(subject, user_text, re.I):
        return None
    if re.search(r"(?:不要|别|无需|禁止|停止|取消).{0,8}(?:" + subject + r")", user_text, re.I):
        return None
    execution_intent = re.search(r"运行|执行|启动|开始|算一下|计算|给出.*权重|生成.*方案|优化一下|做一次|跑一次", user_text, re.I)
    if not execution_intent:
        return None

    market = "cn"
    if re.search(r"港股|HK", user_text, re.I):
        market = "hk"
    elif re.search(r"美股|US", user_text, re.I):
        market = "us"
    elif re.search(r"ETF", user_text, re.I):
        market = "etf"

    symbols: List[str] = []
    pattern = r"(?<![A-Za-z0-9])(?:sh|sz|bj)?\d{6}(?!\d)|(?<![A-Za-z0-9])hk\d{5}(?!\d)|(?<!\d)\d{5}(?!\d)|(?<![A-Za-z])[A-Z]{2,5}(?![A-Za-z])"
    for match in re.finditer(pattern, user_text, re.I):
        value = match.group(0)
        if value.upper() in {"ETF", "BLACK", "US", "CN", "HK"}:
            continue
        if value not in symbols:
            symbols.append(value)
    return {"market": market, "symbols": symbols or None}


def _format_optimizer_result(result: Dict[str, Any]) -> str:
    if result.get("error"):
        return f"## ❌ 优化器运行失败\n\n{result['error']}"

    recommended = result.get("recommended_scheme", "")
    schemes = result.get("schemes", {})
    labels = {
        "mean_variance": "均值-方差",
        "black_litterman": "Black-Litterman",
        "risk_parity": "风险平价",
        "cost_adjusted": "成本约束",
    }
    lines = [
        "## ✅ 组合优化已完成",
        "",
        f"- 市场：`{str(result.get('market', '')).upper()}`",
        f"- 标的：{', '.join(result.get('symbols') or [])}",
        f"- 有效样本：{result.get('observations', 0)} 个交易日",
        f"- 推荐方案：**{labels.get(recommended, recommended)}**",
        f"- 结果文件：`{result.get('output_file', '')}`",
        "",
        "| 方案 | 风险袖年化收益 | 组合净年化 | 年化波动 | 净夏普 | VaR95 | 最大回撤 |",
        "|---|---:|---:|---:|---:|---:|---:|",
    ]
    for name, item in schemes.items():
        metrics = item.get("metrics", {})
        stress = item.get("stress", {})
        net_metrics = item.get("net_metrics", {})
        lines.append(
            f"| {labels.get(name, name)} | {metrics.get('annual_return', 0) * 100:.2f}% | "
            f"{net_metrics.get('net_annual_return', 0) * 100:.2f}% | {net_metrics.get('annual_volatility', 0) * 100:.2f}% | "
            f"{net_metrics.get('net_sharpe', 0):.3f} | {stress.get('var95', 0) * 100:.2f}% | "
            f"{stress.get('max_drawdown', 0) * 100:.2f}% |"
        )
    if recommended in schemes:
        lines.extend(["", "### 推荐权重", ""])
        for symbol, weight in schemes[recommended].get("portfolio_weights", {}).items():
            lines.append(f"- `{symbol}`：{weight * 100:.2f}%")
    dropped = result.get("dropped_symbols") or {}
    if dropped:
        lines.extend(["", f"> ⚠️ 已跳过：{json.dumps(dropped, ensure_ascii=False)}"])
    lines.extend(["", "> 结果基于历史数据与模拟约束，不构成投资建议。"])
    return "\n".join(lines)


def _looks_like_stock_analysis(user_text: str) -> bool:
    text = user_text.strip()
    if not text:
        return False
    if re.search(r"(?<![A-Za-z0-9])(?:sh|sz|bj)?\d{6}(?!\d)|(?<![A-Za-z0-9])hk\d{5}(?!\d)|(?<![A-Za-z0-9])us[A-Za-z.]{1,10}\b|(?<!\d)\d{5}(?!\d)", text, re.I):
        return True
    if re.search(r"(?<![A-Za-z])(?:[A-Z]{2,5})(?![A-Za-z])", text):
        return True
    # Broad market/configuration requests belong to the normal assistant, not
    # the single-security snapshot route.
    if re.search(r"A\s*股(?:市场|大盘|整体|行情)|(?:项目)?代码|配置|文件|风控|优化器|数据源", text, re.I):
        return False
    if re.search(r"茅台|腾讯(?:控股)?|阿里(?:巴巴)?|宁德时代|中国平安|招商银行|五粮液", text):
        return True
    return bool(re.search(r"技术面|基本面|个股|股票|股价|股份|K线|k线|走势", text))


def _extract_stock_query(user_text: str) -> str:
    code = re.search(r"(?<![A-Za-z0-9])(?:sh|sz|bj)?\d{6}(?!\d)|(?<![A-Za-z0-9])hk\d{5}(?!\d)|(?<![A-Za-z0-9])us[A-Za-z.]{1,10}\b|(?<!\d)\d{5}(?!\d)", user_text, re.I)
    if code:
        return code.group(0)
    ticker = re.search(r"(?<![A-Za-z])(?:[A-Z]{2,5})(?![A-Za-z])", user_text)
    if ticker:
        return ticker.group(0)
    cleaned = re.sub(r"(请|帮我|麻烦|分析一下|分析下|分析|看一下|看看|一下|股票|个股|技术面|基本面|走势|的|怎么样|如何|今天|现在)", "", user_text)
    cleaned = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]+", "", cleaned).strip()
    return cleaned or user_text.strip()


def _build_stock_analysis_context(user_text: str) -> Optional[Dict[str, Any]]:
    if not _looks_like_stock_analysis(user_text):
        return None
    query = _extract_stock_query(user_text)
    if not query:
        return None
    search_result: Any = None
    code = query
    if not re.search(r"\d{5,6}|^[A-Za-z.]{1,10}$", query):
        search_result = _stock_fetcher("search", query)
        first: Any = None
        if isinstance(search_result, list) and search_result:
            first = search_result[0]
        elif isinstance(search_result, dict) and search_result.get("stocks"):
            first = search_result["stocks"][0]
        if isinstance(first, dict):
            code = first.get("symbol") or first.get("code") or query
        else:
            # Never pass an unresolved Chinese phrase to snapshot: the fetcher
            # would treat it as a code and turn a failed search into noise.
            return None
    snapshot = _stock_fetcher("snapshot", code)
    if isinstance(snapshot, dict) and snapshot.get("error"):
        return None
    return {"query": query, "resolved_code": code, "search": search_result, "snapshot": snapshot}

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
    if not isinstance(data, dict):
        return None
    name = data.get("tool") or data.get("tool_name") or data.get("name")
    if name not in TOOLS:
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


def _strip_tool_blocks(text: str) -> str:
    text = re.sub(r"```tool\s*\n.*?\n\s*```", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
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
