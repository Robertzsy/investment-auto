# Investment Auto v2.1.3

[中文](#中文) | [English](#english)

## 中文

Investment Auto 2.1.3 是 2.0 DSH 重构路线的首个完整稳定版本。它把 2.0 的底座迁移、2.1.0 的产品化外壳、2.1.1 的固定流程路由、2.1.2 的恢复与幂等，以及 2.1.3 的 IA 自维护能力合并为一个可安装、可升级、可恢复的 Windows 桌面版本。

### 主要变化

- **完整 DSH 深度改造**：对话、会话、思考、流式输出、工具、Skills、目标、计划、子代理和工作流由 DSH 提供；Investment Auto 隐藏工作区、模式选择和底座品牌，只保留投资产品界面。
- **重新设计的产品 UI**：Dashboard、投资助手、真实分析流程、设置和会话列表；删除账户入口与其他非投资模式，保留原生对话内核不变。
- **选股与股票分析分离**：用户指定证券直接接入固定分析流程；选股请求先经过硬筛选和多因子评分，再把标准化候选交给同一流程。
- **固定全流程多角色分析**：四类基础研究、多空辩论、研究经理、逐标的交易员、组合草案、三类风险辩论、风险经理和最终组合决策。
- **可恢复、不会重复成交**：原子 checkpoint、跨进程 cycle lease、决策指纹、`ready_for_execution` 状态和账户内执行回执共同处理超时、崩溃、重启和重复请求。
- **内部会话隔离**：headless 和角色子会话使用独立 DSH Home，不再出现在用户会话列表。
- **IA 自维护**：开放文件、PowerShell、搜索、后台任务和 Ralph 能力；IA 可以从日志诊断问题，修改权威源码，运行测试和构建，重试任务并在失败时回滚。
- **安装与升级加固**：DPAPI 密钥、自包含 Python/Node/.NET、用户数据与程序分离；安装器排除开发会话与凭据，覆盖升级清理旧版误装的 `app/dev-home`。

### 从 2.0 到 2.1.3

- `2.0.0`：迁移到 DSH 运行时和独立 Python 投资引擎，完成桌面安装、DPAPI 与旧数据迁移。
- `2.1.0`：完成 Investment Auto 产品化 UI、Dashboard、分析流程和设置。
- `2.1.1`：统一用户指定股票与自主轮次的固定分析入口，加入异步启动和状态轮询。
- `2.1.2`：完成成交幂等、指纹绑定、跨进程恢复和内部会话隔离。
- `2.1.3`：完成 IA 全权限自维护、schema 兼容、重试竞态、Windows 原子写入和安装器修复。

### 验证

- Python 184 项、Node 插件 22 项、Windows 桌面 20 项测试通过。
- Skills、插件组合、真实 Profile 和安装升级验证通过。
- 原失败 AAPL 轮次使用同一 cycle id 在正式安装版复跑，完整通过五个 checkpoint 并到达 `ready_for_execution`；`submit=false`，未产生交易。
- 覆盖升级前后 76,167 个用户数据文件缺失 0、变化 0、新增 0。

### 下载与校验

- Windows 10/11 x64：`InvestmentAuto-Setup-x64.exe`
- 大小：161,497,160 bytes
- SHA-256：`00522AA80EAEF39BB9B59F1B50B458A2177F909AD807914DDB34367A85B49560`

> Investment Auto 只支持模拟交易，不连接真实券商。IA 的系统级完整权限不会绕过用户批准、成交幂等、模拟交易边界和 Python 硬风控。

## English

Investment Auto 2.1.3 is the first complete stable release of the 2.x DSH rebuild. It combines the 2.0 runtime migration, the 2.1.0 standalone product shell, the 2.1.1 fixed workflow routing, the 2.1.2 recovery and idempotency layer, and the 2.1.3 IA self-maintenance surface into one installable, upgrade-safe, and recoverable Windows desktop product.

### Highlights

- **Deep DSH rebuild:** DSH provides conversations, sessions, reasoning, streaming, tools, Skills, goals, plans, subagents, and workflows. Investment Auto hides workspace selection, runtime modes, and platform branding behind a focused investment product.
- **Redesigned product UI:** Dashboard, Investment Assistant, real Analysis Workflow, Settings, and session list. The unnecessary Account entry and every non-investment mode are removed while the native conversation core stays unchanged.
- **Screening separated from analysis:** a user-specified security enters the fixed workflow directly; a screening request first runs deterministic filters and factor scoring, then passes normalized candidates into the same workflow.
- **Fixed full multi-role workflow:** four base-research roles, bull/bear debate, research manager, per-symbol trader, portfolio draft, three-way risk debate, risk manager, and final portfolio decision.
- **Recoverable without duplicate fills:** atomic checkpoints, cross-process cycle leases, decision fingerprints, `ready_for_execution`, and account-embedded broker receipts cover timeouts, crashes, restarts, and repeated requests.
- **Internal session isolation:** headless and role subagent sessions use a separate DSH Home and no longer appear in the user session list.
- **IA self-maintenance:** filesystem, PowerShell, search, background-job, and Ralph capabilities let IA diagnose logs, edit authoritative source, run tests/builds, replay tasks, and roll back failures.
- **Hardened install and upgrade:** DPAPI credentials, bundled Python/Node/.NET, separated program and user data, exclusion of development sessions and credentials, and cleanup of `app/dev-home` accidentally shipped by older installers.

### From 2.0 to 2.1.3

- `2.0.0`: migrated to DSH and an independent Python investment engine; delivered the desktop installer, DPAPI, and legacy-data migration.
- `2.1.0`: delivered the standalone Investment Auto UI, Dashboard, live workflow, and Settings.
- `2.1.1`: unified user-specified and autonomous analysis behind the fixed workflow; added asynchronous starts and status polling.
- `2.1.2`: completed broker idempotency, fingerprint binding, cross-process recovery, and internal-session isolation.
- `2.1.3`: completed full IA self-maintenance plus schema, retry-race, Windows atomic-write, and installer fixes.

### Validation

- 184 Python tests, 22 Node plugin tests, and 20 Windows desktop tests passed.
- Skills, plugin composition, real profiles, and in-place upgrade validation passed.
- The original failed AAPL cycle was replayed with the same cycle ID on the installed build, completed all five checkpoints, and reached `ready_for_execution`. It used `submit=false` and placed no trade.
- All 76,167 user-data files were preserved across the upgrade with zero missing, changed, or added files.

### Download and checksum

- Windows 10/11 x64: `InvestmentAuto-Setup-x64.exe`
- Size: 161,497,160 bytes
- SHA-256: `00522AA80EAEF39BB9B59F1B50B458A2177F909AD807914DDB34367A85B49560`

> Investment Auto is paper-trading only and does not connect to a live broker. Full IA system authority does not bypass user approval, execution idempotency, the paper-only boundary, or deterministic Python risk controls.
