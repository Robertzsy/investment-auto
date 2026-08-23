# Investment Auto

[简体中文](README.md) | [English](README_EN.md)

Investment Auto 2.1.3 是一款面向 A 股、港股、美股和场内 ETF 的投资研究与模拟交易桌面应用。2.x 完全基于 DeepSeek Harness（DSH）深度改造，但对用户呈现的是独立的 Investment Auto 产品：没有工作区选择、模式选择或底座品牌，只有 Dashboard、投资助手、分析流程和设置。

> 本项目只支持研究与模拟交易，不连接真实券商，也不应直接用于真实资金决策。

## 下载

- [Investment Auto v2.1.3 Release](https://github.com/Robertzsy/investment-auto/releases/tag/v2.1.3)
- 安装包：`InvestmentAuto-Setup-x64.exe`
- 支持 Windows 10/11 x64；安装包内置 Python、Node.js、.NET 桌面运行环境与 WebView2 兜底安装程序。

SHA-256：

```text
00522AA80EAEF39BB9B59F1B50B458A2177F909AD807914DDB34367A85B49560
```

程序默认安装到 `%LocalAppData%\Programs\InvestmentAuto`，用户数据保存在 `%LocalAppData%\InvestmentAuto`。覆盖升级不会改动账户、持仓、报告、配置、凭据和会话；2.1.3 的实机升级校验确认 76,167 个用户数据文件无丢失、无变化。

## 2.0 以来发生了什么

| 版本 | 核心变化 |
|---|---|
| 2.0.0 | 从 1.x 的自研 Agent/窗口双层架构切换为 DSH 原生对话、工具、Skills、子代理与工作流；原投资业务收敛为独立 Python 引擎，并完成 Windows 桌面发行、DPAPI 密钥和 1.x 数据迁移。 |
| 2.1.0 | 把“DSH + 投资 preset”产品化为 Investment Auto：重做 UI，加入 Dashboard、分析流程和设置，删除工作区/模式选择与底座标识，同时保持 DSH 原生对话、思考、流式输出和工具展示不变。 |
| 2.1.1 | 把“选股”和“分析股票”拆成明确入口；用户指定股票直接接入固定完整分析流程。引入异步轮次、轮询状态和初版交易幂等，解决窗口 AI 自行分析、长请求超时和重复启动问题。 |
| 2.1.2 | 强化成交回执、决策指纹、跨进程租约、失败/重启恢复和用户标的绑定；内部 headless 与角色会话迁入独立 DSH Home，不再污染用户会话栏。 |
| 2.1.3 | 开放 IA 的文件、PowerShell、搜索、后台任务与 Ralph 自维护能力；加入自维护 Skill，修复工作流 schema 兼容、失败轮次重试竞态、Windows 原子写入和安装器误打包开发数据等问题。 |

完整记录见 [中文更新日志](CHANGELOG.md)、[English changelog](CHANGELOG_EN.md) 和 [v2.1.3 双语发行说明](docs/RELEASE_NOTES_2.1.3.md)。

## 当前产品能力

- 四市场行情、选股、证券分析、组合检查、组合优化、宏观日报和独立模拟账户；
- Dashboard 汇总引擎状态、风险状态、市场资产、最近轮次和报告；
- 投资助手保留 DSH 原生会话、思考过程、流式输出、工具、Skills、计划、目标和子代理能力；
- 分析流程页展示真实轮次、阶段、证据数、检查点和最终决策，不用对话历史冒充流程进度；
- 设置页统一管理模型与 DPAPI 密钥、策略、硬风控、市场、选股、调度和通知；
- Windows 单实例桌面壳、动态回环端口、随机访问令牌、后台进程托管和覆盖升级；
- IA 可读取日志和权威源码，执行修改、测试、构建、任务重试和回滚。

## 选股与股票分析

2.1 将用户入口明确分为两块：

```text
选股请求
  → 市场硬筛选与多因子评分
  → Agent 复筛
  → 标准化候选列表
  → 固定完整分析流程

用户指定股票
  → 证券身份确认
  → 跳过选股
  → 固定完整分析流程
```

固定分析流程如下：

```text
技术面 / 基本面 / 新闻 / 情绪研究
  → 多空辩论
  → 研究经理与逐标的交易员
  → 组合草案
  → 激进 / 保守 / 中立风险辩论
  → 风险经理
  → 最终组合决策
  → 用户批准或自主轮次授权
  → Python 硬风控
  → 模拟撮合、审计与报告
```

各阶段原子写入 `runtime/analysis_runs/`。程序异常或系统重启后按 checkpoint 续跑；同一请求使用 cycle id、决策指纹和账户内成交回执避免重复分析与重复成交。

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

更多细节见 [2.0 架构](docs/ARCHITECTURE_2.0.md)、[引擎 API](docs/ENGINE_API.md)、[产品外壳](docs/PRODUCT_SHELL.md) 和 [应用侧说明](app/README.md)。

## 权限与安全边界

IA 以 DSH `danger-full-access` 运行，在当前 Windows 用户权限范围内可以读取日志、修改项目源码、运行 PowerShell、测试和构建。这让它具备“诊断 → 修复 → 验证 → 重试 → 回滚”的自维护基础。

系统级权限与交易权限彼此独立：

- 始终只使用模拟账户和纸面经纪；
- 手动提交仍需要用户批准；
- 所有决策必须通过 Python 授权书和硬风控；
- cycle id、决策指纹和账户内执行回执阻止重复成交；
- API Key 和 Webhook 使用 Windows DPAPI 保存，不写入普通配置或日志；
- headless 与角色子会话保存在独立内部 DSH Home，不进入用户会话栏。

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

## 分支与兼容性

- `dsch/2.0`：当前 2.x 开发与发布分支；
- `master`：保留 1.x（v0.9.1）作为历史与回退版本；
- 1.x 用户数据可以迁移到 2.x，迁移与覆盖升级都不会删除源数据。

## 许可证

[MIT License](LICENSE)
