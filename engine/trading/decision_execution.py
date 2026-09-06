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
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence
from zoneinfo import ZoneInfo

from engine.config import cfg
from engine.paths import runtime_dir

logger = logging.getLogger("investment-auto.decisions")
AUDIT_DIR = runtime_dir() / "trading" / "audit"
IDEMPOTENCY_DIR = runtime_dir() / "trading" / "idempotency"
_idempotency_lock = threading.RLock()


def _idempotency_path(key: str) -> Path:
    return IDEMPOTENCY_DIR / f"{key}.json"


def _read_idempotency(key: str) -> Optional[Dict[str, Any]]:
    path = _idempotency_path(key)
    if not path.exists():
        return None
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    return payload if isinstance(payload, dict) else None


def _write_idempotency(key: str, payload: Mapping[str, Any]) -> None:
    IDEMPOTENCY_DIR.mkdir(parents=True, exist_ok=True)
    path = _idempotency_path(key)
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    temporary.replace(path)


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


def decision_fingerprint(market: str, decisions: Any) -> str:
    """Canonical content fingerprint of one logical submission.

    Engine-computed over the normalized decision shape (symbol/action/
    weight/confidence), sorted by symbol so harmless reordering does not
    change the identity. Both the analysis run's ready state and the
    submission claim store this value; a mismatch is always rejected.
    """
    import hashlib

    normalized = _normalize_decisions(decisions) if isinstance(decisions, list) else []
    canonical = {
        "market": str(market or "").strip().lower(),
        "decisions": sorted(
            (
                {
                    "symbol": item["symbol"],
                    "action": item["action"],
                    "target_weight": round(float(item.get("target_weight", 0)), 6),
                    "confidence": round(float(item.get("confidence", 0)), 6),
                }
                for item in normalized
            ),
            key=lambda row: row["symbol"],
        ),
    }
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:32]


def _record_rejected(key: str, error: str, *, fingerprint: str, market: str, label: str) -> None:
    """Terminal rejection record: a retry replays the same rejection."""
    with _idempotency_lock:
        _write_idempotency(key, {
            "status": "rejected",
            "market": market,
            "label": label,
            "fingerprint": fingerprint,
            "error": str(error)[:1000],
            "rejected_at": _now().isoformat(timespec="seconds"),
        })


def _release_claim(key: str) -> None:
    """Drop a pre-broker claim so a later retry may proceed (transient path)."""
    with _idempotency_lock:
        path = _idempotency_path(key)
        try:
            if path.exists():
                path.unlink()
        except OSError:
            logger.warning("[DECISIONS] failed to release claim %s", key)


def submit_decisions(
    market: str,
    decisions: Any,
    *,
    label: str = "dsh-manual",
    note: str = "",
    requested_by: str = "dsh",
    idempotency_key: str = "",
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
    dedupe_key = re.sub(r"[^A-Za-z0-9_.-]+", "-", str(idempotency_key or "")).strip("-")[:120]
    if not dedupe_key:
        raise ValueError("缺少 idempotency_key：每次提交必须携带稳定幂等键（分析轮次用其 cycle_id）")
    # Content fingerprint: a malformed decision list is a DETERMINISTIC
    # content rejection — record it as terminal so the same key cannot be
    # reused for a different (later corrected) payload.
    try:
        fingerprint = decision_fingerprint(market, decisions)
    except ValueError as exc:
        with _idempotency_lock:
            _write_idempotency(dedupe_key, {
                "status": "rejected",
                "market": market,
                "label": label,
                "fingerprint": "",
                "error": str(exc)[:1000],
                "rejected_at": current.isoformat(timespec="seconds"),
            })
        raise

    def progress(message: str) -> None:
        if progress_callback is not None:
            progress_callback(message)

    # ── idempotency ledger: replay / claim / reject ─────────────────────────
    with _idempotency_lock:
        recorded = _read_idempotency(dedupe_key)
        if recorded is not None:
            status = recorded.get("status")
            if status == "completed":
                if recorded.get("fingerprint") not in {"", fingerprint}:
                    raise ValueError("同一幂等键已对应不同的决策内容，拒绝重放（请更换幂等键）")
                logger.info("[DECISIONS:%s] idempotent replay of key %s", market, dedupe_key)
                return {**dict(recorded.get("result") or {}), "duplicate": True, "idempotency_key": dedupe_key}
            if status == "rejected":
                raise RuntimeError("该幂等键的提交已被终态拒绝：" + str(recorded.get("error") or "（无记录原因）"))
            # in_progress: a claim. A live duplicate is refused; a stale claim
            # (the previous attempt died before the broker) is reclaimed, but
            # only for the SAME content.
            claimed_at = str(recorded.get("claimed_at") or "")
            try:
                claimed = datetime.fromisoformat(claimed_at)
            except ValueError:
                claimed = None
            if claimed is not None and (datetime.now(claimed.tzinfo) - claimed).total_seconds() < 600:
                raise RuntimeError("同一幂等键的提交正在进行中，请查询其状态而不是重新提交")
            if recorded.get("fingerprint") not in {"", fingerprint}:
                _record_rejected(dedupe_key, "同一幂等键先前提交的决策内容不同，拒绝复用", fingerprint=fingerprint, market=market, label=label)
                raise ValueError("同一幂等键先前提交的决策内容不同，拒绝复用（请更换幂等键）")
        _write_idempotency(dedupe_key, {
            "status": "in_progress",
            "market": market,
            "label": label,
            "fingerprint": fingerprint,
            "claimed_at": current.isoformat(timespec="seconds"),
        })

    # ── deterministic gates (terminal rejections, no claim left behind) ────
    if str(cfg.trading.get("mode", "paper")).strip().lower() != "paper":
        _record_rejected(dedupe_key, "决策执行只允许 trading.mode=paper；实盘账户不会被修改", fingerprint=fingerprint, market=market, label=label)
        raise RuntimeError("决策执行只允许 trading.mode=paper；实盘账户不会被修改")
    try:
        normalized_decisions = _normalize_decisions(decisions)
    except ValueError as exc:
        _record_rejected(dedupe_key, str(exc), fingerprint=fingerprint, market=market, label=label)
        raise

    # ── transient gates (claim released, retry allowed) ────────────────────
    state = control.load_state()
    if state.get("kill_switch"):
        _release_claim(dedupe_key)
        raise RuntimeError("紧急停止已激活：拒绝一切决策执行，必须先 reset_kill 解除")
    if state.get("paused"):
        logger.warning("[DECISIONS:%s] engine is paused; manual submission still proceeds", market)

    progress("校验决策清单…")
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

    # ── run binding: a submission carrying an analysis cycle id must be that
    # run's own ready decisions; only then may its analyzed targets enter the
    # allowed universe. Standalone submissions stay inside the classic pool. ──
    bound_run = None
    try:
        from engine import analysis_runs

        bound_run = analysis_runs.get(dedupe_key)
    except Exception:  # noqa: BLE001 - binding is an enhancement, not a hard dependency
        bound_run = None
    allowed = _allowed_symbols(market, holdings)
    if bound_run is not None:
        if bound_run.get("status") != "ready_for_execution":
            _record_rejected(dedupe_key, "分析轮次尚未就绪（状态必须为 ready_for_execution）或已经执行完成", fingerprint=fingerprint, market=market, label=label)
            raise RuntimeError("分析轮次尚未就绪：请等待固定分析流程到达 ready_for_execution 后再提交")
        run_fingerprint = str(bound_run.get("decision_fingerprint") or "")
        if run_fingerprint != fingerprint:
            _record_rejected(dedupe_key, "提交的决策与分析轮次记录的最终决策不一致，拒绝执行", fingerprint=fingerprint, market=market, label=label)
            raise ValueError("提交的决策与分析轮次记录的最终决策不一致，拒绝执行（请原样提交该轮最终决策）")
        for symbol in [str(item).strip().upper() for item in bound_run.get("symbols", [])]:
            if symbol and symbol not in allowed:
                allowed.append(symbol)
    unknown = [item["symbol"] for item in normalized_decisions if item["symbol"] not in allowed]
    if unknown:
        _record_rejected(dedupe_key, f"标的超出允许池（持仓 ∪ 最新选股 ∪ 配置默认标的 ∪ 已分析目标）：{', '.join(unknown[:10])}", fingerprint=fingerprint, market=market, label=label)
        raise ValueError(
            f"标的超出允许池（持仓 ∪ 最新选股 ∪ 配置默认标的 ∪ 已分析目标）：{', '.join(unknown[:10])}"
        )

    progress("获取行情…")
    symbols = list(dict.fromkeys([item["symbol"] for item in normalized_decisions] + holdings))
    prices, market_data_errors, security_names = _fetch_prices(symbols)
    missing_price = [item["symbol"] for item in normalized_decisions if item["symbol"] not in prices]
    if missing_price:
        # Transient data gap: release the claim so the same key can retry.
        _release_claim(dedupe_key)
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
        idempotency_key=dedupe_key,
        decision_fingerprint=fingerprint,
    )

    # From here the broker receipt already owns durability: nothing below may
    # fail the submission (audit/report/reflection are auxiliary records).
    payload = {
        "command": "submit_decisions",
        "market": market,
        "label": label,
        "note": str(note)[:2000],
        "requested_by": str(requested_by)[:80],
        "generated_at": current.isoformat(timespec="seconds"),
        "decisions": normalized_decisions,
        "idempotency_key": dedupe_key,
        "decision_fingerprint": fingerprint,
        "mandate": mandate,
        "allowed_universe": allowed,
        "prices": {key: value for key, value in prices.items()},
        "market_data_errors": market_data_errors,
        "risk": risk,
        "execution": execution,
    }
    audit_path: Optional[Path] = None
    try:
        audit_path = _write_audit(payload)
    except Exception:  # noqa: BLE001 - the receipt, not the audit, is the durability point
        logger.exception("[DECISIONS:%s] audit write failed (receipt remains authoritative)", market)

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
    result = {
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
        "audit_file": str(audit_path) if audit_path is not None else None,
        "report": str(report_path) if report_path else None,
        "notification": notification,
        "reflection": reflection,
        "market_data_errors": market_data_errors,
        "idempotency_key": dedupe_key,
        "decision_fingerprint": fingerprint,
    }
    if execution.get("replayed"):
        result["recovered"] = True
    with _idempotency_lock:
        _write_idempotency(dedupe_key, {
            "status": "completed",
            "market": market,
            "label": label,
            "fingerprint": fingerprint,
            "audit_file": str(audit_path) if audit_path is not None else None,
            "result": result,
        })

    # Bound runs transition to their terminal state with the execution truth.
    if bound_run is not None and bound_run.get("status") == "ready_for_execution":
        try:
            from engine import analysis_runs

            analysis_runs.finish({
                "cycle_id": dedupe_key,
                "decisions": normalized_decisions,
                "execution": execution,
                "report": str(report_path) if report_path else None,
                "audit_file": str(audit_path) if audit_path is not None else None,
            })
        except Exception:  # noqa: BLE001 - the trade is already durable in the receipt
            logger.exception("[DECISIONS:%s] bound analysis run finalize failed", market)
    return result
