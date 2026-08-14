from __future__ import annotations

import copy
import json
import threading
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Mapping, Optional
from zoneinfo import ZoneInfo


ROOT = Path(__file__).resolve().parents[2]
MANDATE_FILE = ROOT / "runtime" / "investment" / "mandate.json"
_lock = threading.RLock()


@dataclass(frozen=True)
class StrategyDefinition:
    profile: str
    display_name: str
    objective: str
    prompt: str
    max_total_position_pct: float
    min_cash_reserve_pct: float
    max_position_pct: float
    max_order_value_pct: float
    min_confidence: float
    max_cycle_turnover_pct: float
    max_orders_per_cycle: int
    max_daily_trades: int
    drawdown_reduce_pct: float
    max_drawdown_pct: float


STRATEGIES: Dict[str, StrategyDefinition] = {
    "conservative": StrategyDefinition(
        profile="conservative",
        display_name="保守策略",
        objective="优先保护本金和控制组合回撤，在证据高度一致时获取稳健收益",
        prompt=(
            "当前首要目标是控制本金损失和组合回撤，其次才是收益。优先选择流动性充足、"
            "基本面稳定、估值和波动相对合理且证据一致的标的；证据冲突、事件风险高或短期"
            "涨幅异常时优先观望。采用小仓位逐步确认，允许持有较高现金。"
        ),
        max_total_position_pct=55,
        min_cash_reserve_pct=45,
        max_position_pct=8,
        max_order_value_pct=3,
        min_confidence=0.75,
        max_cycle_turnover_pct=10,
        max_orders_per_cycle=2,
        max_daily_trades=4,
        drawdown_reduce_pct=-6,
        max_drawdown_pct=-8,
    ),
    "neutral": StrategyDefinition(
        profile="neutral",
        display_name="中立策略",
        objective="在可控回撤下取得稳定的风险调整后收益",
        prompt=(
            "综合基本面、趋势、估值、情绪与新闻催化，在风险和收益之间保持平衡。证据较强时"
            "建立适中仓位，证据增强时加仓，逻辑削弱时减仓，逻辑失效时卖出；兼顾行业分散、"
            "交易成本与资金利用率。"
        ),
        max_total_position_pct=75,
        min_cash_reserve_pct=25,
        max_position_pct=12,
        max_order_value_pct=5,
        min_confidence=0.65,
        max_cycle_turnover_pct=20,
        max_orders_per_cycle=3,
        max_daily_trades=6,
        drawdown_reduce_pct=-9,
        max_drawdown_pct=-12,
    ),
    "aggressive": StrategyDefinition(
        profile="aggressive",
        display_name="激进策略",
        objective="在严格风险预算内主动捕捉高增长、强趋势与重大催化机会",
        prompt=(
            "主动捕捉盈利加速、技术突破、行业景气变化和重大催化机会，可以接受较高波动与"
            "较快轮换。证据充分时可更快建仓，但必须给出失效条件和退出计划；高波动本身不是"
            "买入理由，数据质量、流动性和组合回撤仍是硬门槛。"
        ),
        max_total_position_pct=95,
        min_cash_reserve_pct=5,
        max_position_pct=20,
        max_order_value_pct=8,
        min_confidence=0.55,
        max_cycle_turnover_pct=40,
        max_orders_per_cycle=5,
        max_daily_trades=10,
        drawdown_reduce_pct=-14,
        max_drawdown_pct=-18,
    ),
}


def _now() -> str:
    return datetime.now(ZoneInfo("Asia/Shanghai")).isoformat(timespec="seconds")


def _default_payload(profile: str = "neutral") -> Dict[str, Any]:
    definition = STRATEGIES[profile]
    return {
        **asdict(definition),
        "risk_policy_version": f"{profile}-v1",
        "prompt_version": f"{profile}-v1",
        "selected_by": "system-default",
        "effective_from_cycle": "next",
        "updated_at": _now(),
        "version": 1,
    }


def get_mandate() -> Dict[str, Any]:
    with _lock:
        try:
            payload = json.loads(MANDATE_FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            payload = _default_payload()
        profile = str(payload.get("profile", "neutral"))
        if profile not in STRATEGIES:
            return _default_payload()
        # Code definitions are authoritative; the file stores the user choice and version.
        result = _default_payload(profile)
        result.update({key: value for key, value in payload.items() if key not in asdict(STRATEGIES[profile])})
        result.update(asdict(STRATEGIES[profile]))
        return result


def set_mandate(profile: str, *, selected_by: str = "user") -> Dict[str, Any]:
    normalized = str(profile or "").strip().lower()
    if normalized not in STRATEGIES:
        raise ValueError("策略必须是 conservative、neutral 或 aggressive")
    with _lock:
        previous = get_mandate()
        payload = _default_payload(normalized)
        payload.update({
            "selected_by": str(selected_by)[:80],
            "updated_at": _now(),
            "version": int(previous.get("version", 0)) + 1,
        })
        MANDATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        temporary = MANDATE_FILE.with_suffix(".json.tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(MANDATE_FILE)
        return payload


def effective_configs(
    autonomous: Mapping[str, Any],
    trading: Mapping[str, Any],
    market_config: Mapping[str, Any],
    mandate: Optional[Mapping[str, Any]] = None,
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Overlay the selected strategy's non-negotiable limits on runtime config.

    A user-selected mandate is authoritative. When no mandate file exists we
    preserve explicit runtime limits for backward compatibility and attach the
    neutral prompt/objective only; the first UI/API selection makes it fully
    authoritative.
    """
    selected = dict(mandate or get_mandate())
    auto = copy.deepcopy(dict(autonomous))
    trade = copy.deepcopy(dict(trading))
    market = copy.deepcopy(dict(market_config))
    explicitly_selected = MANDATE_FILE.exists() or str(selected.get("selected_by", "")) != "system-default"
    if not explicitly_selected:
        auto["strategy_profile"] = selected["profile"]
        auto["strategy_prompt"] = selected["prompt"]
        return auto, trade, market
    for key in (
        "max_total_position_pct", "max_position_pct", "max_order_value_pct",
        "min_confidence", "max_cycle_turnover_pct", "max_orders_per_cycle",
        "drawdown_reduce_pct",
    ):
        auto[key] = selected[key]
    trade["max_daily_trades"] = selected["max_daily_trades"]
    risk = market.setdefault("risk", {})
    risk["min_cash_reserve_pct"] = selected["min_cash_reserve_pct"]
    risk["max_drawdown_pct"] = selected["max_drawdown_pct"]
    return auto, trade, market
