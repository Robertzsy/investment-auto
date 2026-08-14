from __future__ import annotations

from typing import Any, Mapping


def format_cycle_result(result: Mapping[str, Any]) -> str:
    market = str(result.get("market", "")).lower()
    names = {"cn": "A 股", "hk": "港股", "us": "美股", "etf": "ETF"}
    autonomous = result.get("autonomous", {}) if isinstance(result.get("autonomous"), Mapping) else {}
    status = str(result.get("status", ""))
    autonomous_status = str(autonomous.get("status", ""))
    fills = autonomous.get("fills", []) if isinstance(autonomous, Mapping) else []
    completed = status == "generated" and autonomous_status in {"executed", "no_trade"}
    lines = [f"## {'✅' if completed else '⚠️'} {names.get(market, market.upper())}完整投资轮次"]
    if status:
        lines.append(f"- **报告状态**：{status}")
    mandate = result.get("mandate", autonomous.get("mandate", {}))
    if isinstance(mandate, Mapping) and mandate.get("display_name"):
        lines.append(f"- **策略授权书**：{mandate.get('display_name')}（{mandate.get('risk_policy_version', '')}）")
    labels = {
        "executed": "已完成分析、风控与模拟执行",
        "no_trade": "已完成分析与风控，本轮没有可执行订单",
        "paused": "运行时安全暂停，未提交模拟订单",
        "disabled": "自主投资总开关未启用",
        "blocked": "被安全边界阻止",
        "error": "投资流程发生错误",
    }
    lines.append(f"- **投资执行状态**：{labels.get(autonomous_status, autonomous_status or '未知')}")
    reason = autonomous.get("error") or autonomous.get("reason")
    control = autonomous.get("control") if isinstance(autonomous.get("control"), Mapping) else {}
    if not reason and autonomous_status == "paused":
        reason = control.get("reason")
    if reason:
        lines.append(f"- **原因**：{reason}")
    lines.append(f"- **模拟成交数**：{len(fills) if isinstance(fills, list) else 0}")
    if result.get("report"):
        lines.append(f"- **完整报告**：`{result.get('report')}`")
    notification = result.get("notification", {})
    if isinstance(notification, Mapping):
        lines.append(f"- **报告推送**：{notification.get('status', '未知')}")
        if notification.get("reason"):
            lines.append(f"- **推送说明**：{notification.get('reason')}")
    lines.append("\n本轮为模拟研究与纸面交易，不构成投资建议。")
    return "\n".join(lines)

