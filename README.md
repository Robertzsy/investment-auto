# Investment Auto

**AI 驱动多市场投资研究与模拟交易自动化系统**

[简体中文](README.md) | [English](README_EN.md)

[![Release](https://img.shields.io/badge/release-v2.1.3-brightgreen)](https://github.com/Robertzsy/ai-trading-automation/releases/tag/v2.1.3)
[![License](https://img.shields.io/badge/license-MIT-blue)](LICENSE)
[![Platform](https://img.shields.io/badge/platform-Windows%2010%2F11%20x64-lightgrey)]()

Investment Auto 2.1.3 是一款面向 A 股、港股、美股和场内 ETF 的投资研究与**模拟交易**桌面应用，内置确定性选股、13 角色多智能体分析和硬风控撮合。2.x 基于 DeepSeek Harness（DSH）深度改造，对用户呈现为独立产品：只有 Dashboard、投资助手、分析流程和设置。

> 本项目只支持研究与模拟交易，不连接真实券商，也不应直接用于真实资金决策。

## 目录

- [特性](#特性)
- [安装](#安装)
- [从源码运行](#从源码运行)
- [核心投资逻辑](#核心投资逻辑)
- [架构](#架构)
- [版本历史](#版本历史)
- [安全边界](#安全边界)
- [测试与发行](#测试与发行)
- [文档](#文档)
- [分支与兼容性](#分支与兼容性)
- [许可证](#许可证)

## 特性

- **四市场一站式**：A 股 / 港股 / 美股 / 场内 ETF 统一行情、选股、分析与模拟账户；T+1/T+0、整手、涨跌停、佣金、印花税、滑点等市场规则内建。
- **确定性选股**：硬筛选 + 六因子加权评分（动量 / 趋势 / 流动性 / 估值 / 量能 / 低波动），每只入选股附中文证据，权重与阈值可配置。
- **13 角色委员会式分析**：四路基础研究 → 多空辩论 → 研究经理与逐标的交易员 → 组合草案 → 三方风险辩论 → 最终决策；结论强制引用证据，进度与检查点真实可见。
- **不可绕过的风控**：三档策略授权书、仓位封顶、回撤熔断、止损止盈内置；AI 决策必经 Python 硬风控与纸面撮合。
- **产品化桌面应用**：Dashboard / 投资助手 / 分析流程 / 设置四个页面；一键安装、单实例、托盘、开机自启、覆盖升级保留数据。
- **对话式 AI 助手**：保留 DSH 原生思考、流式输出、工具、Skills、计划与子代理能力；IA 还能读日志、改源码、跑测试，具备自我维护能力。

## 安装

- 下载 [v2.1.3 Release](https://github.com/Robertzsy/ai-trading-automation/releases/tag/v2.1.3) 中的 `InvestmentAuto-Setup-x64.exe`
- 支持 Windows 10/11 x64；安装包内置 Python、Node.js、.NET 桌面运行时与 WebView2 兜底安装程序

SHA-256：

```text
00522AA80EAEF39BB9B59F1B50B458A2177F909AD807914DDB34367A85B49560
```

程序默认安装到 `%LocalAppData%\Programs\InvestmentAuto`，用户数据保存在 `%LocalAppData%\InvestmentAuto`。覆盖升级不改动账户、持仓、报告、配置、凭据和会话；2.1.3 升级实测 76,167 个用户数据文件零丢失。

## 从源码运行

需要 Python 3.10+、Node.js 22+ 和 .NET 8 SDK（仅构建桌面壳时需要）。

```powershell
# 安装 Python 依赖
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -e .

# 终端 1：启动投资引擎
.\.venv\Scripts\python.exe -m engine.main serve

# 终端 2：启动 Investment Auto Web 产品壳
.\app\scripts\dev.ps1 -Port 4567
```

然后访问 `http://127.0.0.1:4567`，在设置页配置模型与 API Key。

## 核心投资逻辑

Investment Auto 的投资智能由三段确定性逻辑构成：**先排除、再打分、后解释的选股**，**13 角色委员会式分析**，以及**不可绕过的风控纪律**。

- **选股**：先硬筛选（代码归一化、最低价 / 成交额 / 市值、PE/PB 上限、排除 ST/退市/权证），再按六因子加权评分排序，每只入选股附中文证据；
- **多角色分析**：五阶段 13 角色（技术面/基本面/新闻/情绪 → 多空辩论 → 研究经理与交易员 → 组合草案 → 三方风险辩论 → 风险经理 → 组合经理），结论强制引用证据，研究不足的持仓强制 HOLD；
- **决策与风控**：三档策略授权书定义硬边界；仓位 = min(市场单票上限, 策略单票上限)；回撤熔断强制清仓；止损止盈优先于 AI 建议；标的限于允许池、价格由引擎实时抓取。

因子计算方式、角色分工、风控参数与商业价值评估的完整说明见 [投资逻辑详解](docs/INVESTMENT_LOGIC.md)。

## 架构

```text
Windows WPF + WebView2
          │
Investment Auto 产品外壳
          │
DSH 对话 / 工具 / Skills / 子代理 / 工作流
          │  本机令牌保护的回环 HTTP API
Python 投资引擎
          │
行情、选股、组合、风控、模拟撮合、审计与调度
```

- `app/`：DSH profiles、preset、Skills、投资工具桥、固定工作流和产品 UI；
- `engine/`：投资事实、配置、分析轮次状态、硬风控、纸面经纪与调度；
- `windows/desktop/`：WPF/WebView2 桌面壳与进程生命周期；
- `installer/`：自包含 Windows 安装器；
- `tests/`：引擎、恢复、幂等、配置和桌面回归测试。

## 版本历史

| 版本 | 核心变化 |
|---|---|
| 2.0.0 | 从 1.x 的自研 Agent/窗口双层架构切换为 DSH 原生对话、工具、Skills、子代理与工作流；原投资业务收敛为独立 Python 引擎，并完成 Windows 桌面发行、DPAPI 密钥和 1.x 数据迁移。 |
| 2.1.0 | 把“DSH + 投资 preset”产品化为 Investment Auto：重做 UI，加入 Dashboard、分析流程和设置，删除工作区/模式选择与底座标识，同时保持 DSH 原生对话、思考、流式输出和工具展示不变。 |
| 2.1.1 | 把“选股”和“分析股票”拆成明确入口；用户指定股票直接接入固定完整分析流程。引入异步轮次、轮询状态和初版交易幂等，解决窗口 AI 自行分析、长请求超时和重复启动问题。 |
| 2.1.2 | 强化成交回执、决策指纹、跨进程租约、失败/重启恢复和用户标的绑定；内部 headless 与角色会话迁入独立 DSH Home，不再污染用户会话栏。 |
| 2.1.3 | 开放 IA 的文件、PowerShell、搜索、后台任务与 Ralph 自维护能力；加入自维护 Skill，修复工作流 schema 兼容、失败轮次重试竞态、Windows 原子写入和安装器误打包开发数据等问题。 |

完整记录见 [中文更新日志](CHANGELOG.md)、[English changelog](CHANGELOG_EN.md) 和 [v2.1.3 双语发行说明](docs/RELEASE_NOTES_2.1.3.md)。

## 安全边界

IA 以 DSH `danger-full-access` 运行，在当前 Windows 用户权限范围内可以读取日志、修改项目源码、运行 PowerShell、测试和构建。系统级权限与交易权限彼此独立：

- 始终只使用模拟账户和纸面经纪；
- 手动提交仍需要用户批准；
- 所有决策必须通过 Python 授权书和硬风控；
- cycle id、决策指纹和账户内执行回执阻止重复成交；
- API Key 和 Webhook 使用 Windows DPAPI 保存，不写入普通配置或日志；
- headless 与角色子会话保存在独立内部 DSH Home，不进入用户会话栏。

## 测试与发行

2.1.3 已通过：

- Python：184 项；
- Node 插件：22 项；
- Windows 桌面：20 项；
- Skills、插件组合、真实 Profile、安装升级和 AAPL 完整分析恢复实测。

完整发行门禁：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\release-check.ps1
```

## 文档

- [投资逻辑详解](docs/INVESTMENT_LOGIC.md) · [Investment Logic (EN)](docs/INVESTMENT_LOGIC_EN.md)
- [2.0 架构](docs/ARCHITECTURE_2.0.md) · [引擎 API](docs/ENGINE_API.md) · [产品外壳](docs/PRODUCT_SHELL.md) · [应用侧说明](app/README.md)

## 分支与兼容性

- `dsch/2.0`：当前 2.x 开发与发布分支；
- `master`：保留 1.x（v0.9.1）作为历史与回退版本；
- 1.x 用户数据可以迁移到 2.x，迁移与覆盖升级都不会删除源数据。

## 许可证

[MIT License](LICENSE)
