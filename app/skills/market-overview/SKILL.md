---
name: market-overview
description: 生成多市场/单市场概览：账户状态、各市场交易时段、最新选股、最新轮次报告与宏观日报的汇总。适用「今天市场怎么样」「给个 A 股概览」「看看整体情况」等请求。
whenToUse: 用户要求市场或系统整体概览、开盘前/收盘后复盘概览时加载。
---

# 市场概览（Market Overview）

## 目标

快速汇总引擎中的真实状态与最近产出，形成一页式概览，不虚构任何行情或成交。

## 数据获取

1. `investment_status` —— 模式（手动/自动）、暂停/紧急停止、当前策略、各市场交易时段与下一轮次。
2. `investment_macro_latest` —— 宏观日报（政策、日历、全球市场）。
3. `investment_screening(market)` / `investment_run_screening(market)` —— 各市场最新选股池（缓存缺失时说明，是否刷新询问用户）。
4. `investment_report_latest(market)` —— 各市场最近轮次报告。
5. `investment_portfolio(market)` —— 各市场账户现金、持仓。
6. 实时行情点：用 `investment_market_snapshot` 查看用户点名的指数/龙头/持仓。

## 输出结构

```text
## 市场概览 <日期 时间>
### 系统状态
- 模式：手动/自动；暂停：是/否；紧急停止：是/否
- 策略：保守/中立/激进
### 宏观摘要
…
### 分市场
| 市场 | 交易时段 | 下一轮次 | 最近报告 | 账户现金 | 持仓数 |
…
### 选股池与亮点
…
### 风险提示
…
```

对任何拿不到的部分写明"暂无数据"，不猜测。若用户想深入了解某一市场，转入 security-analysis 或 portfolio-review 流程。
