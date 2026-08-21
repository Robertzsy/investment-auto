"""Deterministic acceptance evaluation for the top-level Skill selector."""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.manager.skill_selector import SkillSelector


CASES = [
    ("今天的A股行情怎么样", "market-overview"),
    ("今日大盘表现如何", "market-overview"),
    ("A股市场涨跌家数", "market-overview"),
    ("给我A股市场概览", "market-overview"),
    ("分析一下贵州茅台", "security-analysis"),
    ("英伟达现在贵不贵", "security-analysis"),
    ("对比腾讯和阿里的估值", "security-analysis"),
    ("看看 AAPL 最新行情", "security-analysis"),
    ("研究一下宁德时代基本面", "security-analysis"),
    ("苹果最近有什么公司新闻", "security-analysis"),
    ("比较 MSFT 和 GOOGL", "security-analysis"),
    ("腾讯现在股价怎么样", "security-analysis"),
    ("分析沪深300ETF", "security-analysis"),
    ("茅台的主要风险是什么", "security-analysis"),
    ("NVDA valuation analysis", "security-analysis"),
    ("compare TSLA and BYD", "security-analysis"),
    ("AAPL latest quote", "security-analysis"),
    ("这只证券技术面如何", "security-analysis"),
    ("哪只更值得长期关注，腾讯还是阿里", "security-analysis"),
    ("今天有什么值得关注的股票", "stock-screening"),
    ("运行一次 A 股选股", "stock-screening"),
    ("刷新美股候选池", "stock-screening"),
    ("查看港股筛选结果", "stock-screening"),
    ("给我当前候选股票", "stock-screening"),
    ("看看 ETF 选股评分", "stock-screening"),
    ("帮我查看当前持仓", "portfolio-review"),
    ("分析美股组合风险", "portfolio-review"),
    ("账户里还有多少现金", "portfolio-review"),
    ("当前仓位怎么样", "portfolio-review"),
    ("做一次持仓收益归因", "portfolio-review"),
    ("检查 A 股账户", "portfolio-review"),
    ("看看我的资产配置", "portfolio-review"),
    ("组合最近回撤如何", "portfolio-review"),
    ("运行美股组合优化器", "portfolio-optimization"),
    ("计算最优权重", "portfolio-optimization"),
    ("优化当前仓位", "portfolio-optimization"),
    ("做一次资产配置优化", "portfolio-optimization"),
    ("给 ETF 组合做优化", "portfolio-optimization"),
    ("运行一次美股完整投资", "complete-investment-cycle"),
    ("开始 A 股模拟投资", "complete-investment-cycle"),
    ("执行一轮港股交易", "complete-investment-cycle"),
    ("跑一轮 ETF", "complete-investment-cycle"),
    ("开始美股交易", "complete-investment-cycle"),
    ("进行一次完整周期", "complete-investment-cycle"),
    ("自动投资 A 股", "complete-investment-cycle"),
    ("完成一次模拟投资", "complete-investment-cycle"),
    ("暂停投资 Agent", "account-management"),
    ("恢复自动运行", "account-management"),
    ("查看当前运行状态", "account-management"),
    ("把美股模拟账户恢复初始资金", "account-management"),
    ("切换为自动模式", "account-management"),
    ("现在有哪些 skill", "system-administration"),
    ("查看 Harness 能力目录", "system-administration"),
    ("列出 action 列表", "system-administration"),
    ("修复数据缺口", "incident-repair"),
    ("回放验证最近失败的执行", "incident-repair"),
]


def main() -> int:
    selector = SkillSelector()
    rows = []
    for request, expected in CASES:
        try:
            actual = selector.select(request).manifest.name
            error = ""
        except Exception as exc:
            actual = ""
            error = str(exc)
        rows.append({
            "request": request,
            "expected": expected,
            "actual": actual,
            "passed": actual == expected,
            "error": error,
        })
    passed = sum(1 for row in rows if row["passed"])
    result = {
        "passed": passed,
        "total": len(rows),
        "pass_rate": passed / len(rows),
        "failures": [row for row in rows if not row["passed"]],
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if passed == len(rows) else 1


if __name__ == "__main__":
    raise SystemExit(main())
