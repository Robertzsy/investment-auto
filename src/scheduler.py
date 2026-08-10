from __future__ import annotations

import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from apscheduler.schedulers.background import BackgroundScheduler as _BgScheduler
from apscheduler.triggers.cron import CronTrigger

from src.config import cfg
from src.llm.registry import resolve_llm
from src.portfolio import account

logger = logging.getLogger(__name__)
ROOT = Path(__file__).resolve().parent.parent


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


def _day_of_week(market: str, time_str: str) -> str:
    """Map Beijing-time jobs to the relevant market's trading weekdays."""
    schedule = cfg.schedule
    if not schedule.get("weekdays_only", True):
        return "*"

    hour = int(time_str.split(":", 1)[0])
    if market.lower() == "us" and hour < 12:
        return _normalize_days(schedule.get("us_early_morning_days"), default="1-5")
    return "0-4"


def _cron_trigger(market: str, time_str: str) -> CronTrigger:
    hour_text, minute_text = time_str.split(":", 1)
    return CronTrigger(
        day_of_week=_day_of_week(market, time_str),
        hour=int(hour_text),
        minute=int(minute_text),
        timezone=cfg.schedule.get("timezone", "Asia/Shanghai"),
    )

# ── market rules from YAML ──────────────────────────
def _market_config(market: str) -> Dict:
    return cfg.market_config(market)

# ── job factories ───────────────────────────────
def _build_intraday_job(market: str, time_str: str, label: str):
    def job():
        logger.info(f"[INTRADAY:{market}] {label} started at {time_str}")
        llm = resolve_llm(role="analyst")
        acct = account.account(market)
        symbols = [h["code"] for h in acct.get("holdings", [])]
        prompt = (
            f"今天是 {datetime.now().strftime('%Y-%m-%d %H:%M')}。{market}市场 {label} 轮次。\n"
            f"当前持仓: {json.dumps(symbols, ensure_ascii=False)}\n"
            f"请基于行情执行快速检查、止损止盈评估，如果需要新建仓则给出候选标的和量化权重建议。"
        )
        resp = llm.chat([{"role":"user","content":prompt}])
        # write stub report
        (ROOT / "runtime" / "reports" / f"{datetime.now().strftime('%Y%m%d')}-{market}-{label}.md").parent.mkdir(parents=True, exist_ok=True)
        (ROOT / "runtime" / "reports" / f"{datetime.now().strftime('%Y%m%d')}-{market}-{label}.md").write_text(resp, encoding="utf-8")
    return job

def _build_close_job(market: str, time_str: str):
    def job():
        logger.info(f"[CLOSE:{market}] started at {time_str}")
        llm = resolve_llm(role="judge")
        acct = account.account(market)
        resp = llm.chat([{"role":"user","content":f"今天是 {datetime.now().strftime('%Y-%m-%d')}，执行 {market} 收盘完整分析（含优化器、压力测试和最终再平衡）。"}])
        (ROOT / "runtime" / "reports" / f"{datetime.now().strftime('%Y%m%d')}-{market}-close.md").parent.mkdir(parents=True, exist_ok=True)
        (ROOT / "runtime" / "reports" / f"{datetime.now().strftime('%Y%m%d')}-{market}-close.md").write_text(resp, encoding="utf-8")
    return job

# ── scheduler setup ───────────────────────────────
def start() -> _BgScheduler:
    scheduler = _BgScheduler(timezone=cfg.schedule["timezone"])
    for market in cfg.enabled_markets:
        for t in cfg.intraday_times(market) or []:
            label = t.replace(":", "")
            trigger = _cron_trigger(market, t)
            scheduler.add_job(_build_intraday_job(market, t, label), trigger=trigger, id=f"{market}-{label}")
        ct = cfg.close_time(market)
        if ct:
            trigger = _cron_trigger(market, ct)
            scheduler.add_job(_build_close_job(market, ct), trigger=trigger, id=f"{market}-close")
    scheduler.start()
    return scheduler

# ── manual run once ────────────────────────────────
def run_once(market: str = "cn"):
    llm = resolve_llm(role="judge")
    resp = llm.chat([{"role":"user","content":f"执行一次 {market} 市场分析并输出报告。"}])
    print(resp)
