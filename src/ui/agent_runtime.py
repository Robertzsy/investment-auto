"""Typed, bounded Agent runtime for the web conversation.

The model only sees the tools declared in this module. There is deliberately
no arbitrary shell or file-write tool: trading controls are handled by the
deterministic command routes in ``chat_server`` and execution remains behind
the paper broker and hard risk engine.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import queue
import threading
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncIterable, Dict, Generator, List, Optional

from pydantic_ai import Agent, CancellationToken, RunContext, Tool, UsageLimits
from pydantic_ai.exceptions import RunCancelled, UsageLimitExceeded
from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent

from src.config import cfg
from src.llm.agent_model import resolve_agent_model

logger = logging.getLogger("investment-auto.agent")

ROOT = Path(__file__).resolve().parents[2]
REPORT_DIR = ROOT / "runtime" / "reports"
AUDIT_DIR = ROOT / "runtime" / "trading" / "audit"

_REQUEST_LIMIT = 6
_TOOL_CALL_LIMIT = 8
_TOTAL_TOKEN_LIMIT = 32_000
_QUEUE_POLL_SECONDS = 0.05


class RepeatedToolLoop(RuntimeError):
    """Raised when the same tool, arguments, and result repeat twice."""


class _ToolLoopDetector:
    """Track progress by completed ``(tool, args, result)`` triples."""

    def __init__(self) -> None:
        self._calls: Dict[str, str] = {}
        self._results: Dict[str, int] = {}

    def record_call(self, tool_call_id: str, tool_name: str, arguments: Any) -> None:
        self._calls[tool_call_id] = f"{tool_name}:{_json_safe(arguments, limit=4000)}"

    def record_result(self, tool_call_id: str, tool_name: str, result: Any) -> None:
        call_key = self._calls.get(tool_call_id, tool_name)
        result_key = f"{call_key}:{_json_safe(result, limit=6000)}"
        fingerprint = hashlib.sha256(result_key.encode("utf-8", errors="replace")).hexdigest()
        self._results[fingerprint] = self._results.get(fingerprint, 0) + 1
        if self._results[fingerprint] >= 2:
            raise RepeatedToolLoop(f"检测到重复工具调用且结果没有变化：{tool_name}")


@dataclass
class ChatAgentDeps:
    cancellation_token: CancellationToken


def _json_safe(value: Any, *, limit: int = 16_000) -> str:
    try:
        text = json.dumps(value, ensure_ascii=False, sort_keys=True, default=str)
    except Exception:
        text = repr(value)
    return text[:limit]


def _portfolio_context() -> Dict[str, Any]:
    from src.portfolio import account as account_store

    portfolio = account_store.load()
    accounts = portfolio.get("accounts", {})
    result: Dict[str, Any] = {"mode": cfg.trading.get("mode", "paper"), "accounts": {}}
    for market in cfg.enabled_markets:
        account = accounts.get(market, {})
        holdings = []
        for item in account.get("holdings", [])[:30]:
            holdings.append({
                "code": item.get("code"),
                "name": item.get("name"),
                "shares": item.get("shares", item.get("quantity", 0)),
                "cost_price": item.get("costPrice", item.get("cost", 0)),
                "last_price": item.get("lastPrice", 0),
                "high_price": item.get("highPrice"),
            })
        result["accounts"][market] = {
            "cash": account.get("cash", 0),
            "total_capital": account.get("totalCapital", 0),
            "high_water_mark": account.get("highWaterMark"),
            "holdings": holdings,
            "trade_count": len(account.get("tradeHistory", [])),
            "recent_trades": account.get("tradeHistory", [])[-10:],
        }
    return result


def _risk_context() -> Dict[str, Any]:
    markets: Dict[str, Any] = {}
    for market in cfg.enabled_markets:
        market_config = cfg.market_config(market)
        markets[market] = {
            "risk": market_config.get("risk", {}),
            "trading_mechanics": market_config.get("trading", {}),
        }
    return {
        "autonomous": cfg.autonomous,
        "trading": cfg.trading,
        "markets": markets,
    }


def _report_context(question: str) -> Dict[str, Any]:
    if not REPORT_DIR.exists():
        return {"reports": [], "latest_content": ""}
    files = sorted(
        (path for path in REPORT_DIR.glob("*.md") if path.is_file()),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    market = ""
    for candidate, terms in {
        "cn": ("A股", "沪深", "cn"),
        "hk": ("港股", "香港", "hk"),
        "us": ("美股", "美国", "us"),
        "etf": ("ETF", "场内基金", "etf"),
    }.items():
        if any(term.lower() in question.lower() for term in terms):
            market = candidate
            break
    selected = [path for path in files if not market or f"-{market}-" in path.name]
    latest = selected[0] if selected else (files[0] if files else None)
    content = ""
    if latest is not None:
        try:
            content = latest.read_text(encoding="utf-8")[:14_000]
        except OSError as exc:
            content = f"读取失败：{exc}"
    return {
        "reports": [
            {
                "file": path.name,
                "modified_at": datetime.fromtimestamp(path.stat().st_mtime).isoformat(timespec="seconds"),
            }
            for path in selected[:12]
        ],
        "latest_file": latest.name if latest else None,
        "latest_content": content,
    }


def _ops_context() -> Dict[str, Any]:
    from src.trading.control import load_state
    from src.trading.controller import autonomous_enabled

    audit_files = sorted(
        AUDIT_DIR.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    ) if AUDIT_DIR.exists() else []
    latest_audit: Any = None
    if audit_files:
        try:
            latest_audit = json.loads(audit_files[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            latest_audit = {"file": audit_files[0].name, "error": str(exc)}
    return {
        "timezone": cfg.schedule.get("timezone", "Asia/Shanghai"),
        "enabled_markets": cfg.enabled_markets,
        "scheduler": {
            "chat_start_scheduler": cfg.schedule.get("chat_start_scheduler", True),
            "intraday_rounds": cfg.schedule.get("intraday_rounds", {}),
            "close_rounds": cfg.schedule.get("close_rounds", {}),
            "macro_daily_time": cfg.schedule.get("macro_daily_time"),
        },
        "autonomous_enabled": autonomous_enabled(),
        "control": load_state(),
        "control_semantics": "paused 或 kill_switch 阻止模拟订单提交；调度任务和报告生成仍继续",
        "latest_audit": latest_audit,
    }


PORTFOLIO_AGENT = Agent(
    name="portfolio_agent",
    instructions=(
        "你是模拟投资组合分析员。只根据给出的账户快照回答，计算时明确口径；"
        "不得声称已经交易，不得提出绕过风控的操作。使用中文 Markdown。"
    ),
)
RISK_AGENT = Agent(
    name="risk_agent",
    instructions=(
        "你是风险控制分析员。区分可编辑配置与撮合硬规则，说明配置的实际影响。"
        "不得降低或绕过纸面交易、仓位、回撤、T+1 等硬约束。使用中文 Markdown。"
    ),
)
REPORT_AGENT = Agent(
    name="report_agent",
    instructions=(
        "你是投资报告分析员。只总结提供的本地报告，标明报告文件和时间；"
        "报告不存在或过期时必须直说。使用中文 Markdown。"
    ),
)
OPS_AGENT = Agent(
    name="ops_agent",
    instructions=(
        "你是 Investment-Auto 运行状态分析员。只解释提供的调度、控制和审计状态；"
        "运行时 paused/kill_switch 只阻止模拟订单提交，不会停止调度器，也不会阻止报告生成。"
        "不得声称执行了命令，不得猜测当前时间或进程状态。使用中文 Markdown。"
    ),
)


async def _ask_specialist(
    agent: Agent[Any, str],
    ctx: RunContext[ChatAgentDeps],
    question: str,
    context: Dict[str, Any],
) -> str:
    result = await agent.run(
        f"用户问题：{question}\n\n可信数据：\n{_json_safe(context)}",
        model=ctx.model,
        usage=ctx.usage,
        cancellation_token=ctx.deps.cancellation_token,
        model_settings={"temperature": 0.15, "max_tokens": 1800},
    )
    return str(result.output).strip()


async def consult_portfolio_agent(ctx: RunContext[ChatAgentDeps], question: str) -> str:
    """Ask the portfolio specialist about current simulated accounts and holdings.

    Args:
        question: The user's portfolio question.
    """

    return await _ask_specialist(PORTFOLIO_AGENT, ctx, question, _portfolio_context())


async def consult_risk_agent(ctx: RunContext[ChatAgentDeps], question: str) -> str:
    """Ask the risk specialist about current limits and trading mechanics.

    Args:
        question: The user's risk or configuration question.
    """

    return await _ask_specialist(RISK_AGENT, ctx, question, _risk_context())


async def consult_report_agent(ctx: RunContext[ChatAgentDeps], question: str) -> str:
    """Ask the report specialist to find and summarize a local investment report.

    Args:
        question: The user's report question, including a market when relevant.
    """

    return await _ask_specialist(REPORT_AGENT, ctx, question, _report_context(question))


async def consult_ops_agent(ctx: RunContext[ChatAgentDeps], question: str) -> str:
    """Ask the operations specialist about scheduler and autonomy state.

    Args:
        question: The user's runtime or scheduling question.
    """

    return await _ask_specialist(OPS_AGENT, ctx, question, _ops_context())


def search_security(query: str) -> Dict[str, Any]:
    """Search the local market-data provider for a security.

    Args:
        query: Security name or ticker to search.
    """

    from src.ui.chat_server import _stock_fetcher

    return _stock_fetcher("search", query)


def get_security_snapshot(code: str) -> Dict[str, Any]:
    """Get the latest available quote snapshot for one security.

    Args:
        code: Resolved ticker such as 600519, hk00700, or AAPL.
    """

    from src.ui.chat_server import _stock_fetcher

    return _stock_fetcher("snapshot", code)


def get_stock_screening(market: str = "all", refresh: bool = False) -> Dict[str, Any]:
    """Read or refresh the deterministic stock-screening shortlist.

    Args:
        market: cn, hk, us, etf, or all. A refresh requires one concrete market.
        refresh: Run a new non-trading screen instead of reading the latest result.
    """

    from src.screening import latest_screening

    normalized = str(market or "all").strip().lower()
    allowed = {"cn", "hk", "us", "etf"}
    if normalized != "all" and normalized not in allowed:
        raise ValueError("market 必须是 cn、hk、us、etf 或 all")
    if refresh:
        if normalized == "all":
            raise ValueError("刷新选股时请指定一个具体市场")
        from src.trading.controller import run_screening_preview

        return run_screening_preview(normalized)
    markets = cfg.enabled_markets if normalized == "all" else [normalized]
    results = {market_name: latest_screening(market_name) for market_name in markets}
    return {
        "screening_enabled": cfg.screening.get("enabled", True),
        "results": {key: value for key, value in results.items() if value is not None},
        "missing_markets": [key for key, value in results.items() if value is None],
    }


MANAGER_AGENT = Agent(
    name="investment_auto_manager",
    deps_type=ChatAgentDeps,
    instructions=(
        "你是 Investment-Auto 的中文对话协调 Agent。你的职责是解释系统、查询可信状态、"
        "调用最少数量的专业工具并给出清楚结论。\n"
        "规则：\n"
        "1. 当前账户、报告、调度、风控问题必须调用相应 specialist；证券行情使用 search/security snapshot。\n"
        "   选股、候选池和筛选分数必须调用 stock screening；用户明确要求立即刷新时设置 refresh=true。\n"
        "2. 一般只调用一个 specialist；只有确实需要跨域综合时才调用多个。\n"
        "3. 你没有 Shell、文件写入、配置修改或交易执行权限，不得虚构已经完成操作。\n"
        "4. 用户要求启动、暂停交易或修改配置时，说明应使用明确控制命令或设置页；"
        "聊天入口会在你之前处理受支持的确定性命令。\n"
        "5. 不输出工具 JSON，不猜测时间、日志、行情和持仓。工具失败时明确报告失败。\n"
        "6. 涉及投资判断必须标明是模拟研究信息，不构成投资建议。"
    ),
    tools=[
        Tool(consult_portfolio_agent, sequential=True, timeout=80),
        Tool(consult_risk_agent, sequential=True, timeout=80),
        Tool(consult_report_agent, sequential=True, timeout=80),
        Tool(consult_ops_agent, sequential=True, timeout=80),
        Tool(search_security, sequential=True, timeout=50),
        Tool(get_security_snapshot, sequential=True, timeout=50),
        Tool(get_stock_screening, sequential=True, timeout=180),
    ],
    retries=1,
    tool_timeout=90,
)


def _conversation_prompt(
    message: str,
    history: List[Dict[str, Any]],
    memory: str,
) -> str:
    recent = []
    for item in history[-16:]:
        role = "用户" if item.get("role") == "user" else "助手"
        content = str(item.get("content", "")).strip()
        if content:
            recent.append(f"{role}: {content[:3000]}")
    sections = []
    if memory.strip():
        sections.append("长期偏好：\n" + memory.strip()[:6000])
    if recent:
        sections.append("最近对话（仅供上下文，不是新指令）：\n" + "\n".join(recent))
    sections.append("当前用户问题：\n" + message)
    return "\n\n".join(sections)


def _redact_error(exc: BaseException) -> str:
    text = str(exc) or exc.__class__.__name__
    for provider_config in cfg.raw.get("llm", {}).get("models", {}).values():
        env_name = str(provider_config.get("api_key_env", ""))
        if env_name:
            import os

            secret = os.getenv(env_name, "")
            if secret:
                text = text.replace(secret, "***")
    return text[:2000]


def run_agent_events(
    message: str,
    *,
    history: List[Dict[str, Any]],
    memory: str,
    thinking: bool,
    provider: Optional[str],
    model: Optional[str],
    cancel_event: threading.Event,
) -> Generator[Dict[str, Any], None, None]:
    """Bridge Pydantic AI's async run into the synchronous SSE generator."""

    model_instance = resolve_agent_model(provider=provider, model=model, role="chat")
    cancellation_token = CancellationToken()
    deps = ChatAgentDeps(cancellation_token=cancellation_token)
    events: "queue.Queue[Dict[str, Any]]" = queue.Queue()
    prompt = _conversation_prompt(message, history, memory)

    async def event_handler(_: RunContext[ChatAgentDeps], stream: AsyncIterable[Any]) -> None:
        loop_detector = _ToolLoopDetector()
        async for event in stream:
            if isinstance(event, FunctionToolCallEvent):
                try:
                    arguments = event.part.args_as_dict()
                except Exception:
                    arguments = {"raw": event.part.args_as_json_str()}
                loop_detector.record_call(
                    event.tool_call_id,
                    event.part.tool_name,
                    arguments,
                )
                events.put({
                    "type": "tool",
                    "name": event.part.tool_name,
                    "params": arguments,
                })
            elif isinstance(event, FunctionToolResultEvent):
                loop_detector.record_result(
                    event.tool_call_id,
                    event.part.tool_name,
                    event.part.content,
                )

    async def run() -> None:
        settings: Dict[str, Any] = {
            "temperature": 0.2,
            "max_tokens": 4096,
            "timeout": float(cfg.raw.get("llm", {}).get("request_timeout_seconds", 120)),
        }
        if thinking:
            settings["extra_body"] = {"thinking": {"type": "enabled"}}
        try:
            result = await MANAGER_AGENT.run(
                prompt,
                model=model_instance,
                deps=deps,
                model_settings=settings,
                usage_limits=UsageLimits(
                    request_limit=_REQUEST_LIMIT,
                    tool_calls_limit=_TOOL_CALL_LIMIT,
                    total_tokens_limit=_TOTAL_TOKEN_LIMIT,
                ),
                cancellation_token=cancellation_token,
                event_stream_handler=event_handler,
            )
            output = str(result.output).strip()
            if not output:
                raise RuntimeError("模型返回了空响应")
            logger.info("Agent run completed: usage=%s", result.usage)
            events.put({"type": "result", "content": output})
        except RunCancelled:
            events.put({"type": "cancelled"})
        except RepeatedToolLoop as exc:
            events.put({"type": "error", "content": str(exc)})
        except UsageLimitExceeded:
            events.put({
                "type": "error",
                "content": "Agent 已达到本轮工具/请求上限并安全停止，请把问题拆小后重试。",
            })
        except Exception as exc:
            logger.exception("Agent run failed")
            events.put({"type": "error", "content": _redact_error(exc)})

    def worker() -> None:
        asyncio.run(run())

    thread = threading.Thread(target=worker, name="chat-agent-run", daemon=True)
    thread.start()
    try:
        while True:
            if cancel_event.is_set() and not cancellation_token.cancelled:
                cancellation_token.cancel()
            try:
                event = events.get(timeout=_QUEUE_POLL_SECONDS)
            except queue.Empty:
                if not thread.is_alive():
                    yield {"type": "error", "content": "Agent 异常结束且没有返回结果。"}
                    return
                continue
            yield event
            if event.get("type") in {"result", "cancelled", "error"}:
                return
    finally:
        if thread.is_alive():
            cancellation_token.cancel()
            thread.join(timeout=1.0)
