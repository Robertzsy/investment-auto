# Investment Auto 2.0 架构文档（as-built）

> 分支 `dsch/2.0`。本文档描述已实现的架构、数据流、安全边界与扩展方法。
> 1.x（v0.9.1）保留在 `master` 作为回退版本。

## 1. 总体架构

```
┌ Windows 桌面壳（windows/desktop，WPF + WebView2，复用 1.x 壳）────────────┐
│ 单实例 / 托盘 / 开机自启 / Job Object 强杀无孤儿进程 / 动态回环端口          │
│ 首次运行打开引擎 /setup 向导页；完成后切换到 DSH 助手并启动投资引擎         │
│ 状态栏轮询引擎 /api/status（模式、策略、风控、最近轮次）                    │
└──────────────┬──────────────────────────────────────────┬────────────────┘
               ▼ http://127.0.0.1:<web>                    ▼ http://127.0.0.1:<api> + X-IA-Token
┌ DSH web（Node 22，profile: investment-web，DSH_HOME=用户数据目录）────────┐
│ 对话/会话/设置/模型页/Skills/plan/goal/jobs（DSH 原生）                    │
│ agent preset "investment"：投资 persona，无 shell/文件/编码工具            │
│ host 插件 investment-tools：15+ 个 investment_* 工具（引擎 HTTP 桥）       │
│ 桌面 overlay：credentials 行换成 DPAPI provider（app/profiles/patches）    │
└──────────────┬───────────────────────────────────────────────────────────┘
               │ MCP 式工具调用 = HTTP + X-IA-Token（回环）
               ▼
┌ 投资引擎（Python，engine/）── 不可绕过的执行边界 ─────────────────────────┐
│ HTTP 命令 API（serve）：状态/行情/选股/组合/报告/宏观/授权书/凭据/向导      │
│ 命令总线 → InvestmentAgentService → 硬风控 build_orders → 纸面经纪         │
│ submit_decisions：DSH 决策的唯一执行入口（取价、重算仓位、撮合、审计）      │
│ 调度器（APScheduler，市场时段）：轮次触发 → DSH 桥（headless）             │
└──────────────────────────────────────────────────────────────────────────┘
```

**两层语义**：DSH 应用是对话与决策面（通用 AI 助手 + 投资职责）；引擎是
执行面（行情、选股、组合、风控、纸面撮合、调度、报告）。两面只通过两条
通道连接，AI 永远绕不过引擎代码。

## 2. 两条连接通道

### 2.1 对话方向（用户 → 引擎）

`investment_*` 工具（`app/plugins/dsh-investment-tools`，host 平面注册，
`ctx.tools.register` + `defineTool`）调用引擎回环 API：

- 只读：`investment_status / portfolio / market_snapshot / market_history /
  security_search / screening / optimizer_latest / reports / report_latest /
  macro_latest / mandate`
- 写（纸面边界）：`investment_set_strategy / control / run_cycle /
  run_screening / reset_account / submit_decisions`

写操作在 Skill 与 persona 中约定为「先计划、用户批准后执行」；引擎侧还有
独立防线（见第 4 节）。

### 2.2 自主方向（调度 → AI 决策）

引擎调度器在市场时段触发轮次，通过可插拔 cycle runner
（`engine/scheduler.set_cycle_runner`）调用 `engine/dsh_bridge.DshBridgeRunner`：

1. spawn `dsh --profile investment "<任务>"`（headless，同一套
   investment 工具与 Skills；shell/文件/编码工具与 plan 模式在该
   profile 中已禁用）；
2. 会话按 complete-investment-cycle 流程研究并调用
   `investment_submit_decisions` 提交决策；
3. 引擎在 `submit_decisions` 内完成取价、授权书硬边界、硬风控、纸面撮合，
   原子写入审计；
4. runner 读取本轮审计（引擎事实）回填决策/成交，调度器据此写报告、
   通知、反思。

未注册 runner（开发模式未装 app/ 或配置关闭）时轮次返回 `skipped`，
不中断调度。

## 3. 目录与部署

| 路径 | 内容 |
|---|---|
| `engine/` | Python 业务引擎（1.x 复用部分 + 2.0 API/桥） |
| `app/` | DSH 应用壳：`profiles/`（investment-web/investment + patches）、`presets/investment/`、`skills/`（7 个 SKILL.md）、`plugins/`（2 个）、锁定的 rc.6 依赖树 |
| `windows/desktop/` | WPF + WebView2 壳（2.0 进程模型） |
| `installer/` | Inno Setup（每用户；engine + app + Node22 + Python 捆绑） |
| `scripts/` | 构建/门禁/运行时获取 |

安装布局（与 1.x 相同的数据分离）：

```text
程序目录：%LocalAppData%\Programs\InvestmentAuto   （engine/、app/、python/、node/，只读）
用户数据：%LocalAppData%\InvestmentAuto             （= DSH_HOME = 引擎数据根）
  ├─ profiles/ .agent-presets/ skills/ sessions/ settings.yaml   （DSH 数据）
  ├─ runtime/  （账户、审计、报告、记忆、选股、优化、调度锁）
  ├─ config/   （config.yaml + market 规则）
  └─ secrets.enc （API Key，DPAPI 加密）
```

引擎启动时 `engine/dsh_home.seed_dsh_home` 从程序目录播种 profiles/presets/
skills/plugins 到用户数据目录（已存在不覆盖）；安装版全程不运行 PowerShell。

模型与密钥：DSH「设置 → 模型」页配置；桌面 overlay 把凭据存储换成引擎
DPAPI store（`app/plugins/dsh-dpapi-credentials`），密钥不落明文文件。

## 4. 安全边界（铁律）

1. 只有 `trading.mode=paper` 可执行；引擎代码拒绝其他模式。
2. `submit_decisions` 是 AI 决策的唯一执行入口：引擎自行取价（不信任
   载荷）、标的必须在允许池（持仓 ∪ 最新选股 ∪ 配置默认标的）、仓位由
   `build_orders` 按授权书硬边界重算并封顶。
3. 紧急停止（kill）激活时拒绝一切决策执行；暂停不阻断人工提交但记录告警。
4. 撮合在纸面经纪的持仓锁 + 原子写内完成；审计原子落盘。崩溃最多丢失
   一轮研究，不会重复成交（fail-safe 由幂等设计保证，替代 1.x 的
   checkpoint 重放）。
5. 自主轮次的 headless agent 无 shell、无文件工具、无 plan 模式；
   对话 preset（investment）同样无 shell/编码工具。
6. 服务仅绑定 127.0.0.1 动态端口；引擎 API 要求 `X-IA-Token`（每启动
   随机），令牌只注入 WebView2 对引擎源的请求，从不注入 DSH web 源。

## 5. 阶段状态

| 阶段 | 内容 | 状态 |
|---|---|---|
| P0 | 分支、engine/ 重排、app/ 锁版 rc.6、自定义 profile 可启动、API 骨架 | ✅ 91→97 测试 |
| P1 | 引擎只读 API + 15 个 investment_* 工具 + 投资 preset + persona | ✅ 97→105 测试 |
| P2 | submit_decisions 执行链 + 7 个 DSH Skills + 技能校验脚本 | ✅ 105→113 测试 |
| P3 | headless 自主轮次（DSH 桥 + runner + headless profile 收紧） | ✅ 113→119 测试 |
| P4 | 桌面壳 2.0 进程模型、DPAPI 凭据、首次向导、安装器/运行时脚本 | ✅ 119 + C# 20/20 |
| P5 | 发行门禁、架构文档、真实模型端到端验收 | ✅ 121 测试；自主轮次 E2E 实测通过（见下） |

### 5.1 自主轮次真实模型端到端验收（2026-08-21 实测）

隔离数据目录下完整验证：引擎 `serve` + `run`（调度器 + DSH 桥）→
`run_cycle` → headless 会话（DPAPI 凭据 overlay）→ 真实模型 + 真实行情
研究 4 只候选并形成 3 条 BUY 决策 → `investment_submit_decisions` →
引擎硬风控 + 纸面经纪 **2 笔成交** → 审计/报告/反思全链路落盘。验收中
发现并修复三个真实缺陷：

1. **工具 render 契约**：`output.render(args, value)` 必须返回内容块
   `[{type:"text", text}]`；返回裸字符串会破坏工具结果消息结构
   （第二轮模型请求 `content.some is not a function` → TRANSPORT）。
2. **桥进程环境**：runner 必须把 `INVESTMENT_API_PORT` 派生成
   `INVESTMENT_ENGINE_URL` 传给 headless 会话，否则轮次工具全部
   指向默认端口 8790。
3. **runner 注册范围**：`serve` 进程也要注册桥（API 的 run_cycle 在
   serve 内执行），否则手动轮次报 `cycle_runner_unavailable`。

对应回归测试：`tests/test_dsh_bridge.py`（10 项）、
`app/plugins/dsh-investment-tools/test/tools.test.mjs`（render 契约）。

## 6. 已延期 / 待办

- **客户端投资面板插件**（状态栏、持仓/报告页嵌进 DSH 侧栏）：需要
  按 `dsh.client` roster 行 + client 插件包模式实现；当前桌面壳状态栏
  （模式/策略/风控/轮次）已覆盖最常用信息。扩展入口：在
  investment-web patch 的 browser roster 中新增自定义 client 包。
- **对话面 E2E 验收**：自主轮次链路已用真实模型实测通过；桌面对话页
  （investment-web + 投资 preset）的日常问答与手动周期流程待配置
  安装版 API Key 后走一轮人工验收（DPAPI overlay 已在 headless 同款
  路径上验证）。
- **正式 Release**：VM 全流程验收（安装 → 向导 → 对话 → 轮次 → 托盘 →
  重启恢复 → 卸载保数据）后发布 2.0 安装包；发行门禁
  `scripts/release-check.ps1` 已切到 2.0 全部测试面。

## 7. 扩展方法

- **加引擎能力**：在 `engine/investment/service.py` 加命令 + `engine/api`
  加路由 + 测试；DSH 侧在工具插件加一个 `investment_*` 工具即可。
- **加 Skill**：`app/skills/<kebab-case>/SKILL.md`（frontmatter:
  name/description/whenToUse），`node app/scripts/check-skills.mjs`
  校验结构与被引用工具一致性；`seed.ps1 -Force` 或引擎启动播种。
- **改 agent 行为**：编辑 `app/presets/investment/agent.cordis.yml`
  （工具面）与 persona 文本；profile patch
  `app/profiles/investment-web/cordis.patch.yml` 控制 host 平面。
- **升级 DSH**：改 `app/package.json` + lockfile 到目标版本，重跑
  python/node/dotnet 全部门禁与 boot 冒烟；rc 阶段升级是显式流程，
  不追 rc。
