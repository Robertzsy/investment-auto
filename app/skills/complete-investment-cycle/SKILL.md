---
name: complete-investment-cycle
description: 完成一整轮投资周期，固定分为两块：选股（产生标准化候选列表）与股票分析（把用户点名或选股得到的 symbols 传入固定分析流程）。分析完成后经用户批准再以幂等键提交引擎执行（硬风控+纸面撮合）。适用「跑一轮完整 A 股投资分析决策」「帮我选几只 A 股并完整分析」等请求。
whenToUse: 用户要求执行完整投资轮次（选股/研究+可能的交易）时加载。
---

# 完整投资周期（Complete Investment Cycle）

## 两块固定流程

分析流程明确分为两块，窗口 AI 只负责编排与总结，不自行重写分析：

### 块 1：选股（用户要求选股或未指定标的时执行）
1. `investment_status`：确认模式与暂停/紧急停止状态；紧急停止激活时说明并停止。
2. `investment_screening(market)` 读取缓存；需要最新时 `investment_run_screening(market)` 刷新（数十秒，先告知用户）。
3. 输出**标准化股票列表**：代码、名称、现价、因子亮点、事件/风险、排除说明。
4. 用户点名了具体证券时**跳过本块**，直接进入块 2。

### 块 2：股票分析（固定流程，唯一实现）
1. 目标 symbols = 用户点名列表（`symbols_source="user"`）或块 1 的选股结果（`symbols_source="screening"`）。**手动入口不允许空股票列表**：没有 symbols 时先完成块 1。
2. 启动固定流程：
```text
investment_analysis_workflow(market=..., symbols=[...], symbols_source=..., submit=false)
```
记住返回的 cycle_id。持仓信息只作为上下文，不得擅自扩大目标列表。
3. `investment_analysis_status(cycle_id=...)` 轮询直到 **ready_for_execution**（分析完成）或 failed；不要重复启动轮次。failed 时可用同一 cycle_id 重试。
4. 向用户总结最终决策、故障安全处理与风险提示。

## 执行（仅用户明确批准后）

```text
investment_submit_decisions(market=..., decisions=<固定流程最终决策原样>, idempotency_key="<cycle_id>")
```
- 引擎校验提交内容与该轮记录的**决策指纹**完全一致，不一致直接拒绝。
- 引擎自行取价、按授权书硬边界重算仓位、执行硬风控与纸面撮合；返回成交/拒绝清单、审计与报告。
- **幂等**：同一 cycle_id 只成交一次（成交回执与账户变更同一次原子落盘）；超时或重复调用返回原结果，绝不更换键重试绕过。
- 被拒原因（置信度不足、超单股上限、超换手、当日交易上限、T+1 可卖不足等）如实转述，不要重试绕过。

## 复盘
- `investment_report_latest(market)` 读取轮次报告；用 portfolio-review 的输出结构总结本轮变化与下一步。

## 铁律
- 引擎紧急停止（kill）激活时不得提交任何决策。
- 不虚构行情、成交或事件；引擎返回什么就报告什么。
- 分析本身不改变账户；未获用户本次明确授权绝不调用 investment_submit_decisions。
