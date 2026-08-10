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
ROOT = Path(__file__).resolve().parent.parent.parent

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
            trigger = CronTrigger.from_crontab(f"{t.split(':')[1]} {t.split(':')[0]} * * 1-5")
            scheduler.add_job(_build_intraday_job(market, t, label), trigger=trigger, id=f"{market}-{label}")
        ct = cfg.close_time(market)
        if ct:
            trigger = CronTrigger.from_crontab(f"{ct.split(':')[1]} {ct.split(':')[0]} * * 1-5")
            scheduler.add_job(_build_close_job(market, ct), trigger=trigger, id=f"{market}-close")
    scheduler.start()
    return scheduler

# ── manual run once ────────────────────────────────
def run_once(market: str = "cn"):
    llm = resolve_llm(role="judge")
    resp = llm.chat([{"role":"user","content":f"执行一次 {market} 市场分析并输出报告。"}])
    print(resp)
