from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
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
    prefix = (
        f"# {title}\n\n"
        f"> 生成时间：{generated_at.isoformat(timespec='seconds')}  "
        f"| 模式：{'启动补跑' if catch_up else '定时运行'}\n\n"
    )
    path.write_text(prefix + content.strip() + "\n", encoding="utf-8")


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


def _run_intraday_job(
    market: str,
    time_str: str,
    label: str,
    *,
    catch_up: bool = False,
    now: Optional[datetime] = None,
    scheduled_at: Optional[datetime] = None,
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
        context = _account_context(market)
        macro = _latest_macro_excerpt()
        llm = resolve_llm(role="analyst")
        prompt = (
            f"当前北京时间 {current.strftime('%Y-%m-%d %H:%M')}，执行 {market} 市场 {label} 轮次"
            f"（计划时间 {scheduled.isoformat(timespec='minutes')}，{'补跑' if catch_up else '准时运行'}）。\n"
            f"账户及持仓行情：\n{json.dumps(context, ensure_ascii=False)[:8000]}\n\n"
            f"最新宏观摘要：\n{(macro or '暂无宏观日报')[:2500]}\n\n"
            "请直接输出不超过 800 字的可审计最终报告，不展示思考过程。包括行情与持仓检查、"
            "止损止盈、风险暴露和操作参考；数据不足时明确说明，不得虚构成交。"
        )
        response = _complete_report(llm, [{"role": "user", "content": prompt}])
        _write_report(path, f"{market.upper()} {label} 轮次报告", response, current, catch_up)
        logger.info("[INTRADAY:%s] %s report written: %s", market, label, path)
        return {"status": "generated", "market": market, "label": label, "report": str(path)}


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
        context = _account_context(market)
        optimizer_result: Dict[str, Any]
        try:
            from src.optimizer.runner import compact_result, run_optimizer

            optimizer_result = compact_result(run_optimizer(market=market, now=current))
        except Exception as exc:
            optimizer_result = {"error": str(exc)}
        llm = resolve_llm(role="judge")
        prompt = (
            f"当前北京时间 {current.strftime('%Y-%m-%d %H:%M')}，执行 {market} 收盘分析"
            f"（计划时间 {scheduled.isoformat(timespec='minutes')}，{'补跑' if catch_up else '准时运行'}）。\n"
            f"账户及持仓行情：\n{json.dumps(context, ensure_ascii=False)[:7000]}\n\n"
            f"组合优化结果：\n{json.dumps(optimizer_result, ensure_ascii=False)[:9000]}\n\n"
            "请直接输出不超过 1200 字的收盘复盘、压力测试解读和下一交易日计划，"
            "不展示思考过程，不得虚构成交。"
        )
        response = _complete_report(llm, [{"role": "user", "content": prompt}])
        _write_report(path, f"{market.upper()} 收盘报告", response, current, catch_up)
        logger.info("[CLOSE:%s] report written: %s", market, path)
        return {"status": "generated", "market": market, "label": "close", "report": str(path)}


def _build_intraday_job(market: str, time_str: str, label: str):
    def job() -> None:
        current = _now()
        _run_intraday_job(market, time_str, label, now=current, scheduled_at=_scheduled_reference(current, time_str))

    return job


def _build_close_job(market: str, time_str: str):
    def job() -> None:
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


def run_once(market: str = "cn") -> str:
    current = _now()
    result = _run_intraday_job(market, current.strftime("%H:%M"), "manual", now=current)
    return json.dumps(result, ensure_ascii=False, indent=2)
