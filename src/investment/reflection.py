from __future__ import annotations

from datetime import date, datetime
from typing import Any, Callable, Dict, Mapping, Optional

from src.platform.memory_store import StructuredMemoryStore


class InvestmentReflectionService:
    """Turns cycle outcomes into auditable lessons, never direct policy edits."""

    def __init__(self, store: Optional[StructuredMemoryStore] = None) -> None:
        self.store = store or StructuredMemoryStore()

    def reflect_cycle(
        self,
        result: Mapping[str, Any],
        *,
        mandate: Mapping[str, Any],
        trigger: str,
    ) -> Dict[str, Any]:
        autonomous = result.get("autonomous", result)
        if not isinstance(autonomous, Mapping):
            autonomous = {}
        execution = autonomous.get("execution", {})
        if not isinstance(execution, Mapping):
            execution = {}
        risk = autonomous.get("risk", {})
        if not isinstance(risk, Mapping):
            risk = {}
        errors = []
        for key in ("error", "reason"):
            value = autonomous.get(key) or result.get(key)
            if value:
                errors.append(str(value)[:1000])
        fills = execution.get("fills", []) if isinstance(execution.get("fills", []), list) else []
        rejected = risk.get("rejected", []) if isinstance(risk.get("rejected", []), list) else []
        decisions = []
        chair = autonomous.get("chair", {})
        if isinstance(chair, Mapping) and isinstance(chair.get("decisions"), list):
            decisions = [dict(item) for item in chair["decisions"] if isinstance(item, Mapping)]
        prices = autonomous.get("prices", {}) if isinstance(autonomous.get("prices"), Mapping) else {}
        lesson = (
            "本轮未完成，下一轮优先验证失败阶段和外部依赖。" if errors else
            "本轮没有成交；复核观望是否来自证据不足、置信度或硬风控。" if not fills else
            "本轮已产生模拟成交；后续按 T+1/T+5/T+20 评价预测与实际结果，避免即时盈亏归因。"
        )
        candidates = []
        if errors:
            candidates.append({"area": "reliability", "proposal": "优先修复失败阶段并增加同类错误验证", "requires": "test"})
        if not fills and rejected:
            candidates.append({"area": "decision_quality", "proposal": "复核目标仓位、置信度与硬风控拒绝原因的一致性", "requires": "historical_replay"})
        if fills:
            candidates.append({"area": "outcome_evaluation", "proposal": "在 T+1/T+5/T+20 对成交决策做延迟归因", "requires": "future_market_data"})
        record = {
            "kind": "immediate_cycle_reflection",
            "market": str(result.get("market", autonomous.get("market", ""))),
            "trigger": trigger,
            "strategy_profile": mandate.get("profile"),
            "strategy_version": mandate.get("risk_policy_version"),
            "cycle_status": result.get("status", autonomous.get("status")),
            "investment_status": autonomous.get("status"),
            "fills_count": len(fills),
            "rejected_count": len(rejected),
            "errors": errors,
            "lesson": lesson,
            "candidate_policy_changes": candidates,
            "policy_change_status": "requires_evaluation",
            "outcome_horizons": ["T+1", "T+5", "T+20"],
            "audit_file": autonomous.get("audit_file"),
            "report": result.get("report"),
            "decision_snapshot": [
                {
                    "symbol": str(item.get("symbol", "")).upper(),
                    "action": str(item.get("action", "HOLD")).upper(),
                    "confidence": item.get("confidence"),
                    "target_weight": item.get("target_weight"),
                    "reference_price": prices.get(str(item.get("symbol", "")).upper()),
                    "reason": str(item.get("reason", ""))[:500],
                }
                for item in decisions
                if item.get("symbol")
            ],
            "outcome_status": "pending",
        }
        return self.store.append("investment_reflections", record)

    def evaluate_pending(
        self,
        market: str = "",
        *,
        history_loader: Optional[Callable[..., Mapping[str, Any]]] = None,
        limit: int = 500,
    ) -> list[Dict[str, Any]]:
        """Evaluate prior decisions only after later market bars exist.

        Evaluation records are append-only.  T+1, T+5 and T+20 are added as
        they become available, so an early result can never overwrite its
        audit trail.  These evaluated records are the only reflections exposed
        to future Agents by :meth:`recent`.
        """
        if history_loader is None:
            from src.data.fetcher import history as history_loader

        filters = {"market": market} if market else None
        records = self.store.recent("investment_reflections", limit=limit, filters=filters)
        pending = [item for item in records if item.get("outcome_status") == "pending"]
        completed: Dict[str, set[str]] = {}
        for item in records:
            source_id = str(item.get("source_record_id", ""))
            if source_id and source_id not in completed:
                completed[source_id] = {
                    str(value) for value in item.get("evaluated_horizons", []) if value
                }

        created: list[Dict[str, Any]] = []
        for source in pending:
            source_id = str(source.get("record_id", ""))
            if not source_id:
                continue
            try:
                created_date = datetime.fromisoformat(str(source.get("created_at", ""))).date()
            except ValueError:
                continue
            outcomes: list[Dict[str, Any]] = []
            available_horizons: set[str] = set()
            for decision in source.get("decision_snapshot", []):
                if not isinstance(decision, Mapping):
                    continue
                symbol = str(decision.get("symbol", "")).upper()
                try:
                    reference_price = float(decision.get("reference_price"))
                except (TypeError, ValueError):
                    continue
                if not symbol or reference_price <= 0:
                    continue
                try:
                    payload = history_loader(symbol, lookback=90)
                except Exception:
                    continue
                future_rows = []
                for row in payload.get("data", []) if isinstance(payload, Mapping) else []:
                    if not isinstance(row, Mapping):
                        continue
                    try:
                        row_date = date.fromisoformat(str(row.get("date", ""))[:10])
                        close = float(row.get("close"))
                    except (TypeError, ValueError):
                        continue
                    if row_date > created_date and close > 0:
                        future_rows.append((row_date, close))
                future_rows.sort(key=lambda item: item[0])
                # A truncated history response must not mislabel an arbitrary
                # later quote as T+1.
                if future_rows and (future_rows[0][0] - created_date).days > 7:
                    continue
                action = str(decision.get("action", "HOLD")).upper()
                symbol_outcomes: Dict[str, Any] = {
                    "symbol": symbol,
                    "action": action,
                    "reference_price": reference_price,
                    "horizons": {},
                }
                for horizon, offset in (("T+1", 1), ("T+5", 5), ("T+20", 20)):
                    if len(future_rows) < offset:
                        continue
                    observed_date, close = future_rows[offset - 1]
                    observed_return = close / reference_price - 1.0
                    directional_score = (
                        observed_return if action == "BUY" else
                        -observed_return if action == "SELL" else None
                    )
                    symbol_outcomes["horizons"][horizon] = {
                        "date": observed_date.isoformat(),
                        "close": round(close, 6),
                        "observed_return": round(observed_return, 6),
                        "directional_score": None if directional_score is None else round(directional_score, 6),
                    }
                    available_horizons.add(horizon)
                if symbol_outcomes["horizons"]:
                    outcomes.append(symbol_outcomes)

            if not outcomes or available_horizons.issubset(completed.get(source_id, set())):
                continue
            scores = [
                horizon["directional_score"]
                for item in outcomes
                for horizon in item["horizons"].values()
                if horizon.get("directional_score") is not None
            ]
            average_score = sum(scores) / len(scores) if scores else None
            if average_score is None:
                lesson = "观望决策已有后续行情，但不以涨跌方向自动判定对错。"
            elif average_score >= 0:
                lesson = "已观察到的后续行情整体支持当时的买卖方向；继续等待更长周期验证。"
            else:
                lesson = "已观察到的后续行情整体不支持当时的买卖方向；后续研究应复核当时证据与反方意见。"
            created.append(self.store.append("investment_reflections", {
                "kind": "delayed_outcome_reflection",
                "market": source.get("market", ""),
                "source_record_id": source_id,
                "source_audit_file": source.get("audit_file"),
                "strategy_profile": source.get("strategy_profile"),
                "strategy_version": source.get("strategy_version"),
                "outcome_status": "evaluated",
                "evaluated_horizons": sorted(available_horizons, key=("T+1", "T+5", "T+20").index),
                "outcomes": outcomes,
                "average_directional_score": None if average_score is None else round(average_score, 6),
                "lesson": lesson,
            }))
        return created

    def recent(self, market: str = "", limit: int = 5) -> list[Dict[str, Any]]:
        filters = {"market": market} if market else None
        records = self.store.recent("investment_reflections", limit=max(limit * 5, limit), filters=filters)
        # Only observed outcomes are lessons. Immediate process diagnostics may
        # remain visible in audit files but must not bias future market calls.
        evaluated = [item for item in records if item.get("outcome_status") == "evaluated"]
        # Keep only the newest incremental evaluation for each source cycle.
        unique: list[Dict[str, Any]] = []
        seen: set[str] = set()
        for item in evaluated:
            source_id = str(item.get("source_record_id") or item.get("record_id"))
            if source_id in seen:
                continue
            seen.add(source_id)
            unique.append(item)
        return unique[:limit]
