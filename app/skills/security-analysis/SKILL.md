---
name: security-analysis
description: 对单只或多只证券做完整多角色投资分析：识别证券身份后直接进入固定分析流程（四类基础研究、多空辩论、研究经理、交易员、组合草案、三方风险辩论、风险经理、最终经理），轮询进度并总结结果。适用「分析一下中芯国际」「对某只股票进行完整分析」等请求。
whenToUse: 用户点名具体证券并要求分析、评估买卖持有或解释走势事件时加载。
---

# 证券分析（Security Analysis）

## 目标

用户点名的证券**只走固定分析流程**：本技能不自行调用行情/新闻工具重写分析——`investment_analysis_workflow` 是唯一可信的完整分析实现。窗口 AI 的职责只是识别意图、确认证券身份、提取 symbols、启动固定流程、轮询进度、总结结果。

## 流程

### 1. 确认证券身份
- 用户给出名称/含糊代码时，用 `investment_security_search(keyword)` 确认代码与市场归属（A股/港股/美股/ETF 代码规则不同）。
- 用户指定市场时按其市场解析；无法唯一确认时用 ask_user_question 请用户确认。

### 2. 启动固定分析流程
```text
investment_analysis_workflow(
  market="<cn|hk|us|etf>",
  symbols=["<代码1>", "<代码2>"],
  symbols_source="user",
  submit=false
)
```
- symbols 只放用户点名的证券；**不得**把选股池或持仓中的其他标的混入。
- 持仓信息只作为组合上下文，绝不擅自扩大目标列表。
- 该调用在手动对话中是启动器，会立即返回 cycle_id。记住返回的 cycle_id。

### 3. 轮询进度
```text
investment_analysis_status(cycle_id="<返回的 cycle_id>")
```
- 状态为 running 时说明仍在执行，稍后再次轮询；**不要重复调用 investment_analysis_workflow**（同一 cycle_id 只会启动一个轮次）。
- 状态为 **ready_for_execution**（分析完成，等待批准）或 failed 时停止轮询。

### 4. 总结结果
- 复述最终决策清单（BUY/SELL/HOLD、目标权重、置信度、依据）、研究失败标的的故障安全处理（强制 HOLD / 排除）与风险提示。
- 数据缺口、拒绝原因必须如实说明，不得编造成交。

## 铁律

- 分析本身不产生任何交易；引擎紧急停止（kill）激活时不得提交任何决策。
- 若用户随后明确批准交易，把固定流程的最终决策**原样**提交：
```text
investment_submit_decisions(market=..., decisions=<流程最终决策原样>, idempotency_key="<同一 cycle_id>")
```
- 引擎校验提交内容与该轮记录的决策指纹完全一致，不一致直接拒绝（不得自行改动任何字段）。
- 同一 cycle_id 只成交一次；超时重试返回原结果，不得更换幂等键重试绕过。
- 失败（failed）轮次可用同一 cycle_id 重新调用 investment_analysis_workflow 安全重试，从已有检查点继续。
