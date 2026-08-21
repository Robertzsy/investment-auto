from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from apscheduler.schedulers.background import BackgroundScheduler as _BgScheduler
from apscheduler.triggers.cron import CronTrigger

from engine.config import cfg
from engine.portfolio import account
from engine.runtime_lock import ProcessLease, atomic_claim

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent
from engine.paths import runtime_dir
REPORT_DIR = runtime_dir() / "reports"
SCHEDULER_LOCK = runtime_dir() / "scheduler.lock"
_active_scheduler: Optional[_BgScheduler] = None

# ── cycle runner plug (Investment Auto 2.0) ─────────────────────────────────
#
# The AI decision layer moved to the DSH app. The engine scheduler keeps the
# market-hours triggering, report/reflection/notification lifecycle and the
# fail-safe execution boundaries. The DSH bridge (P3) registers a runner that
# spawns a headless DSH session for one round and returns the cycle result.
#
# Runner contract (see docs/ENGINE_API.md):
#   runner(market: str, cycle_type: str, context: dict) -> dict
#   context keys: label, time_str, scheduled_at, catch_up, now, macro_excerpt,
#                 optimizer_hint, account, progress_callback
#   result keys:  status ("generated"|"skipped"|"error"), reason/error,
#                 report_text (optional AI summary), decisions (optional list),
#                 execution {fills, rejected}, screening, mandate, audit_file,
#                 warnings, degraded_mode, control
_cycle_runner: Optional[Callable[[str, str, Mapping[str, Any]], Dict[str, Any]]] = None
_cycle_runner_lock = threading.RLock()


def set_cycle_runner(runner: Optional[Callable[[str, str, Mapping[str, Any]], Dict[str, Any]]]) -> None:
    """Register (or clear) the cycle runner provided by the DSH bridge."""
    global _cycle_runner
    with _cycle_runner_lock:
        _cycle_runner = runner


def _run_engine_cycle(market: str, cycle_type: str, context: Mapping[str, Any]) -> Dict[str, Any]:
    """Execute one round through the registered runner; fail soft when absent."""
    with _cycle_runner_lock:
        runner = _cycle_runner
    if runner is None:
        return {
            "status": "skipped",
            "reason": "cycle_runner_unavailable",
            "error": "自主轮次执行器未注册（DSH bridge，P3 阶段落地）；行情/选股/组合等只读能力不受影响",
        }
    try:
        return dict(runner(market, cycle_type, dict(context)) or {})
    except Exception as exc:
        logger.exception("[CYCLE:%s] runner failed", market)
        return {"status": "error", "error": str(exc)[:500]}


class _LockedScheduler(_BgScheduler):
    def __init__(self, *args: Any, process_lease: ProcessLease, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        self._process_lease = process_lease

    def shutdown(self, wait: bool = True) -> None:
        global _active_scheduler
        try:
            super().shutdown(wait=wait)
        finally:
            if _active_scheduler is self:
                _active_scheduler = None
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
    elif any(marker in path.stem for marker in ("-manual-", "-chat-", "-button-", "-agent-")):
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
    chair = autonomous.get("chair") if isinstance(autonomous.get("chair"), Mapping) else {}
    decisions = chair.get("decisions", autonomous.get("decisions", [])) or []
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
    excluded = set()
    symbol_errors: Dict[str, Any] = {}
    mandate = autonomous.get("mandate", {}) if isinstance(autonomous.get("mandate"), Mapping) else {}
    symbols = list(dict.fromkeys([*sorted(selected), *sorted(held), *by_symbol]))
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
        if decision:
            action_text = _action_label(decision.get("action"))
            reason_value = decision.get("reason", "未提供依据")
        elif symbol in excluded:
            action_text = "研究失败/已排除"
            reason_value = symbol_errors.get(symbol, "逐标的研究失败，未进入组合决策")
        else:
            action_text = "未形成决策"
            reason_value = "分析链被阻断或失败，未生成该标的决策"
        reason = str(reason_value).replace("|", "/")[:240]
        lines.append(
            f"| {symbol} | {'、'.join(identities) or '分析池'} | "
            f"{action_text} | {confidence_text} | {reason} |"
        )
    if not symbols:
        lines.append("| - | - | 本轮无有效标的 | - | 筛选或行情数据不足 |")

    execution = autonomous.get("execution", {}) if isinstance(autonomous.get("execution"), Mapping) else {}
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


def _fallback_report_text(cycle: Mapping[str, Any], cycle_type: str) -> str:
    if cycle.get("status") == "skipped":
        return f"本轮未执行自主投资分析：{cycle.get('reason') or cycle.get('error') or '执行器未就绪'}。"
    if cycle.get("status") == "error":
        return f"本轮自主投资分析失败：{cycle.get('error') or '未知错误'}。"
    return f"{cycle_type} 轮次执行完成。"


def _build_report_content(cycle: Mapping[str, Any], cycle_type: str) -> str:
    summary = cycle.get("report_text")
    if isinstance(summary, str) and summary.strip():
        content = summary.strip()
    else:
        content = _fallback_report_text(cycle, cycle_type)
    return content + "\n\n" + _decision_section(cycle)


def _deliver_completed_report(path: Path, title: str, content: str, market: str, label: str) -> Dict[str, Any]:
    try:
        from engine.notifications import deliver_report

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
        from engine.data import fetcher

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
        from engine.macro import DATA_ROOT, latest_dates, report_path

        dates = latest_dates(DATA_ROOT)
        if not dates:
            return ""
        path = report_path(dates[0], DATA_ROOT)
        return path.read_text(encoding="utf-8")[:6000] if path.exists() else ""
    except Exception:
        return ""


def _record_reflection(market: str, report_path: Path, cycle: Mapping[str, Any], trigger: str) -> Dict[str, Any]:
    try:
        from engine.investment.mandate import get_mandate
        from engine.investment.reflection import InvestmentReflectionService

        service = InvestmentReflectionService()
        outcome_evaluations = service.evaluate_pending(market)
        reflection = service.reflect_cycle(
            {"status": "generated", "market": market, "report": str(report_path), "autonomous": dict(cycle)},
            mandate=get_mandate(),
            trigger=trigger,
        )
        return {"reflection": reflection, "outcome_evaluations": outcome_evaluations}
    except Exception as exc:
        logger.exception("[REFLECTION:%s] failed", market)
        return {"reflection": {"status": "error", "error": str(exc)}, "outcome_evaluations": []}


def _cycle_result_summary(cycle: Mapping[str, Any]) -> Dict[str, Any]:
    execution = cycle.get("execution", {}) if isinstance(cycle.get("execution"), Mapping) else {}
    return {
        "status": cycle.get("status"),
        "reason": cycle.get("reason"),
        "error": cycle.get("error"),
        "warnings": cycle.get("warnings", []),
        "degraded_mode": cycle.get("degraded_mode"),
        "control": cycle.get("control"),
        "audit_file": cycle.get("audit_file"),
        "fills": execution.get("fills", []),
    }


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
        context: Dict[str, Any] = {
            "label": label,
            "time_str": time_str,
            "scheduled_at": scheduled,
            "catch_up": catch_up,
            "now": current,
            "macro_excerpt": macro,
            "account": _account_context(market),
            "progress_callback": progress_callback,
        }
        if progress_callback is not None:
            progress_callback("正在执行投资轮次（DSH 决策桥）…")
        cycle = _run_engine_cycle(market, "intraday", context)
        report_content = _build_report_content(cycle, "intraday")
        report_title = f"{market.upper()} {label} 完整投资轮次报告"
        _write_report(path, report_title, report_content, current, catch_up)
        notification = _deliver_completed_report(path, report_title, report_content, market, label)
        reflection_meta = _record_reflection(
            market, path, cycle, "catch-up" if catch_up else ("manager" if label.startswith(("agent", "button")) else "scheduler")
        )
        logger.info("[INTRADAY:%s] %s report written: %s", market, label, path)
        return {
            "status": "generated",
            "market": market,
            "label": label,
            "report": str(path),
            "notification": notification,
            **reflection_meta,
            "autonomous": _cycle_result_summary(cycle),
        }


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
            from engine.optimizer.runner import compact_result, run_optimizer

            optimizer_result = compact_result(run_optimizer(market=market, now=current))
        except Exception as exc:
            optimizer_result = {"error": str(exc)}
        macro = _latest_macro_excerpt()
        context: Dict[str, Any] = {
            "label": "close",
            "time_str": time_str,
            "scheduled_at": scheduled,
            "catch_up": catch_up,
            "now": current,
            "macro_excerpt": macro,
            "optimizer_hint": optimizer_result,
            "account": _account_context(market),
            "progress_callback": None,
        }
        cycle = _run_engine_cycle(market, "close", context)
        report_content = _build_report_content(cycle, "close")
        report_title = f"{market.upper()} 收盘完整投资轮次报告"
        _write_report(path, report_title, report_content, current, catch_up)
        notification = _deliver_completed_report(path, report_title, report_content, market, "close")
        reflection_meta = _record_reflection(
            market, path, cycle, "catch-up" if catch_up else "scheduler-close"
        )
        logger.info("[CLOSE:%s] report written: %s", market, path)
        return {
            "status": "generated",
            "market": market,
            "label": "close",
            "report": str(path),
            "notification": notification,
            **reflection_meta,
            "autonomous": _cycle_result_summary(cycle),
        }


def _build_intraday_job(market: str, time_str: str, label: str):
    def job() -> None:
        if str(cfg.autonomous.get("operation_mode", "automatic")).lower() != "automatic":
            logger.info("[INTRADAY:%s] skipped because operation_mode is manual", market)
            return
        current = _now()
        _run_intraday_job(
            market,
            time_str,
            label,
            scheduled_at=_scheduled_reference(current, time_str),
        )

    return job


def _build_close_job(market: str, time_str: str):
    def job() -> None:
        if str(cfg.autonomous.get("operation_mode", "automatic")).lower() != "automatic":
            logger.info("[CLOSE:%s] skipped because operation_mode is manual", market)
            return
        current = _now()
        _run_close_job(
            market,
            time_str,
            scheduled_at=_scheduled_reference(current, time_str),
        )

    return job


def _run_macro_job() -> None:
    from engine.macro import run_daily

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
                from engine.macro import run_daily

                results.append({"kind": "macro", **run_daily(now=current)})
            except Exception as exc:
                logger.exception("Macro startup catch-up failed")
                results.append({"kind": "macro", "status": "error", "error": str(exc)})

    if str(cfg.autonomous.get("operation_mode", "automatic")).lower() != "automatic":
        return results

    for item in planned_catch_up(current, markets):
        try:
            if item["kind"] == "close":
                result = _run_close_job(
                    item["market"],
                    item["time"],
                    catch_up=True,
                    now=current,
                    scheduled_at=item["scheduled"],
                )
            else:
                result = _run_intraday_job(
                    item["market"],
                    item["time"],
                    item["label"],
                    catch_up=True,
                    now=current,
                    scheduled_at=item["scheduled"],
                )
            results.append({"kind": item["kind"], **result})
        except Exception as exc:
            logger.exception("Catch-up failed for %s", item)
            results.append({"kind": item["kind"], "market": item["market"], "label": item["label"], "status": "error", "error": str(exc)})
    return results


def start(*, catch_up: bool = True) -> _BgScheduler:
    global _active_scheduler
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
        _active_scheduler = scheduler
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


def run_scheduled_cycle(
    market: str,
    *,
    cycle_type: str,
    label: str,
    time_str: str = "",
    catch_up: bool = False,
    scheduled_at: str = "",
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    """Worker endpoint used by the cycle command surface (HTTP API / CLI)."""
    current = _now()
    effective_time = time_str or current.strftime("%H:%M")
    if scheduled_at:
        scheduled = _now(datetime.fromisoformat(scheduled_at))
    else:
        scheduled = _scheduled_reference(current, effective_time)
    if cycle_type == "close":
        return _run_close_job(
            market,
            effective_time,
            catch_up=catch_up,
            now=current,
            scheduled_at=scheduled,
        )
    return _run_intraday_job(
        market,
        effective_time,
        label,
        catch_up=catch_up,
        now=current,
        scheduled_at=scheduled,
        progress_callback=progress_callback,
    )


def run_once(market: str = "cn") -> Dict[str, Any]:
    return run_investment_cycle(market, label="cli-once")
