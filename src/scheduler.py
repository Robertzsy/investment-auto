from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler as _BgScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import cfg
from src.llm.registry import resolve_llm
from src.portfolio import account
from src.runtime_lock import ProcessLease, atomic_claim

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
REPORT_DIR = ROOT / "runtime" / "reports"
SCHEDULER_LOCK = ROOT / "runtime" / "scheduler.lock"


class _LockedScheduler(_BgScheduler):
    def __init__(self, *args: Any, process_lease: ProcessLease, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._process_lease = process_lease

    def shutdown(self, wait: bool = True) -> None:
        try:
            super().shutdown(wait=wait)
        finally:
            self._process_lease.release()


def _timezone() -> ZoneInfo:
    return ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai"))


def _now(value: Optional[datetime] = None) -> datetime:
    if value is None:
        return datetime.now(_timezone())
    if value.tzinfo is None:
        return value.replace(tzinfo=_timezone())
    return value.astimezone(_timezone())


def _normalize_days(value: Any, default: str = "1-5") -> str:
    """Return an APScheduler numeric day-of-week expression (Monday is 0)."""
    if value is None or value == "":
        return default

    parts = value if isinstance(value, (list, tuple, set)) else str(value).split(",")
    days = set()
    for part in parts:
        text = str(part).strip()
        if not text:
            continue
        if "-" in text:
            start_text, end_text = text.split("-", 1)
            start, end = int(start_text), int(end_text)
            if start > end:
                raise ValueError(f"Invalid day-of-week range: {text}")
            days.update(range(start, end + 1))
        else:
            days.add(int(text))

    if not days or min(days) < 0 or max(days) > 6:
        raise ValueError(f"APScheduler day-of-week must be between 0 and 6: {value}")

    ordered = sorted(days)
    ranges = []
    start = previous = ordered[0]
    for day in ordered[1:]:
        if day == previous + 1:
            previous = day
            continue
        ranges.append(str(start) if start == previous else f"{start}-{previous}")
        start = previous = day
    ranges.append(str(start) if start == previous else f"{start}-{previous}")
    return ",".join(ranges)


def _expand_days(expression: str) -> set[int]:
    days: set[int] = set()
    if expression == "*":
        return set(range(7))
    for part in expression.split(","):
        if "-" in part:
            start, end = (int(value) for value in part.split("-", 1))
            days.update(range(start, end + 1))
        elif part:
            days.add(int(part))
    return days


def _day_of_week(market: str, time_str: str) -> str:
    """Map Beijing-time jobs to the relevant market's trading weekdays."""
    schedule = cfg.schedule
    if not schedule.get("weekdays_only", True):
        return "*"

    hour = int(str(time_str).split(":", 1)[0])
    if market.lower() == "us" and hour < 12:
        return _normalize_days(schedule.get("us_early_morning_days"), default="1-5")
    return "0-4"


def _cron_trigger(market: str, time_str: str) -> CronTrigger:
    hour_text, minute_text = str(time_str).split(":", 1)
    return CronTrigger(
        day_of_week=_day_of_week(market, str(time_str)),
        hour=int(hour_text),
        minute=int(minute_text),
        timezone=cfg.schedule.get("timezone", "Asia/Shanghai"),
    )


def _market_config(market: str) -> Dict[str, Any]:
    return cfg.market_config(market)


def _round_report_path(market: str, label: str, value: Optional[datetime] = None) -> Path:
    date = _now(value).strftime("%Y%m%d")
    return REPORT_DIR / f"{date}-{market}-{label}.md"


def _write_report(path: Path, title: str, content: str, generated_at: datetime, catch_up: bool) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    if catch_up:
        run_mode = "启动补跑"
    elif "-manual-" in path.stem or "-chat-" in path.stem:
        run_mode = "手动整轮"
    else:
        run_mode = "全自动定时"
    prefix = (
        f"# {title}\n\n"
        f"> 生成时间：{generated_at.isoformat(timespec='seconds')}  "
        f"| 模式：{run_mode}\n\n"
    )
    path.write_text(prefix + content.strip() + "\n", encoding="utf-8")


def _action_label(action: Any) -> str:
    return {"BUY": "买入/加仓", "SELL": "卖出/减仓", "HOLD": "观望/继续持有"}.get(
        str(action or "HOLD").upper(), "观望/继续持有"
    )


def _decision_section(autonomous: Mapping[str, Any]) -> str:
    decisions = autonomous.get("chair", {}).get("decisions", [])
    held = {
        str(item.get("code", "")).upper()
        for item in autonomous.get("account_before", {}).get("holdings", [])
        if item.get("code")
    }
    selected = {
        str(value).upper()
        for value in autonomous.get("screening", {}).get("selected_symbols", [])
    }
    by_symbol = {
        str(item.get("symbol", "")).upper(): item
        for item in decisions
        if isinstance(item, Mapping) and item.get("symbol")
    }
    symbols = list(dict.fromkeys([*sorted(selected), *sorted(held), *by_symbol]))
    mandate = autonomous.get("mandate", {}) if isinstance(autonomous.get("mandate"), Mapping) else {}
    lines = ["## 本轮投资授权书", ""]
    if mandate:
        lines.extend([
            f"- 策略：{mandate.get('display_name', mandate.get('profile', '未知'))}",
            f"- 目标：{mandate.get('objective', '未记录')}",
            f"- 版本：{mandate.get('risk_policy_version', '未记录')}",
            "",
        ])
    else:
        lines.extend(["- 本轮未记录策略授权书快照。", ""])
    lines.extend(["## 逐标的最终决策", "", "| 标的 | 身份 | 决策 | 置信度 | 依据 |", "|---|---|---|---:|---|"])
    for symbol in symbols:
        decision = by_symbol.get(symbol)
        identities = []
        if symbol in selected:
            identities.append("本轮候选")
        if symbol in held:
            identities.append("已有持仓")
        confidence = decision.get("confidence") if decision else None
        confidence_text = f"{float(confidence):.0%}" if isinstance(confidence, (int, float)) else "-"
        action_text = _action_label(decision.get("action")) if decision else "未形成决策"
        reason = str(decision.get("reason", "未提供依据") if decision else "分析链被阻断或失败，未生成该标的决策").replace("|", "/")[:240]
        lines.append(
            f"| {symbol} | {'、'.join(identities) or '分析池'} | "
            f"{action_text} | {confidence_text} | {reason} |"
        )
    if not symbols:
        lines.append("| - | - | 本轮无有效标的 | - | 筛选或行情数据不足 |")

    execution = autonomous.get("execution", {})
    lines.extend(["", "## 模拟执行结果", ""])
    fills = execution.get("fills", []) if isinstance(execution, Mapping) else []
    rejected = execution.get("rejected", []) if isinstance(execution, Mapping) else []
    if fills:
        for fill in fills:
            lines.append(
                f"- 已成交：{fill.get('code')} {_action_label(fill.get('action'))} "
                f"{fill.get('shares', 0)} 股，成交价 {fill.get('price', fill.get('fill_price', '-'))}。"
            )
    else:
        lines.append("- 本轮没有产生模拟成交。")
    for item in rejected:
        lines.append(f"- 被拒订单：{item.get('code', item.get('symbol', '-'))}，原因：{item.get('reason', '未知')}。")
    return "\n".join(lines)


def _deliver_completed_report(path: Path, title: str, content: str, market: str, label: str) -> Dict[str, Any]:
    try:
        from src.notifications import deliver_report

        return deliver_report(
            title=title,
            content=content,
            report_path=path,
            metadata={"market": market, "label": label},
        )
    except Exception as exc:
        logger.exception("[NOTIFY:%s] report delivery failed", market)
        return {"status": "error", "error": str(exc)[:500]}


def _account_context(market: str) -> Dict[str, Any]:
    acct = account.account(market)
    holdings = acct.get("holdings", [])
    snapshots = []
    if holdings:
        from src.data import fetcher

        for holding in holdings[:12]:
            code = holding.get("code")
            if not code:
                continue
            try:
                snapshots.append({"code": code, "snapshot": fetcher.snapshot(str(code))})
            except Exception as exc:
                snapshots.append({"code": code, "error": str(exc)})
    return {"account": acct, "holding_snapshots": snapshots}


def _latest_macro_excerpt() -> str:
    try:
        from src.macro import DATA_ROOT, latest_dates, report_path

        dates = latest_dates(DATA_ROOT)
        if not dates:
            return ""
        path = report_path(dates[0], DATA_ROOT)
        return path.read_text(encoding="utf-8")[:6000] if path.exists() else ""
    except Exception:
        return ""


def _complete_report(llm: Any, messages: List[Dict[str, str]]) -> str:
    kwargs: Dict[str, Any] = {"temperature": 0.2, "max_tokens": 4096}
    if getattr(llm, "provider_name", "") == "deepseek":
        kwargs["extra_body"] = {"thinking": {"type": "disabled"}}
    text = llm.chat(messages, **kwargs)
    if not text or not text.strip():
        raise RuntimeError("LLM returned an empty report")
    return text


def _generate_report_or_fallback(llm: Any, messages: List[Dict[str, str]], autonomous: Mapping[str, Any]) -> str:
    try:
        return _complete_report(llm, messages)
    except Exception as exc:
        logger.exception("LLM report summary failed; writing deterministic report")
        status = autonomous.get("status", "error")
        reason = autonomous.get("error") or autonomous.get("reason") or "无"
        return "\n".join([
            "## 本轮概览",
            "",
            f"- 自主投资状态：{status}",
            f"- 异常或阻断原因：{reason}",
            "- AI 摘要生成失败，以下逐标的决策和模拟执行结果来自本轮审计数据。",
        ])


def _compact_agent_report(report: Any) -> Dict[str, Any]:
    """Keep the auditable conclusions while bounding report-prompt size."""
    if not isinstance(report, Mapping):
        return {}
    compact: Dict[str, Any] = {}
    for key in ("role", "role_name", "stage", "thesis", "summary", "stance", "confidence"):
        if key in report:
            compact[key] = report[key]
    findings = report.get("findings")
    if isinstance(findings, list):
        compact["findings"] = findings[:6]
    decisions = report.get("decisions")
    if isinstance(decisions, list):
        compact["decisions"] = decisions[:20]
    citations = report.get("citations")
    if isinstance(citations, list):
        compact["citations"] = citations[:20]
    data_gaps = report.get("data_gaps")
    if isinstance(data_gaps, list):
        compact["data_gaps"] = data_gaps[:8]
    return compact


def _compact_autonomous_for_report(autonomous: Mapping[str, Any]) -> Dict[str, Any]:
    """Prioritize decisions and execution over verbose multi-agent transcripts."""
    result: Dict[str, Any] = {
        key: autonomous.get(key)
        for key in ("status", "audit_file", "chair", "risk", "execution", "control")
        if key in autonomous
    }
    workflow = autonomous.get("agent_workflow")
    if isinstance(workflow, Mapping) and workflow:
        base_reports = workflow.get("base_reports", {})
        symbol_research = workflow.get("symbol_research", {})
        result["agent_workflow"] = {
            "workflow": workflow.get("workflow"),
            "portfolio_manager": _compact_agent_report(workflow.get("portfolio_manager")),
            "risk_manager": _compact_agent_report(workflow.get("risk_manager")),
            "research_manager": _compact_agent_report(workflow.get("research_manager")),
            "investment_advice": _compact_agent_report(workflow.get("investment_advice")),
            "base_reports": {
                str(role): _compact_agent_report(report)
                for role, report in base_reports.items()
            } if isinstance(base_reports, Mapping) else {},
            "symbol_research": {
                str(symbol): {
                    "status": report.get("status"),
                    "research_manager": _compact_agent_report(report.get("research_manager")),
                    "trader": _compact_agent_report(report.get("trader")),
                    "errors": report.get("errors", {}),
                }
                for symbol, report in symbol_research.items()
                if isinstance(report, Mapping)
            } if isinstance(symbol_research, Mapping) else {},
            "portfolio_proposal": _compact_agent_report(workflow.get("portfolio_proposal")),
            "errors": workflow.get("errors", {}),
            "timings_seconds": workflow.get("timings_seconds", {}),
            "memory": workflow.get("memory", {}),
        }
    for key in ("screening", "prices", "market_data_errors", "error"):
        if key in autonomous:
            result[key] = autonomous[key]
    return result


def _run_intraday_job(
    market: str,
    time_str: str,
    label: str,
    *,
    catch_up: bool = False,
    now: Optional[datetime] = None,
    scheduled_at: Optional[datetime] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    current = _now(now)
    scheduled = _now(scheduled_at) if scheduled_at is not None else _scheduled_reference(current, time_str)
    path = _round_report_path(market, label, scheduled)
    with atomic_claim(path.with_suffix(path.suffix + ".lock")) as claimed:
        if not claimed:
            return {"status": "exists" if path.exists() else "in_progress", "market": market, "label": label, "report": str(path)}
        if path.exists():
            return {"status": "exists", "market": market, "label": label, "report": str(path)}

        logger.info("[INTRADAY:%s] %s started (scheduled %s, catch_up=%s)", market, label, scheduled.isoformat(), catch_up)
        macro = _latest_macro_excerpt()
        try:
            from src.trading.controller import run_autonomous_cycle

            autonomous = run_autonomous_cycle(
                market,
                label=label,
                now=current,
                macro_excerpt=macro,
                catch_up=catch_up,
                progress_callback=progress_callback,
            )
        except Exception as exc:
            logger.exception("[AUTONOMOUS:%s] cycle failed", market)
            autonomous = {"status": "error", "error": str(exc)}
        context = _account_context(market)
        if progress_callback is not None:
            progress_callback("交易审计完成，正在生成并投递整轮报告…")
        llm = resolve_llm(role="analyst")
        prompt = (
            f"当前北京时间 {current.strftime('%Y-%m-%d %H:%M')}，执行 {market} 市场 {label} 轮次"
            f"（计划时间 {scheduled.isoformat(timespec='minutes')}，{'补跑' if catch_up else '准时运行'}）。\n"
            f"账户及持仓行情：\n{json.dumps(context, ensure_ascii=False)[:8000]}\n\n"
            f"最新宏观摘要：\n{(macro or '暂无宏观日报')[:2500]}\n\n"
            f"自主模拟交易执行结果：\n{json.dumps(_compact_autonomous_for_report(autonomous), ensure_ascii=False)[:12000]}\n\n"
            "请直接输出不超过 800 字的可审计最终报告，不展示思考过程。包括行情与持仓检查、"
            "止损止盈、风险暴露和已执行/被拒绝订单；数据不足时明确说明，不得虚构成交。"
        )
        response = _generate_report_or_fallback(llm, [{"role": "user", "content": prompt}], autonomous)
        report_content = response.rstrip() + "\n\n" + _decision_section(autonomous)
        report_title = f"{market.upper()} {label} 完整投资轮次报告"
        _write_report(path, report_title, report_content, current, catch_up)
        notification = _deliver_completed_report(path, report_title, report_content, market, label)
        try:
            from src.investment.mandate import get_mandate
            from src.investment.reflection import InvestmentReflectionService

            reflection_service = InvestmentReflectionService()
            outcome_evaluations = reflection_service.evaluate_pending(market)
            reflection = reflection_service.reflect_cycle(
                {"status": "generated", "market": market, "report": str(path), "autonomous": autonomous},
                mandate=get_mandate(),
                trigger="catch-up" if catch_up else ("manager" if label.startswith(("agent", "button")) else "scheduler"),
            )
        except Exception as exc:
            logger.exception("[REFLECTION:%s] failed", market)
            reflection = {"status": "error", "error": str(exc)}
            outcome_evaluations = []
        logger.info("[INTRADAY:%s] %s report written: %s", market, label, path)
        result = {
            "status": "generated",
            "market": market,
            "label": label,
            "report": str(path),
            "notification": notification,
            "reflection": reflection,
            "outcome_evaluations": outcome_evaluations,
            "autonomous": {
                "status": autonomous.get("status"),
                "reason": autonomous.get("reason"),
                "error": autonomous.get("error"),
                "control": autonomous.get("control"),
                "audit_file": autonomous.get("audit_file"),
                "fills": autonomous.get("execution", {}).get("fills", []),
            },
        }
        if re.fullmatch(r"\d{4}", label):
            try:
                from src.manager.report_inbox import publish_cycle_report

                result["chat_delivery"] = publish_cycle_report(
                    result, title=report_title, report_content=report_content
                )
            except Exception as exc:
                logger.exception("[CHAT-INBOX:%s] report publish failed", market)
                result["chat_delivery"] = {"status": "error", "error": str(exc)[:500]}
        return result


def _run_close_job(
    market: str,
    time_str: str,
    *,
    catch_up: bool = False,
    now: Optional[datetime] = None,
    scheduled_at: Optional[datetime] = None,
) -> Dict[str, Any]:
    current = _now(now)
    scheduled = _now(scheduled_at) if scheduled_at is not None else _scheduled_reference(current, time_str)
    path = _round_report_path(market, "close", scheduled)
    with atomic_claim(path.with_suffix(path.suffix + ".lock")) as claimed:
        if not claimed:
            return {"status": "exists" if path.exists() else "in_progress", "market": market, "label": "close", "report": str(path)}
        if path.exists():
            return {"status": "exists", "market": market, "label": "close", "report": str(path)}

        logger.info("[CLOSE:%s] started (scheduled %s, catch_up=%s)", market, scheduled.isoformat(), catch_up)
        optimizer_result: Dict[str, Any]
        try:
            from src.optimizer.runner import compact_result, run_optimizer

            optimizer_result = compact_result(run_optimizer(market=market, now=current))
        except Exception as exc:
            optimizer_result = {"error": str(exc)}
        macro = _latest_macro_excerpt()
        try:
            from src.trading.controller import run_autonomous_cycle

            autonomous = run_autonomous_cycle(
                market,
                label="close",
                now=current,
                macro_excerpt=macro,
                optimizer_hint=optimizer_result,
                catch_up=catch_up,
            )
        except Exception as exc:
            logger.exception("[AUTONOMOUS:%s] close cycle failed", market)
            autonomous = {"status": "error", "error": str(exc)}
        context = _account_context(market)
        llm = resolve_llm(role="judge")
        prompt = (
            f"当前北京时间 {current.strftime('%Y-%m-%d %H:%M')}，执行 {market} 收盘分析"
            f"（计划时间 {scheduled.isoformat(timespec='minutes')}，{'补跑' if catch_up else '准时运行'}）。\n"
            f"账户及持仓行情：\n{json.dumps(context, ensure_ascii=False)[:7000]}\n\n"
            f"组合优化结果：\n{json.dumps(optimizer_result, ensure_ascii=False)[:9000]}\n\n"
            f"自主模拟交易执行结果：\n{json.dumps(_compact_autonomous_for_report(autonomous), ensure_ascii=False)[:12000]}\n\n"
            "请直接输出不超过 1200 字的收盘复盘、压力测试解读和下一交易日计划，"
            "同时列出真实执行与被风控拒绝的订单，不展示思考过程，不得虚构成交。"
        )
        response = _generate_report_or_fallback(llm, [{"role": "user", "content": prompt}], autonomous)
        report_content = response.rstrip() + "\n\n" + _decision_section(autonomous)
        report_title = f"{market.upper()} 收盘完整投资轮次报告"
        _write_report(path, report_title, report_content, current, catch_up)
        notification = _deliver_completed_report(path, report_title, report_content, market, "close")
        try:
            from src.investment.mandate import get_mandate
            from src.investment.reflection import InvestmentReflectionService

            reflection_service = InvestmentReflectionService()
            outcome_evaluations = reflection_service.evaluate_pending(market)
            reflection = reflection_service.reflect_cycle(
                {"status": "generated", "market": market, "report": str(path), "autonomous": autonomous},
                mandate=get_mandate(),
                trigger="catch-up" if catch_up else "scheduler-close",
            )
        except Exception as exc:
            logger.exception("[REFLECTION:%s] close reflection failed", market)
            reflection = {"status": "error", "error": str(exc)}
            outcome_evaluations = []
        logger.info("[CLOSE:%s] report written: %s", market, path)
        result = {
            "status": "generated",
            "market": market,
            "label": "close",
            "report": str(path),
            "notification": notification,
            "reflection": reflection,
            "outcome_evaluations": outcome_evaluations,
            "autonomous": {
                "status": autonomous.get("status"),
                "reason": autonomous.get("reason"),
                "error": autonomous.get("error"),
                "control": autonomous.get("control"),
                "audit_file": autonomous.get("audit_file"),
                "fills": autonomous.get("execution", {}).get("fills", []),
            },
        }
        try:
            from src.manager.report_inbox import publish_cycle_report

            result["chat_delivery"] = publish_cycle_report(
                result, title=report_title, report_content=report_content
            )
        except Exception as exc:
            logger.exception("[CHAT-INBOX:%s] close report publish failed", market)
            result["chat_delivery"] = {"status": "error", "error": str(exc)[:500]}
        return result


def _build_intraday_job(market: str, time_str: str, label: str):
    def job() -> None:
        if str(cfg.autonomous.get("operation_mode", "automatic")).lower() != "automatic":
            logger.info("[INTRADAY:%s] skipped because operation_mode is manual", market)
            return
        current = _now()
        _run_intraday_job(market, time_str, label, now=current, scheduled_at=_scheduled_reference(current, time_str))

    return job


def _build_close_job(market: str, time_str: str):
    def job() -> None:
        if str(cfg.autonomous.get("operation_mode", "automatic")).lower() != "automatic":
            logger.info("[CLOSE:%s] skipped because operation_mode is manual", market)
            return
        current = _now()
        _run_close_job(market, time_str, now=current, scheduled_at=_scheduled_reference(current, time_str))

    return job


def _run_macro_job() -> None:
    from src.macro import run_daily

    result = run_daily()
    logger.info("[MACRO] %s", result)


def _scheduled_datetime(current: datetime, time_str: str) -> datetime:
    hour, minute = (int(value) for value in str(time_str).split(":", 1))
    return current.replace(hour=hour, minute=minute, second=0, microsecond=0)


def _scheduled_reference(current: datetime, time_str: str) -> datetime:
    """Infer the intended fire time for a regular job, including midnight misfires."""
    scheduled = _scheduled_datetime(current, time_str)
    if scheduled > current and scheduled - current > timedelta(hours=12):
        scheduled -= timedelta(days=1)
    return scheduled


def planned_catch_up(now: Optional[datetime] = None, markets: Optional[Sequence[str]] = None) -> List[Dict[str, Any]]:
    current = _now(now)
    selected = list(markets or cfg.schedule.get("catch_up_markets") or ["cn"])
    grace = int(cfg.schedule.get("catch_up_grace_minutes", 480))
    planned: List[Dict[str, Any]] = []

    for market in selected:
        if market not in cfg.enabled_markets:
            continue
        for time_str in cfg.intraday_times(market) or []:
            time_text = str(time_str)
            if current.weekday() not in _expand_days(_day_of_week(market, time_text)):
                continue
            scheduled = _scheduled_datetime(current, time_text)
            label = time_text.replace(":", "")
            age = (current - scheduled).total_seconds() / 60
            if 0 <= age <= grace and not _round_report_path(market, label, scheduled).exists():
                planned.append({"kind": "intraday", "market": market, "time": time_text, "label": label, "scheduled": scheduled})
        close_time = str(cfg.close_time(market) or "")
        if close_time and current.weekday() in _expand_days(_day_of_week(market, close_time)):
            scheduled = _scheduled_datetime(current, close_time)
            age = (current - scheduled).total_seconds() / 60
            if 0 <= age <= grace and not _round_report_path(market, "close", scheduled).exists():
                planned.append({"kind": "close", "market": market, "time": close_time, "label": "close", "scheduled": scheduled})
    return sorted(planned, key=lambda item: item["scheduled"])


def run_catch_up(markets: Optional[Sequence[str]] = None, *, include_macro: bool = True, now: Optional[datetime] = None) -> List[Dict[str, Any]]:
    current = _now(now)
    results: List[Dict[str, Any]] = []
    if include_macro:
        macro_time = str(cfg.schedule.get("macro_daily_time", "08:00"))
        if _scheduled_datetime(current, macro_time) <= current:
            try:
                from src.macro import run_daily

                results.append({"kind": "macro", **run_daily(now=current)})
            except Exception as exc:
                logger.exception("Macro startup catch-up failed")
                results.append({"kind": "macro", "status": "error", "error": str(exc)})

    if str(cfg.autonomous.get("operation_mode", "automatic")).lower() != "automatic":
        return results

    for item in planned_catch_up(current, markets):
        try:
            if item["kind"] == "intraday":
                result = _run_intraday_job(
                    item["market"], item["time"], item["label"],
                    catch_up=True, now=current, scheduled_at=item["scheduled"],
                )
            else:
                result = _run_close_job(
                    item["market"], item["time"],
                    catch_up=True, now=current, scheduled_at=item["scheduled"],
                )
            results.append({"kind": item["kind"], **result})
        except Exception as exc:
            logger.exception("Catch-up failed for %s", item)
            results.append({"kind": item["kind"], "market": item["market"], "label": item["label"], "status": "error", "error": str(exc)})
    return results


def start(*, catch_up: bool = True) -> _BgScheduler:
    lease = ProcessLease(SCHEDULER_LOCK)
    lease.acquire()
    misfire_seconds = max(60, int(cfg.schedule.get("catch_up_grace_minutes", 480)) * 60)
    scheduler = _LockedScheduler(
        timezone=cfg.schedule.get("timezone", "Asia/Shanghai"),
        job_defaults={"coalesce": True, "max_instances": 1, "misfire_grace_time": misfire_seconds},
        process_lease=lease,
    )
    try:
        for market in cfg.enabled_markets:
            for time_str in cfg.intraday_times(market) or []:
                time_text = str(time_str)
                label = time_text.replace(":", "")
                scheduler.add_job(_build_intraday_job(market, time_text, label), trigger=_cron_trigger(market, time_text), id=f"{market}-{label}", replace_existing=True)
            close_time = str(cfg.close_time(market) or "")
            if close_time:
                scheduler.add_job(_build_close_job(market, close_time), trigger=_cron_trigger(market, close_time), id=f"{market}-close", replace_existing=True)

        macro_time = str(cfg.schedule.get("macro_daily_time", "08:00"))
        macro_hour, macro_minute = (int(value) for value in macro_time.split(":", 1))
        scheduler.add_job(
            _run_macro_job,
            trigger=CronTrigger(hour=macro_hour, minute=macro_minute, timezone=cfg.schedule.get("timezone", "Asia/Shanghai")),
            id="macro-daily",
            replace_existing=True,
        )
        scheduler.start()
        if catch_up:
            scheduler.add_job(
                run_catch_up,
                trigger="date",
                run_date=_now() + timedelta(seconds=1),
                id="startup-catch-up",
                replace_existing=True,
            )
    except Exception:
        lease.release()
        raise
    logger.info("Scheduler started with %d jobs", len(scheduler.get_jobs()))
    return scheduler


def run_investment_cycle(
    market: str = "cn",
    *,
    label: str = "manual",
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    current = _now()
    unique_label = f"{label}-{current.strftime('%H%M%S')}"
    return _run_intraday_job(
        market, current.strftime("%H:%M"), unique_label,
        now=current, progress_callback=progress_callback,
    )


def run_once(market: str = "cn") -> str:
    return json.dumps(run_investment_cycle(market), ensure_ascii=False, indent=2)
