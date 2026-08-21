"""Manual decision execution: the engine-side half of a DSH-driven cycle.

The DSH conversation plane researches and decides (tools + skills); this
module is the non-bypassable execution boundary that turns those decisions
into paper orders:

    决策清单 → 授权书硬边界 → 代码重算仓位 → 硬风控拒绝表
            → 纸面经纪撮合 → 审计/报告/通知/反思

Prices are always fetched by the engine (never trusted from the payload),
the allowed universe is bounded (持仓 ∪ 最新选股 ∪ 配置默认标的), and only
`trading.mode: paper` is ever executed. The kill switch refuses everything.
"""
from __future__ import annotations

import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from engine.config import cfg
from engine.paths import runtime_dir

logger = logging.getLogger("investment-auto.decisions")
AUDIT_DIR = runtime_dir() / "trading" / "audit"


def _now(value: Optional[datetime] = None) -> datetime:
    timezone = ZoneInfo(cfg.schedule.get("timezone", "Asia/Shanghai"))
    if value is None:
        return datetime.now(timezone)
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone)
    return value.astimezone(timezone)


def _normalize_decisions(raw: Any) -> List[Dict[str, Any]]:
    if not isinstance(raw, list):
        raise ValueError("decisions 必须是数组")
    if len(raw) > int(cfg.autonomous.get("max_decisions", 40)):
        raise ValueError(f"决策数量超过上限 {cfg.autonomous.get('max_decisions', 40)}")
    decisions: List[Dict[str, Any]] = []
    for index, item in enumerate(raw):
        if not isinstance(item, Mapping):
            raise ValueError(f"decisions[{index}] 必须是对象")
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol:
            raise ValueError(f"decisions[{index}] 缺少 symbol")
        action = str(item.get("action", "")).strip().upper()
        if action not in {"BUY", "SELL", "HOLD"}:
            raise ValueError(f"decisions[{index}] action 必须是 BUY、SELL 或 HOLD")
        target_weight = item.get("target_weight", 0)
        try:
            target_weight = float(target_weight)
        except (TypeError, ValueError):
            target_weight = 0.0
        confidence = item.get("confidence", 0)
        try:
            confidence = float(confidence)
        except (TypeError, ValueError):
            confidence = 0.0
        decisions.append({
            "symbol": symbol,
            "action": action,
            "target_weight": min(1.0, max(0.0, target_weight)),
            "confidence": min(1.0, max(0.0, confidence)),
            "reason": str(item.get("reason", ""))[:500],
            "decision_id": f"dsh-{index + 1}",
        })
    return decisions


def _sanitize_label(value: Any) -> str:
    label = re.sub(r"[^A-Za-z0-9_-]", "", str(value or "").strip())[:40]
    return label or "dsh"


def _allowed_symbols(market: str, holdings: Sequence[str]) -> List[str]:
    selected: List[str] = []
    try:
        from engine.screening import latest_screening

        cached = latest_screening(market)
        if isinstance(cached, Mapping):
            selected = [str(symbol) for symbol in cached.get("selected_symbols", [])]
    except Exception:
        logger.debug("screening cache unavailable for allowed-universe", exc_info=True)
    defaults = [str(symbol) for symbol in (cfg.optimizer.get("default_symbols") or {}).get(market, [])]
    seen: Dict[str, None] = {}
    for symbol in [*holdings, *selected, *defaults]:
        normalized = str(symbol).strip().upper()
        if normalized:
            seen[normalized] = None
    return list(seen)


def _fetch_prices(symbols: Sequence[str]) -> tuple[Dict[str, float], Dict[str, str], Dict[str, str]]:
    from engine.data import fetcher

    prices: Dict[str, float] = {}
    errors: Dict[str, str] = {}
    names: Dict[str, str] = {}
    for symbol in symbols:
        try:
            payload = fetcher.snapshot(symbol)
            realtime = payload.get("realtime", {}) if isinstance(payload, Mapping) else {}
            price = realtime.get("price")
            if isinstance(price, (int, float)):
                prices[symbol] = float(price)
            else:
                errors[symbol] = "行情缺失现价"
            name = realtime.get("name")
            if name:
                names[symbol] = str(name)[:60]
        except Exception as exc:  # noqa: BLE001 - per-symbol degradation
            errors[symbol] = str(exc)[:200]
    return prices, errors, names


def _write_audit(payload: Mapping[str, Any]) -> Path:
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    stamp = _now().strftime("%Y%m%d-%H%M%S")
    path = AUDIT_DIR / f"{stamp}-{payload['market']}-{payload['label']}.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(dict(payload), ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)
    return path


def submit_decisions(
    market: str,
    decisions: Any,
    *,
    label: str = "dsh-manual",
    note: str = "",
    requested_by: str = "dsh",
    now: Optional[datetime] = None,
    progress_callback: Optional[Callable[[str], None]] = None,
) -> Dict[str, Any]:
    from engine.investment.mandate import effective_configs, get_mandate
    from engine.portfolio import account as account_store
    from engine.trading import control

    normalized = str(market or "").strip().lower()
    if normalized not in {"cn", "hk", "us", "etf"}:
        raise ValueError("market 必须是 cn、hk、us 或 etf")
    market = normalized
    label = _sanitize_label(label)
    current = _now(now)

    if str(cfg.trading.get("mode", "paper")).strip().lower() != "paper":
        raise RuntimeError("决策执行只允许 trading.mode=paper；实盘账户不会被修改")
    state = control.load_state()
    if state.get("kill_switch"):
        raise RuntimeError("紧急停止已激活：拒绝一切决策执行，必须先 reset_kill 解除")
    if state.get("paused"):
        logger.warning("[DECISIONS:%s] engine is paused; manual submission still proceeds", market)

    def progress(message: str) -> None:
        if progress_callback is not None:
            progress_callback(message)

    progress("校验决策清单…")
    normalized_decisions = _normalize_decisions(decisions)
    mandate = get_mandate()
    auto, trading, market_config = effective_configs(
        cfg.autonomous, cfg.trading, cfg.market_config(market), mandate
    )
    account_data = account_store.account(market)
    holdings = [
        str(holding.get("code", "")).strip().upper()
        for holding in account_data.get("holdings", [])
        if holding.get("code")
    ]
    allowed = _allowed_symbols(market, holdings)
    unknown = [item["symbol"] for item in normalized_decisions if item["symbol"] not in allowed]
    if unknown:
        raise ValueError(
            f"标的超出允许池（持仓 ∪ 最新选股 ∪ 配置默认标的）：{', '.join(unknown[:10])}"
        )

    progress("获取行情…")
    symbols = list(dict.fromkeys([item["symbol"] for item in normalized_decisions] + holdings))
    prices, market_data_errors, security_names = _fetch_prices(symbols)
    missing_price = [item["symbol"] for item in normalized_decisions if item["symbol"] not in prices]
    if missing_price:
        raise ValueError(f"以下标的前置行情缺失，拒绝执行：{', '.join(missing_price[:10])}")

    progress("硬风控重算仓位…")
    from engine.trading.risk import build_orders

    risk = build_orders(
        normalized_decisions,
        account=account_data,
        prices=prices,
        allowed_symbols=allowed,
        market_config=market_config,
        autonomous_config=auto,
        trading_config=trading,
        now=current,
    )

    progress("模拟撮合…")
    from engine.trading.broker import execute_orders

    execution = execute_orders(
        market,
        risk.get("orders", []),
        market_config=market_config,
        trading_mode="paper",
        now=current,
        equity_snapshot=risk.get("equity"),
        mark_prices=prices,
        security_names=security_names,
    )

    payload = {
        "command": "submit_decisions",
        "market": market,
        "label": label,
        "note": str(note)[:2000],
        "requested_by": str(requested_by)[:80],
        "generated_at": current.isoformat(timespec="seconds"),
        "decisions": normalized_decisions,
        "mandate": mandate,
        "allowed_universe": allowed,
        "prices": {key: value for key, value in prices.items()},
        "market_data_errors": market_data_errors,
        "risk": risk,
        "execution": execution,
    }
    audit_path = _write_audit(payload)

    report_shape = {
        "screening": {"selected_symbols": []},
        "account_before": {"holdings": [{"code": code} for code in holdings]},
        "chair": {"decisions": normalized_decisions},
        "execution": execution,
        "mandate": mandate,
        "status": "generated",
    }
    report_content = str(note).strip() or "本轮决策已由 DSH 会话提交并经引擎硬风控执行。"
    report_path = None
    notification = {"status": "skipped"}
    try:
        from engine.scheduler import REPORT_DIR, _decision_section, _deliver_completed_report, _write_report

        report_dir = REPORT_DIR
        date = current.strftime("%Y%m%d")
        report_path = report_dir / f"{date}-{market}-{label}.md"
        title = f"{market.upper()} {label} 决策执行报告"
        _write_report(report_path, title, report_content + "\n\n" + _decision_section(report_shape), current, catch_up=False)
        notification = _deliver_completed_report(report_path, title, report_content + "\n\n" + _decision_section(report_shape), market, label)
    except Exception as exc:  # noqa: BLE001 - report/notify must not roll back fills
        logger.exception("[DECISIONS:%s] report/notification failed", market)

    reflection = {"status": "skipped"}
    try:
        from engine.investment.reflection import InvestmentReflectionService

        service = InvestmentReflectionService()
        service.evaluate_pending(market)
        reflection = service.reflect_cycle(
            {
                "status": "generated",
                "market": market,
                "report": str(report_path) if report_path else "",
                "autonomous": report_shape,
            },
            mandate=mandate,
            trigger="dsh-manual",
        )
    except Exception as exc:  # noqa: BLE001
        logger.exception("[DECISIONS:%s] reflection failed", market)
        reflection = {"status": "error", "error": str(exc)}

    fills = execution.get("fills", [])
    rejected = list(risk.get("rejected", [])) + list(execution.get("rejected", []))
    return {
        "ok": True,
        "status": "generated",
        "market": market,
        "label": label,
        "mandate": mandate,
        "risk": {
            "orders": risk.get("orders", []),
            "rejected": risk.get("rejected", []),
            "protective_decisions": risk.get("protective_decisions", []),
            "circuit_breaker": risk.get("circuit_breaker", False),
            "equity": risk.get("equity"),
        },
        "execution": execution,
        "rejected": rejected,
        "fills": fills,
        "audit_file": str(audit_path),
        "report": str(report_path) if report_path else None,
        "notification": notification,
        "reflection": reflection,
        "market_data_errors": market_data_errors,
    }
