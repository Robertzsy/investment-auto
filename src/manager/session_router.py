from __future__ import annotations

import re
from typing import Optional

from src.manager.skill_models import SESSION_SCOPES


_ADMIN = re.compile(r"(?:创建|新建|修改|安装|卸载|配置|代码|源码|skill|tool|供应商|api\s*key)", re.I)
_EXECUTION = re.compile(
    r"(?:完整投资|自动投资|运行一轮|执行一轮|开始投资|开始交易|模拟下单|买入|卖出|调仓|重置.*账户)",
    re.I,
)
_PORTFOLIO = re.compile(r"(?:持仓|组合|仓位|账户|资金|回撤|优化器|资产配置|收益归因)", re.I)
_RESEARCH = re.compile(r"(?:分析|研究|估值|行情|股价|证券|股票|基金|ETF|对比|新闻|基本面|技术面|情绪|选股)", re.I)
_CONTINUATION = re.compile(r"^(?:继续|接着|再看|它|这个|刚才|上一只|上一个)", re.I)


class SessionRouter:
    """Route a request before capability selection, so each domain has a stable context."""

    def route(
        self,
        request: str,
        *,
        skill_scope: str = "",
        last_scope: Optional[str] = None,
    ) -> str:
        if skill_scope:
            if skill_scope not in SESSION_SCOPES:
                raise ValueError(f"未知会话范围: {skill_scope}")
            return skill_scope
        text = str(request or "").strip()
        if _CONTINUATION.search(text) and last_scope in SESSION_SCOPES:
            return str(last_scope)
        if _ADMIN.search(text):
            return "system_admin"
        if _EXECUTION.search(text):
            return "investment_execution"
        if _PORTFOLIO.search(text):
            return "portfolio_management"
        if _RESEARCH.search(text):
            return "investment_research"
        return str(last_scope) if last_scope in SESSION_SCOPES else "investment_research"
