---
name: stock-screening
description: 全市场选股（固定分析流程之前的独立块）：查看或刷新选股池、按用户条件在池内/持仓中筛选候选、输出标准化股票列表，并把结果作为 symbols 传入固定分析流程。适用「帮我选几只 A 股」「筛选低估值蓝筹」「刷新选股池」等请求。
whenToUse: 用户要求选股、刷新候选池、按条件筛选标的时加载。
---

# 选股（Stock Screening）

## 定位

选股是完整投资周期的**块 1**，只产生候选名单，不直接分析个股、不直接下单。用户要求「选股并完整分析」时：先完成本块输出标准化列表，再把列表传入 `investment_analysis_workflow`（见下）。

## 流程

1. 查看缓存：`investment_screening(market)`。
   - 缓存为空或用户要求最新：调用 `investment_run_screening(market)` 刷新（数十秒，先告知用户）。
2. 理解筛选口径：引擎先排除 ST/退市、特殊证券、低价低流动性、过小市值、极端涨跌与超估值标的，再按因子评分排序。
3. 若用户提出个性化条件（行业、风格、市值区间等），在候选池内二次过滤，无法满足时明确说明并给出最接近结果。
4. 输出**标准化股票列表**：
```text
## 选股结果（<市场>，<筛选时间>）
| 代码 | 名称 | 现价 | 因子亮点 | 事件/风险 |
…
### 排除说明
…
```

## 进入分析块（用户要求时）

把标准化列表的代码作为 symbols 传入固定流程，来源标注为 screening：
```text
investment_analysis_workflow(market=..., symbols=["<代码1>", ...], symbols_source="screening", submit=false)
```
随后按 complete-investment-cycle 的块 2 轮询 `investment_analysis_status(cycle_id=...)` 并总结。

## 铁律
- 选股只产生候选名单；不直接下单。
- 引擎紧急停止（kill）激活时说明并停止。
- 不虚构筛选依据；引擎返回什么就报告什么。
