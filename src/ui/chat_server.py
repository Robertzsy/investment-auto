"""
AI Chat backend for investment-auto.

Features:
- Persistent conversation history and memory
- Deterministic routes for controls, market state, stocks, and optimization
- Typed Pydantic AI tools for open-ended Agent conversations
- Streaming-friendly generator for SSE endpoint
- Per-Agent least-privilege tool access
"""

from __future__ import annotations

import json
import logging
import queue
import re
import subprocess
import threading
import uuid
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Generator, List, Mapping, Optional
from zoneinfo import ZoneInfo

from src.config import cfg
from src.subprocess_utils import decode_subprocess_output

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

# ── deterministic helpers (not exposed as Agent tools) ───────────────
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


_LEGACY_TOOL_NAMES = {
    "run_shell", "read_file", "write_file", "list_dir",
    "stock_search", "stock_snapshot", "optimizer",
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
            from src.scheduler import run_investment_cycle

            result = run_investment_cycle(
                market,
                label=label,
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
    final_answer = _format_full_cycle_result(result) if result else f"## ❌ 完整投资轮次失败\n\n{error or '未知错误'}"
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

    agent_limit_answer = _build_agent_limit_answer(message)
    if agent_limit_answer is not None:
        yield {"type": "tool", "name": "agent_limits", "params": {}}
        append_history("assistant", agent_limit_answer)
        yield {"type": "token", "content": agent_limit_answer}
        yield {"type": "final", "content": agent_limit_answer}
        return

    autonomy_control_answer = _handle_autonomy_control_command(message)
    if autonomy_control_answer is not None:
        yield {"type": "tool", "name": "autonomy_control", "params": {}}
        emitted = ""
        for chunk in _chunk_text(autonomy_control_answer, 18):
            if cancel_event.is_set():
                yield from _emit_cancelled(emitted)
                return
            emitted += chunk
            yield {"type": "token", "content": chunk}
        append_history("assistant", autonomy_control_answer)
        yield {"type": "final", "content": autonomy_control_answer}
        return

    autonomy_status_answer = _build_autonomy_status_answer(message)
    if autonomy_status_answer is not None:
        yield {"type": "tool", "name": "autonomy_status", "params": {}}
        emitted = ""
        for chunk in _chunk_text(autonomy_status_answer, 18):
            if cancel_event.is_set():
                yield from _emit_cancelled(emitted)
                return
            emitted += chunk
            yield {"type": "token", "content": chunk}
        append_history("assistant", autonomy_status_answer)
        yield {"type": "final", "content": autonomy_status_answer}
        return

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

    stock_context = _build_stock_analysis_context(message)
    if stock_context:
        from src.llm.registry import resolve_llm

        llm = resolve_llm(provider=provider, model=model) if provider else resolve_llm(role="chat")
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


def _named_market(user_text: str) -> Optional[str]:
    if re.search(r"美股|美国股市|(?<![A-Za-z])US(?![A-Za-z])", user_text, re.I):
        return "us"
    if re.search(r"港股|香港股市|(?<![A-Za-z])HK(?![A-Za-z])", user_text, re.I):
        return "hk"
    if re.search(r"ETF|场内基金", user_text, re.I):
        return "etf"
    if re.search(r"A股|沪深|中国股市|(?<![A-Za-z])CN(?![A-Za-z])", user_text, re.I):
        return "cn"
    return None


def _build_agent_limit_answer(user_text: str) -> Optional[str]:
    text = re.sub(r"\s+", "", str(user_text or ""))
    if not re.search(r"(?:工具|请求|调用).{0,8}(?:上限|限制)|(?:上限|限制).{0,8}(?:工具|请求|调用)", text):
        return None
    from src.ui.agent_runtime import _REQUEST_LIMIT, _TOOL_CALL_LIMIT, _TOTAL_TOKEN_LIMIT

    return "\n".join([
        "## 当前通用对话 Agent 上限",
        "",
        f"- **模型请求**：每轮最多 {_REQUEST_LIMIT} 次",
        f"- **工具调用**：每轮最多 {_TOOL_CALL_LIMIT} 次",
        f"- **总 Token**：每轮最多 {_TOTAL_TOKEN_LIMIT:,}",
        "",
        "这些限制用于阻止通用对话陷入工具循环。完整投资轮次走独立的确定性入口，"
        "会自行完成选股、候选与持仓分析、组合决策、硬风控、模拟下单和报告，不受这 8 次工具调用上限约束。",
    ])


def _format_full_cycle_result(result: Mapping[str, Any]) -> str:
    names = {"cn": "A 股", "hk": "港股", "us": "美股", "etf": "ETF"}
    market = str(result.get("market", ""))
    status = str(result.get("status", "error"))
    autonomous = result.get("autonomous", {}) if isinstance(result.get("autonomous"), Mapping) else {}
    autonomous_status = str(autonomous.get("status", ""))
    fills = autonomous.get("fills", []) if isinstance(autonomous, Mapping) else []
    notification = result.get("notification", {}) if isinstance(result.get("notification"), Mapping) else {}
    completed = status == "generated" and autonomous_status in {"executed", "no_trade"}
    icon = "✅" if completed or status == "exists" else "⚠️"
    lines = [f"## {icon} {names.get(market, market.upper())}完整投资轮次", ""]
    lines.extend(["本次已按一条完整链执行：全市场选股 → 候选与持仓分析 → 组合决策 → 硬风控 → 模拟撮合 → 最终报告。", ""])
    if status == "generated":
        outcome_labels = {
            "executed": "分析和风控完成，已产生模拟成交",
            "no_trade": "分析和风控完成，本轮决定观望或没有订单通过风控",
            "paused": "运行时安全暂停，本轮未分析和下单",
            "disabled": "自主模块未启用，本轮未分析和下单",
            "blocked": "分析或交易前置条件不满足，本轮未下单",
            "skipped": "本轮按配置跳过交易",
            "in_progress": "同市场已有一轮正在执行，本轮未重复启动",
            "error": "分析链执行失败，本轮未下单",
        }
        lines.append(f"- **投资执行状态**：{outcome_labels.get(autonomous_status, autonomous_status or '未知')}")
        block_reason = autonomous.get("error") or autonomous.get("reason")
        control = autonomous.get("control") if isinstance(autonomous.get("control"), Mapping) else {}
        if not block_reason and autonomous_status == "paused":
            block_reason = control.get("reason")
        if block_reason:
            lines.append(f"- **原因**：{block_reason}")
        lines.append(f"- **模拟成交**：{len(fills)} 笔")
        lines.append(f"- **报告文件**：`{result.get('report') or '-'}`")
        notify_status = str(notification.get("status", "disabled"))
        labels = {"delivered": "已发送", "disabled": "通知未启用", "skipped": "通知未配置", "error": "发送失败"}
        lines.append(f"- **报告推送**：{labels.get(notify_status, notify_status)}")
        if notification.get("reason"):
            lines.append(f"- **推送说明**：{notification['reason']}")
        report_path = Path(str(result.get("report", "")))
        if report_path.is_file() and report_path.parent.resolve() == (PROJECT_ROOT / "runtime" / "reports").resolve():
            try:
                lines.extend(["", "---", "", report_path.read_text(encoding="utf-8")[:12000]])
            except OSError:
                pass
    elif status in {"exists", "in_progress"}:
        lines.append("本轮报告已存在或同市场整轮正在执行，没有重复启动。")
    else:
        lines.append(f"整轮未完成：{result.get('error') or result.get('reason') or status}")
    lines.extend(["", "本轮不需要逐步确认；全部买入、持有、观望和卖出判断均记录在最终报告中。"])
    return "\n".join(lines)


def _autonomy_control_request(user_text: str) -> Optional[Dict[str, Any]]:
    """Recognize only imperative pause/resume requests, never status questions."""
    text = re.sub(r"\s+", "", str(user_text or ""))
    if not text or re.search(r"吗|么|是否|有没有|什么时候|何时|状态|怎么|如何|为什么|为何", text):
        return None
    if not re.search(r"交易|下单|自主|自动|全局暂停", text, re.I):
        return None

    explicit_resume = bool(
        re.search(r"解除.{0,8}暂停", text)
        or re.search(r"(?:恢复|继续).{0,20}(?:自主|自动|交易|下单)", text)
        or re.search(r"(?:自主|自动|交易|下单).{0,12}(?:恢复|继续)", text)
    )
    explicit_pause = bool(
        re.search(r"(?:暂停|停止|关闭).{0,20}(?:交易|下单)", text)
        or re.search(r"(?:交易|下单).{0,12}(?:暂停|停止|关闭)", text)
    )
    if explicit_resume:
        action = "resume"
    elif explicit_pause:
        action = "pause"
    else:
        return None

    market = _named_market(text)
    global_scope = bool(
        re.search(r"全局|全部|所有市场|全部市场", text)
        or re.search(r"自主(?:模拟)?交易|自动(?:模拟)?交易", text)
    )
    return {"action": action, "market": market, "global_scope": global_scope}


def _handle_autonomy_control_command(user_text: str) -> Optional[str]:
    """Apply explicit global control commands without sending the LLM on a tool loop."""
    request = _autonomy_control_request(user_text)
    if request is None:
        return None

    names = {"cn": "A 股", "hk": "港股", "us": "美股", "etf": "ETF"}
    market = request.get("market")
    if market and not request.get("global_scope"):
        action_name = "恢复" if request["action"] == "resume" else "暂停"
        return "\n".join([
            "## ⚠️ 尚未执行",
            "",
            f"你要求{action_name}{names[market]}交易，但当前运行时暂停锁是**全局控制**，不能只对单个市场解除或设置。",
            f"直接执行会同时影响所有已启用市场，因此我没有擅自扩大命令范围。",
            "",
            f"如果确认{action_name}所有已启用市场，请发送：",
            "",
            f"> {'解除全局暂停并恢复自主模拟交易' if request['action'] == 'resume' else '全局暂停自主模拟交易'}",
        ])
    if not request.get("global_scope"):
        return "\n".join([
            "## ⚠️ 尚未执行",
            "",
            "这条命令没有明确作用范围。当前暂停锁是全局控制，我没有修改运行状态。",
            "",
            f"请明确发送：`{'解除全局暂停并恢复自主模拟交易' if request['action'] == 'resume' else '全局暂停自主模拟交易'}`",
        ])

    from src.trading.control import load_state, set_paused
    from src.trading.controller import autonomous_enabled

    enabled_markets = [names.get(value, str(value).upper()) for value in cfg.enabled_markets]
    enabled_label = "、".join(enabled_markets) or "无"
    state = load_state()
    if request["action"] == "pause":
        if state.get("paused"):
            return f"## ⏸️ 已处于全局暂停状态\n\n运行状态未变化。已启用市场：{enabled_label}。"
        state = set_paused(True, reason="通过 AI 对话人工全局暂停自主模拟交易", updated_by="human")
        return "\n".join([
            "## ⏸️ 已全局暂停自主模拟交易",
            "",
            f"受影响的已启用市场：{enabled_label}。后续调度仍可生成报告，但不会提交模拟订单。",
            f"更新时间：{state.get('updated_at') or '刚刚'}",
        ])

    if str(cfg.trading.get("mode", "paper")).lower() != "paper":
        return "## ⛔ 未恢复\n\n当前不是 `paper` 模式。为避免触发真实交易，运行状态未修改。"
    if not autonomous_enabled(cfg.autonomous):
        return "## ⛔ 未恢复\n\n配置或环境变量中的 AI 自主交易总开关未启用，运行状态未修改。"
    if state.get("kill_switch"):
        return "## ⛔ 未恢复\n\n紧急停止开关仍处于激活状态。请先在设置中解除紧急停止，再重新确认恢复。"
    if not state.get("paused"):
        execution_note = "自动执行已开启" if cfg.autonomous.get("auto_execute", False) else "自动执行仍关闭"
        return f"## ▶️ 自主模拟交易已在运行\n\n无需重复解除暂停。已启用市场：{enabled_label}；{execution_note}。"

    state = set_paused(
        False,
        reason="通过 AI 对话人工确认解除全局暂停并恢复自主模拟交易",
        updated_by="human",
    )
    execution_note = (
        "系统将在后续轮次按风控规则自主提交模拟订单。"
        if cfg.autonomous.get("auto_execute", False)
        else "但自动执行配置仍关闭，只会生成决策而不会提交模拟订单。"
    )
    return "\n".join([
        "## ▶️ 已解除全局暂停",
        "",
        f"已恢复的市场：{enabled_label}。{execution_note}",
        f"更新时间：{state.get('updated_at') or '刚刚'}",
    ])


def _build_autonomy_status_answer(user_text: str) -> Optional[str]:
    """Answer autonomy switch/control questions without an LLM round trip."""

    text = re.sub(r"\s+", "", str(user_text or ""))
    if not re.search(r"自主(?:模拟)?交易|自动(?:模拟)?交易|AI(?:自主)?交易", text, re.I):
        return None
    if not re.search(r"状态|开关|暂停|运行|开启|启用|打开|为什么|为何|是否|有没有|吗|么", text):
        return None

    from src.trading.control import load_state
    from src.trading.controller import autonomous_enabled

    state = load_state()
    configured = bool(cfg.autonomous.get("enabled", False))
    effective = autonomous_enabled(cfg.autonomous)
    auto_execute = bool(cfg.autonomous.get("auto_execute", False))
    paper_mode = str(cfg.trading.get("mode", "paper")).lower() == "paper"
    names = {"cn": "A 股", "hk": "港股", "us": "美股", "etf": "ETF"}
    markets = "、".join(names.get(value, str(value).upper()) for value in cfg.enabled_markets) or "无"

    if not paper_mode:
        conclusion = "当前不是 `paper` 模式，系统会拒绝自主模拟下单。"
    elif not effective:
        conclusion = "自主交易有效总开关未启用，不会提交模拟订单。"
    elif state.get("kill_switch"):
        conclusion = "紧急停止开关已触发；调度可继续生成报告，但不会提交模拟订单。"
    elif state.get("paused"):
        conclusion = "配置开关已经打开，但运行时暂停锁仍开启；调度和报告继续运行，模拟订单不会提交。"
    elif not auto_execute:
        conclusion = "自主决策已启用，但自动执行关闭；系统只生成决策，不提交模拟订单。"
    else:
        conclusion = "自主模拟交易处于可执行状态，后续轮次仍需通过全部硬风控才能提交订单。"

    return "\n".join([
        "## 🤖 自主模拟交易状态",
        "",
        f"- **配置开关**：{'已打开' if configured else '未打开'}",
        f"- **有效总开关**：{'已启用' if effective else '未启用'}",
        f"- **自动执行**：{'已打开' if auto_execute else '未打开'}",
        f"- **运行时暂停**：{'是' if state.get('paused') else '否'}",
        f"- **紧急停止**：{'已触发' if state.get('kill_switch') else '未触发'}",
        f"- **交易模式**：`{str(cfg.trading.get('mode', 'paper'))}`",
        f"- **已启用市场**：{markets}",
        f"- **控制原因**：{state.get('reason') or '无'}",
        "",
        f"**结论**：{conclusion}",
    ])


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
    utc_offset = current.strftime("%z")
    if len(utc_offset) == 5:
        utc_offset = utc_offset[:3] + ":" + utc_offset[3:]

    lines = [
        f"## 📊 {names[market]}实时运行状态",
        "",
        f"- **当前北京时间**：{current.strftime('%Y-%m-%d %H:%M:%S')}（Asia/Shanghai，UTC{utc_offset}）",
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


def _strip_tool_blocks(text: str) -> str:
    text = re.sub(r"```tool\s*\n.*?\n\s*```", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"<｜｜DSML｜｜tool_calls>.*?</｜｜DSML｜｜tool_calls>", "", text, flags=re.DOTALL).strip()
    text = re.sub(r"<tool_call>.*?</tool_call>", "", text, flags=re.DOTALL | re.IGNORECASE).strip()
    return text


def _chunk_text(text: str, size: int = 20) -> Generator[str, None, None]:
    for i in range(0, len(text), size):
        yield text[i:i+size]
