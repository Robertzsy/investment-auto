---
name: account-management
description: 账户与系统管理：查看/切换策略档位、手动/自动模式、暂停/恢复/紧急停止、重置模拟账户、查看策略授权书。适用「切到保守策略」「暂停自动交易」「重置 A 股模拟账户」等请求。
whenToUse: 用户要求管理策略、模式、交易开关或账户时加载。
---

# 账户管理（Account Management）

## 目标

安全地管理投资引擎的运行状态与账户。**一切破坏性操作（紧急停止解除、重置账户、切换自动模式）必须向用户复述影响并等待明确批准。**

## 操作与工具对照

| 操作 | 工具 | 说明 |
|---|---|---|
| 查看当前策略授权书 | `investment_mandate` | 三档策略的硬边界（仓位/现金/回撤/换手/当日交易） |
| 切换策略档位 | `investment_set_strategy(profile)` | conservative / neutral / aggressive；立即作用于后续轮次；批准后执行 |
| 暂停 / 恢复 | `investment_control(action="pause"\|"resume", reason)` | 暂停只影响自动轮次；恢复前检查 kill 状态 |
| 紧急停止 | `investment_control(action="kill", reason)` | 冻结一切决策执行；必须先 reset_kill 才能恢复 |
| 解除紧急停止 | `investment_control(action="reset_kill", reason)` | 高风险操作：确认用户理解后果 |
| 重置模拟账户 | `investment_reset_account(market, reason)` | 备份后清空该市场模拟账户为初始资金；仅纸面模式；批准后执行 |
| 查看运行状态 | `investment_status` | 模式、时段、下一轮次、最近报告 |

## 安全规则

- 紧急停止激活期间：任何恢复/重置请求先向用户说明 kill 状态与解除风险。
- 重置账户前：报告当前持仓与现金，说明备份位置，等待用户确认。
- 策略切换前：说明新档位的仓位/回撤边界变化。
- 所有操作完成后：用 `investment_status` 复查状态并报告结果。

## 输出

每次管理操作结束输出：

```text
## 账户管理结果
- 操作：…
- 前置状态：…
- 执行结果：…
- 当前状态：…
- 审计/备份位置：…
```
