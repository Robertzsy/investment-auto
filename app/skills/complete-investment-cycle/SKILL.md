---
name: complete-investment-cycle
description: 手动完成一整轮投资周期：选股→逐只研究→形成决策→用户批准→提交引擎（硬风控+纸面撮合）→读取报告。适用「跑一轮 A 股」「帮我完成一轮美股分析」等请求。
whenToUse: 用户要求执行完整投资轮次（研究+交易）时加载。
---

# 完整投资周期（Complete Investment Cycle）

## 目标

把一次完整轮次拆成可批准、可审计的阶段执行。**所有写操作（提交决策、重置账户、切策略）都必须在用户明确批准后进行。** 引擎的硬风控与纸面撮合不可绕过。

## 阶段

### 1. 准备与选股
- `investment_status`：确认模式与暂停/紧急停止状态；紧急停止激活时说明并停止。
- `investment_screening(market)`（必要时 `investment_run_screening(market)` 刷新）取得候选池。
- `investment_portfolio(market)` + 持仓 `investment_market_snapshot` 确定已有持仓状况。

### 2. 逐只研究
对候选池与持仓中的每只标的执行 security-analysis 的取数与框架（快照、历史、新闻），记录结论与置信度。数据缺口必须写明。

### 3. 形成决策清单
汇总为待提交决策：
```json
[{"symbol": "…", "action": "BUY|SELL|HOLD", "target_weight": 0.08, "confidence": 0.7, "reason": "…"}]
```
- target_weight 是组合权重目标，引擎会按授权书重新计算并硬封顶。
- 低于授权书 min_confidence 的决策会被引擎拒绝——提交前自查。
- 用 plan 模式或 ask_user 向用户呈现完整计划并等待批准。

### 4. 执行（批准后）
`investment_submit_decisions(market, decisions, label, note)`：
- 引擎自行取价、重算仓位、执行硬风控与纸面撮合；
- 返回成交/拒绝清单、审计文件与报告路径。
- 被拒原因（置信度不足、超单股上限、超换手、当日交易上限、T+1 可卖不足等）如实转述给用户，不要重试绕过。

### 5. 复盘
- `investment_report_latest(market)` 读取刚生成的轮次报告；
- 用 portfolio-review 的输出结构总结本轮变化与下一步。

## 铁律

- 引擎紧急停止（kill）激活时不得提交任何决策。
- 不虚构行情、成交或事件；引擎返回什么就报告什么。
- 失败后允许在下一轮修正，但绝不在同一轮反复重试同一笔被拒订单。
