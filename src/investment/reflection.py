from __future__ import annotations

from typing import Any, Dict, Mapping, Optional

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
        }
        return self.store.append("investment_reflections", record)

    def recent(self, market: str = "", limit: int = 5) -> list[Dict[str, Any]]:
        filters = {"market": market} if market else None
        return self.store.recent("investment_reflections", limit=limit, filters=filters)
