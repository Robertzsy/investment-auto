---
name: portfolio-optimization
description: 运行或解读组合优化：马科维茨、Black-Litterman、风险平价与压力测试，基于持仓与候选池给出目标权重方案。适用「优化我的组合」「帮我做下仓位再平衡」等请求。
whenToUse: 用户要求组合优化、仓位再平衡、风险预算配置时加载。
---

# 组合优化（Portfolio Optimization）

## 目标

用引擎的量化优化器生成**目标权重参考**，并与当前持仓对照给出再平衡建议。优化结果只作为建议；实际调仓必须经用户批准后通过 `investment_submit_decisions`（携带稳定 `idempotency_key`，同一键只成交一次）执行。

## 流程

1. 查看缓存结果：`investment_optimizer_latest(market)`。过期或缺失时说明（引擎侧由调度器收盘轮次自动运行优化器；需要立即计算时告知用户该能力由收盘轮次提供，或改用下方手动对照）。
2. 获取当前持仓 `investment_portfolio(market)` 与候选池 `investment_screening(market)`。
3. 对照解读：优化器输出包含多种方案（最大夏普、Black-Litterman、风险平价）与压力测试（VaR、最大回撤）。
4. 输出再平衡建议：把当前权重 vs 目标权重的差异列成表，考虑单股上限、现金储备与换手上限（`investment_mandate`），给出分批调整顺序（先减超配、后补欠配）。

## 输出

```text
## 组合优化（<市场>，<时间>）
### 推荐方案：<方案名>
| 代码 | 当前权重 | 目标权重 | 差异 | 建议动作 |
…
### 压力测试：VaR95 / 最大回撤
### 执行顺序建议（分批）
…
### 风险提示
```

数据不足（无历史/无候选）时如实说明，不得虚构权重或收益数字。
