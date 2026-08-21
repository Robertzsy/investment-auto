# 更新日志

## 2.0.0-preview — DeepSeek Harness 底座重构（2026-08-21，分支 `dsch/2.0`）

Investment Auto 2.0 以 DeepSeek Harness（DSH）为运行底座全面重建：对话、会话、
模型调用、工具、Skills、plan/goal/子代理/工作流全部来自 DSH；1.x 的业务能力收敛为
独立 Python 引擎（`engine/`），通过回环 HTTP 命令 API 与 DSH 工具桥连接。

- **P0 骨架**：`engine/` 业务引擎（1.x 复用部分重排，旧 Agent/Manager/UI/LLM 层删除）；
  `app/` 锁定 `@deepseek-ai/dsh@0.1.0-rc.6` 依赖树（防 npm 混装 rc.8）；
  自定义 `investment-web` / `investment` profile + `investment` agent preset
  （无 Shell/文件/编码工具）；HTTP 命令 API 骨架。
- **P1 对话+桥**：引擎只读 API（状态/行情/选股/组合/报告/宏观/授权书）+ 17 个
  `investment_*` 工具（`ctx.tools.register` 原生插件）+ 投资 persona。
- **P2 投资 Skills**：`submit_decisions` 执行链（引擎自行取价 → 授权书硬边界 →
  `build_orders` 风控 → 纸面撮合 → 审计/报告/反思）+ 7 个 DSH `SKILL.md`
  （证券分析/市场概览/选股/组合检查/组合优化/完整投资周期/账户管理）+
  技能-工具一致性校验。
- **P3 自主轮次**：`engine/dsh_bridge.py` 在调度时刻 spawn DSH headless 会话
  （同一套工具与 Skills；shell/文件/编辑/ralph/plan 模式关闭），runner 以引擎审计
  为事实回填决策与成交；fail-safe 由幂等设计保证（崩溃丢轮不重复成交）。
- **P4 桌面发行**：WPF 壳 2.0 进程模型（引擎 serve/run + DSH web 双进程、动态端口、
  `--patch` overlay、就绪解析、Job Object）；DPAPI 凭据 provider（密钥不落明文）；
  `/setup` 首次向导（导入旧数据 + 初始化 + 完成前不启动自动投资）；引擎启动自动播种
  DSH home（安装版零 PowerShell）；Node 22 运行时与安装器脚本。
- **P5 门禁与实测**：发行门禁覆盖 Python/C#/插件/技能全测试面；自主轮次真实模型
  端到端实测（真实行情研究 → 3 决策 → 纸面成交 → 审计/报告/反思）；真实 1.x 数据
  迁移实测（15 项数据 + 4 密钥 DPAPI 无损导入）。
- **实测修复**：工具 render 契约（内容块而非裸字符串）、桥进程环境
  （INVESTMENT_ENGINE_URL 派生）、runner 注册范围（serve 进程同样执行轮次）。
- **测试基线**：Python 121 项 + C# 桌面 20 项 + Node 插件 6 项 + 技能结构校验。

详细架构见 `docs/ARCHITECTURE_2.0.md`、`docs/ENGINE_API.md`；1.x 保留在 `master`。

## 0.9.1 — Supervised Repair Closure（2026-08-20）

- 安装版内置 `repair_verifier`，自修复不再依赖未随安装包发布的 pytest 测试目录；
- 现有文件只允许带 SHA256 并发保护的单次精确片段替换，拒绝整文件覆盖和超过 200 行的补丁；
- 补丁后的原始请求改由全新 Python 解释器回放，确保验证进程真正加载新模块；
- 测试器启动、超时、结果解析和语义回放异常全部触发备份恢复，并清除对应重启请求；
- 新增持久化 incident repair 审计及桌面 Harness“修复审计”视图。

## 0.9.0 — Failure-Aware Harness（2026-08-20）

- 新增 `market-overview` Skill，广义 A 股问题固定覆盖上证、深证、创业板、沪深 300 与市场宽度样本；
- 引入带交易所、资产类型和供应商代码的证券身份，阻止 `sh000002` 被降级成 `sz000002/万科A`；
- 完成契约新增身份、时效、事实一致性和覆盖率质量门禁，并区分 `completed`、`degraded`、`incomplete`；
- 新增有界恢复轨迹和 `incident-repair`，只有目标测试与原始请求语义回放均通过才确认修复，否则自动回滚；
- system_admin 获得独立的 8 次模型请求、6 次工具调用、单次代码变更预算，内层工具事件会同步显示在桌面对话中；
- 桌面 Harness 工作台增加降级状态展示，版本统一为 0.9.0。

## 0.8.0 — Agent Harness 桌面控制面（2026-08-20）

### Agent Harness / Skill Runtime 核心重构

- 顶层 Manager 从二十余个扁平业务工具一次性切换为六个 Harness 级能力；行情、研究、
  选股、组合和交易函数全部降为 Skill 内部受控 Action；
- 新增研究、组合、执行、系统管理四个持久会话，以及完整 Skill Manifest、Workflow、
  完成契约、权限验证和逐步 execution trajectory；
- 内置证券分析、选股、组合检查、组合优化、完整投资周期、结构化定时轮次、账户管理和
  系统管理 Skills；聊天按钮、HTTP 控制面和配置内定时轮次统一进入 Skill Runtime；
- 新增 `create_skill` 编译—测试—注册—当轮 fulfill 闭环；缺少的只读 Action 继续复用原子
  工具工厂，但不再作为顶层模型可见工具；
- 新增持久化 Skill Scheduler，固定 Skill 版本、Cron、时区和结构化输入，触发时不再解释
  自然语言；成功判定改为完成契约，调用过工具不再等价于已验证；
- 新增 50 条自然语言路由验收与完整 Skill Runtime 回归测试。

### 桌面应用同步

- 新增 Agent Harness 工作台，可查看 Skill 目录、四类持久会话、完成验证和逐步执行轨迹；
- 新增结构化定时 Skill 的创建、启停、立即运行和删除界面；系统管理 Skill 保持禁止后台执行；
- 对话页增加 Skill Runtime 实时摘要和最近执行状态，工具事件改为明确展示 Skill Runtime 入口；
- WPF 状态栏增加 Harness 健康状态与 Skill 数量，桌面壳、Python 包和安装器版本统一为 0.8.0；
- 新定时任务跨进程最多 15 秒热加载，开发模式 HTTP 服务可以干净退出。

## 0.7.0 — 桌面应用（2026-08-15）

投资系统现在可以安装为真正的 Windows 桌面应用：安装后双击桌面图标，在独立桌面窗口
内使用全部功能，不需要浏览器、CMD、PowerShell、Python 或 Node.js。

### 2026-08-17 密钥链路与 Agent 防循环热修复

- 设置页的 LLM API Key 改为与首次向导相同的 Windows DPAPI 加密存储；聊天适配器与
  独立投资 Agent 统一使用“环境变量优先、DPAPI 回退”的解析链路；
- 管理窗口新增 `configure_llm_api_key` 原生工具，可替用户配置已有供应商，工具事件、
  最终回答、错误和对话历史统一脱敏，旧历史中的密钥形态在读取时自动清理；
- 重复工具检测从事后事件观察层移到 Pydantic AI 工具执行中间件，相同工具与参数第二次
  调用会在副作用发生前拦截；连续 10 次只读搜索/检查也会提前停止；
- 修复 DSH/Ralph 与 Pydantic AI 2.27 的启动兼容（`output_type`/`result.output`、全局
  `RunContext`），增加受控分页源码读取、只读 `rg`、可恢复命令拒绝和可配置请求上限；
  DSH 工具角色固定使用兼容 function calling 的 `deepseek-chat`，不影响投资角色模型。

### 2026-08-17 新供应商配置与对话续接热修复

- 管理窗口新增 `configure_openai_compatible_provider` 原子工具：一次调用完成 OpenAI 兼容
  供应商校验、配置深合并、DPAPI 密钥保存、运行时刷新与可选连通测试；配置写入失败时
  自动恢复原密钥，工具事件、结果和错误保持脱敏；
- 默认加入 DeepInfra（`https://api.deepinfra.com/v1/openai`）及
  `deepseek-ai/DeepSeek-V4-Flash-0731` 模型，设置页同步展示；除非用户明确要求，新增供应商
  不会擅自替换当前主供应商；
- 当前消息现在被明确标记为本轮唯一任务。上一轮操作失败后，普通寒暄或要求先回应不会再
  自动续跑旧任务并触发连续只读工具保护；只有用户明确要求继续或重试才恢复旧操作；
- 新增真实 Agent 两轮工具链、工具参数脱敏、配置/密钥回滚和失败对话续接回归测试。

### 2026-08-17 长轮次活动检测与报告恢复热修复

- 完整投资轮次不再使用写死的 480 秒等待：命令进程每 10 秒写活动心跳，并按标的完成数、
  组合草案、风险辩论和最终组合决策发送有效进度；300 秒无活动才判定失联，同时保留
  1800 秒硬上限防止无限运行；
- 桌面每 5 秒的自治状态刷新改为直接读取轻量共享快照，不再把 `status` 命令塞入单线程
  投资队列；最新审计只返回摘要，避免数百 KB 响应和大量重复命令文件；
- 按钮/对话触发的轮次完成后先进入报告收件箱，在线客户端成功收到结果后确认删除；若
  页面断开或达到硬上限，报告仍会在下一次历史刷新时自动送达；
- 修复历史消息忽略原始时间、全部显示为页面加载时刻的问题，并将按钮与 Agent 触发报告
  正确标记为“手动整轮”。

### 核心交付

- **桌面窗口**：.NET 8 WPF + WebView2 承载现有全部页面（仪表盘/对话/报告/持仓/设置），
  底部状态栏实时显示 Agent/服务状态、模式、策略与轮次；单实例、最小化到托盘、登录自启动；
- **安装包**：Inno Setup 每用户安装（%LocalAppData%），捆绑可迁移 Python 3.11 + 全部离线
  依赖 + 便携 Node 20 + WebView2 兜底；桌面与开始菜单快捷方式；卸载保留数据；
- **数据分离**：代码在安装目录（只读），用户数据在 %LocalAppData%\InvestmentAuto；
  升级覆盖程序、保留账户/报告/记忆/自建工具；开发模式零改动；
- **安全**：每次启动动态回环端口 + 随机访问令牌（无凭证 401）；API Key 经 Windows
  DPAPI 加密存储，不落明文文件与日志；WebView2 拦截注入令牌，前端无感知；
- **首次向导**：10 步窗口内向导（旧 D 盘数据导入、密钥、模型、策略、模式、市场、风控、
  通知、初始化、完成）；旧数据只复制不删除、覆盖前备份、密钥转 DPAPI；
- **进程管理**：后台 pythonw 隐藏运行，Job Object 保证退出无孤儿进程；
- **桌面体验**：关闭窗口三选一（最小化到托盘 / 停止并退出 / 取消）；托盘菜单新增
  「开机自启」勾选开关（HKCU Run 键，无需管理员）；修复自启注册表路径丢失反斜杠的问题。

### 验证

- 迁移加固（2026-08-17）：旧 .env 迁移时清除机器相关变量（CONFIG_PATH 等，防止桌面
  应用被指回旧机器）、URI/口令类密钥一并转入 DPAPI；本机网页版 → 桌面版全量数据迁移
  实测通过（config/.env/账户/报告/记忆/选股/优化器/宏观/自建工具/交易审计/证据/指令
  历史/对话历史共 19 项复制、4 个密钥进 DPAPI、0 跳过、自动备份）；
- 修复捆绑 Python 非自包含的 P0 问题（2026-08-17）：构建时 `PYTHONNOUSERSITE=1` +
  `-s` + `--ignore-installed` 隔离 user-site，依赖真正装入捆绑目录（site-packages
  106 个包、`pip check` 无缺失、关键模块 `-s` 导入全过）；桌面后台进程统一
  `pythonw -s -m src.main`，运行时永不借用用户包；发行门禁新增捆绑自包含检查
  （pip check + import smoke + `python -s -m pytest`，此前失败项现已全绿）；
  另修：令牌注入收窄为本次启动的服务源（ready.Url + "/*"）、静默卸载不弹窗默认保留
  数据、verify-upgrade 排除易变 WebView2 缓存并接受幂等重装；
- 修复验收阻断项（2026-08-17）：①向导局部保存改为深合并，不再清空 llm/调度/风控/
  自主交易配置；②模型设置完整接线（服务商下拉数据、快速/深度模型写入、角色映射、
  "保存并测试"真实调用服务商）；③旧 D 盘迁移路径修复（D:\investment-auto）+ 向导
  记住检测到的源目录；④初始化模拟账户按文件存在性判断，portfolio.json 实际创建；
  ⑤首次向导期间只启动对话服务，投资 Agent 在向导完成后 5 秒内自动启动；
  另修：令牌注入仅限回环域名且不再进日志、Job Object 幂等复用、卸载"不保留"真正删除
  数据目录、release-check 失败退出码（已实测为 1）、build-desktop 补齐 publish、
  .sha256 与安装包同步；新增向导端到端测试 9 项（`tests/test_setup_flow.py`）；
- 修复主窗口"一直启动中"的三个根因（2026-08-16）：①ready 文件 URL 用 localhost，
  WebView2 解析为 IPv6 ::1 后对纯 IPv4 监听挂死 SYN_SENT——统一改 127.0.0.1；
  ②WebView2 运行时仅注册在 32 位注册表视图，x64 应用默认发现失败——新增
  WebView2Locator 双视图显式解析；③窗口在 StartAsync 之后才 Show，隐藏窗口上
  初始化 WebView2 永不完成——改为先显示窗口、托盘静默模式延迟到恢复窗口时初始化；
  另加孤儿浏览器进程清理、45 秒初始化超时与启动异常落盘；
- 捆绑运行时跑通全量 252 项 Python 测试（本次改动后 .venv 复跑同样 252 通过）；
- 桌面外壳新增 xUnit 自动化测试 16 项：单实例互斥、ready 文件解析（含小写键/畸形
  JSON/零端口）、pythonw 定位优先级、开机自启注册表往返、进程管理构造与首次运行标记
  （`dotnet test windows\desktop\InvestmentAuto.Desktop.Tests`）；
- 升级保数据自动化校验：`scripts\verify-upgrade.ps1` 快照数据目录 → 静默覆盖安装 →
  逐文件 SHA-256 比对（实测 16/16 文件字节一致、程序文件已替换）；
- 一键发行候选门禁：`scripts\release-check.ps1` 自动执行 C# 测试 + .venv/捆绑运行时
  252 项 pytest + 升级保数据 + 输出安装包 SHA-256，任一失败退出码非 0；
- 安装器界面中英双语（ChineseSimplified.isl，Inno 官方翻译）；
- 本机实测：静默安装、快捷方式、安装目录启动、动态端口、ready 文件、令牌 401/200；
- 待办：干净 Windows 10/11 虚拟机全流程验收（清单见 docs/DESKTOP_USAGE.md）。

---

## 0.6.0 — 管理 Agent 一键自建工具闭环（2026-08-15）

管理 Agent 现在可以**完全自动地为自己添加缺失的工具**：说出「给自己增加一个查询 ××
的工具」，它调用一次 create_manager_tool 即可完成全部流程：

```text
校验输入 → 写入独立工具模块 → 导入并核对函数签名与参数 Schema
→ 编译/测试（白名单命令）→ 注册工具清单 → 试调用验证
→ 任一步失败：代码文件与工具清单一并回滚
```

- 单一原子调用替代原来的「改文件 → 手动测试 → 手动注册」多步流程，不再消耗大量
  工具/请求额度（此前常触发 20/40 上限并搜索文件绕圈）；
- 新工具从**下一条消息**自动可用（对话运行时每轮重建工具集）；试调用在工具内部
  完成，无需模型同轮再次调用；
- 同名/内置名冲突、Schema 非法、签名与 Schema 不符、测试失败均在写盘前或回滚时
  拦截；uninstall_manager_tool 支持完整卸载；
- 工具模块由文件路径直接加载（不受包路径缓存影响）；运行时安装的工具不入库；
- 工具代码禁止导入交易/账户/命令执行模块（AST 检查在写盘前拦截），管理 Agent 的
  无实盘权限边界不因自建工具而放宽；
- 并发会话创建同名工具由进程内锁串行化；
- **单轮闭环**：创建时携带用户当前需求的调用参数（fulfill_args），工具内部创建
  成功后立即执行并返回结果，无需用户再发第二条消息。

### 其它

- 管理 Agent 上限默认放宽为 32 次模型请求 / 64 次工具调用，并可在设置页调整；
- 双模型（快速/深度）设置移至设置页顶部「投资者控制中心」；
- 新增 reset_paper_account 工具：原子化重置指定模拟市场账户（自动加锁、备份、
  仅模拟盘可用）；
- 收盘补跑默认不交易（trade_on_catch_up: false）；修复 Windows checkpoint
  瞬时文件占用。

全量测试 **225 项通过**。

### 真实 API 实测（DeepSeek，2026-08-15）

- 请求「帮我对比贵州茅台和宁德时代过去 30 个交易日的价格走势相关性」：模型自主判断缺能力，
  **恰好一次**调用 create_manager_tool，创建 compare_price_correlation，用真实历史数据算出
  收盘价相关性 0.8051 / 日收益率相关性 0.0296，nonce 同时出现在工具返回与最终答案 → **PASS**；
- 请求「持仓行业分布」：模型正确判断现有工具足够，直接回答（create_tool_calls=0），不为创建而创建；
- 评估创建的工具在 finally 中自动卸载，注册表与源码目录验证干净；
- 创建流程实测 token 消耗约 22.5 万，eval 预算默认提高到 60 万（--total-tokens 可调）。

### 复核修复（第二轮）

- 试调用成为强制步骤：未提供 test_args 时以空参数执行；试调用失败（含返回值不可
  JSON 序列化）整体回滚，坏工具不会留在注册表；
- fulfill 失败时状态明确为 created_fulfill_failed（工具保留、闭环状态如实标记）；
- 回滚后核验源码与清单确实消失；核验失败返回 rollback_failed 并禁用清单；
- uninstall_manager_tool 完整卸载：清单 + 源码 + import 缓存，同名工具可重建；
- 按工具名线程锁 + 跨进程文件锁（含陈旧锁回收）；async 工具函数被拒绝；
- 新增 Agent 级执行链测试：scripted 假模型驱动真实 MANAGER_AGENT 运行循环，验证
  「一次 create_manager_tool 调用 → fulfill_result 同轮回传模型」（该测试证明执行链，
  不证明模型自主判断；自主判断由 scripts/eval-manager-tooling.py 可选真实 API 评估覆盖）；
- 管理指令强化：现有工具无法完成的可复用需求必须自建工具，不得直接回答做不到。

### 复核修复（第三、四轮）

- 试调用/fulfill 契约：只有 fulfill_args 时一次执行兼任试调用与 fulfill；test 与
  fulfill 参数相同只执行一次；两者都没有且存在必填参数时写盘前报明确参数错误；
- 并发锁改为**操作系统级文件锁**（msvcrt.locking / fcntl.flock）：内核原子、进程
  退出自动释放，彻底消除陈旧锁检查-删除的 TOCTOU 窗口；创建与卸载共用同一套锁；
- 跨进程锁互斥测试（multiprocessing + 同步屏障验证临界区不重叠）；创建/卸载竞争
  测试补断言（无异常、双方至少各成功一次）；
- 写盘前必填参数检测覆盖关键字参数（def run(*, market: str)）；
- 签名校验补全：schema 属性必须全部是函数参数、无默认值参数必须列入 required、
  拒绝 *args/**kwargs 与仅位置参数；
- def 匹配改为多行行首锚定，首行即 def 的合法模块不再误判；
- eval 脚本强化：nonce 校验码要求同时出现在工具返回与最终答案，事件流捕获
  create_manager_tool 调用，pass/fail 与退出码，finally 自动卸载测试工具（--keep 保留）。

---

## 0.5.1 — 下单 fail-safe 修复（2026-08-14）

复核发现 0.5.0 的下单 fail-safe 存在三处真实漏洞，全部修复：

### 1. 显式执行状态机（修复核心漏洞）

旧实现中研究工作流提前把 checkpoint 标记为 completed，导致「进入执行但成交未确认」的
checkpoint 对恢复扫描不可见，崩溃重启后可能重放订单。现改为显式状态机：

```text
running → research_completed → execution_pending → completed（终态）
```

- 研究工作流只能标记 research_completed，执行状态完全由调度器持有；
- 标记 execution_pending 会翻转状态，恢复扫描必然发现；
- 任何 execution_pending 的 checkpoint 都会让**下一整轮冻结**（broker 不被调用），
  随后丢弃该 checkpoint 并写入审计原因；
- **fail-closed 写入**：pending 标记写失败 → 拒绝下单；恢复检查异常 → 冻结；
  completed 标记失败 → 告警并让下一轮安全冻结。

### 2. 冻结闸门无时效限制

冻结扫描曾复用 90 分钟恢复窗口，超过 90 分钟的 pending 会被漏掉（相邻轮次间隔
本身就可能超过 90 分钟）。现在 list_execution_pending 扫描**任何年龄**的未确认
pending；普通 running/research_completed 恢复仍保留 90 分钟时效窗口。

### 3. 输入指纹补全

checkpoint 恢复前的输入指纹现在覆盖：账户现金与持仓、完整授权书、市场交易规则、
交易配置（佣金/滑点/整手/T+1）与全部风控限制。现金或持仓变化即作废旧研究决策。

### 其它修复

- 工具模式专属 system prompt（不再与「输出纯 JSON、不调用工具」冲突）；
- tool 模式剥离 response_format、回传工具参数用 JSON 字符串（OpenAI 兼容线格式）；
- 证据库路径统一正斜杠，Linux/Docker 下可加载；
- architecture.research.*（enabled/shell/workspace/max_rounds/timeout）全面接线，
  管理面研究工具同样受其约束；shell 超时由配置封顶；
- bugfix 任务提示改用带引号的正斜杠 pytest 路径（shlex 不吞路径）；
- cycle_evidence 返回真实证据 ID；证据归档失败升级为 warning 并写入审计；
- checkpoint 索引并发锁、阶段元数据剥离、最新优先选择；
- 新增 citation_attach_upstream 开关；sandbox 边界文档如实表述（启发式约束，
  非 OS 沙箱，仅限可信环境）；
- 未配置 architecture: 段时所有新行为默认关闭，旧配置零风险升级。

全量测试 **194 项通过**，其中 fail-safe 相关测试覆盖全部崩溃窗口：
正常路径状态演进、pending 冻结（broker 不被调用）、研究完成恢复、
以及 91 分钟与 7 天超龄 pending 冻结。

---

## 0.5.0 — 架构升级（2026-08-14）


采用 DSH 式分层架构对交易工作流与离线研发能力做了一次系统性升级。全部新功能由
`config/config.yaml` 的 `architecture:` 段控制；**未配置该段时行为与旧版完全一致**，
可随时整体回退。全量测试 185 项通过。

### 新增功能

**1. 工具中介交互（Agent 决策节点）**

关键决策角色（研究经理、逐标的交易员、风险经理、投资组合经理）不再"一次性生成 JSON
然后校验失败就整次重试"，改为原生 function calling：

- `list_evidence_ids`：先查询合法证据目录再引用，杜绝编造 ID；
- `submit_analysis`：提交时当场校验引用，错误信息原路返回，模型原地修正。

**2. 引用自动修复**

此前记录的 Agent 失败中约 84% 是引用格式问题（漏轮次后缀、写裸角色名、漏引必须上游）。
现在对可判定的错误自动修复并在审计中记录 `citation_repairs` 痕迹，只有真正无法修复
的输出才会重试——直接消除大部分重复 LLM 调用。

**3. 摘要跨界 + 证据库**

- 完整证据图（各角色报告、多空辩论、风险讨论）每轮归档到
  `runtime/trading/evidence/`，审计文件体积从一个数量级回到 ~50KB；
- 下游提示词只携带压缩摘要（`evidence_text_max_chars`，默认 16000 字符），降低每轮
  token 成本与延迟；
- 管理对话可经 `evidence_ref` 回溯任意决策的原始证据。

**4. 可续工作单元（Checkpoint）**

- 每个研究阶段原子落盘 `runtime/trading/checkpoints/`，超时、崩溃或进程重启后**只续跑
  未完成的阶段**，不再整轮重来；
- 下单执行采用 fail-safe 原则：恢复轮次若上一次成交未确认，**绝不重放下单**，由下一轮
  自然补上（重复成交比漏单更危险）。

**5. 离线研究循环（Ralph 模式）**

全新的研究平面，让项目可以离线自我验证与进步：

```bash
# 规则回测
python -m src.main research --task backtest --market cn \
  --objective "验证 5 日动量规则在 A 股的有效性" --max-rounds 6

# 策略参数实验
python -m src.main research --task strategy_experiment \
  --objective "寻找低波动因子权重组合" --max-rounds 6

# 自动修 Bug（复现 → 修改 → 全量测试 → 失败自动回滚）
python -m src.main research --task bugfix \
  --objective "修复 XX 模块的 YY 缺陷" --max-rounds 6
```

- 每轮启动**全新 Agent（无对话记忆）**，工作区 `runtime/research/workspace/` 是唯一
  长期记忆，轮间只传递有界结构化报告；
- **受限 shell**：可执行文件白名单 + argv 直执行（免疫 shell 元字符注入）+ 路径参数
  边界校验 + 最小化环境（不含生产密钥）。注意：这是启发式约束而非 OS 级沙箱，python -c
  代码字符串中的路径不受约束，研究 Agent 理论上可读取项目外文件；请仅在可信环境手动运行。
  交易执行路径与管理对话 Agent 永远没有 shell；
- 研究结论要进入生产配置，必须经 `apply_experiment_to_config`（SHA-256 记录、版本
  备份、全量测试、失败自动回滚）。

**6. 管理能力扩展**

研究工具（回测、实验、结果查询、配置应用）可通过能力注册表安装给管理 Agent，管理
Agent 工具目录新增 6 项管理面工具（项目搜索、能力目录、周期证据、技能安装等）。

### 修复与优化

- 修复 2 个过时测试（自主交易流程测试与管理工具目录测试）；
- 版本号单点化：`src/version.py` 为唯一版本来源；
- `.env.example` 中过时的调度器注释已修正；
- 未入库的管理面重构工作已作为 checkpoint 提交。

### 配置说明

新增 `architecture:` 配置段（默认值即当前推荐）：

```yaml
architecture:
  citation_auto_repair: true
  tool_mediated: true
  tool_mediated_roles: [research_manager, trader, risk_manager, portfolio_manager]
  tool_retries: 2
  evidence_store: true
  evidence_text_max_chars: 16000
  checkpoint_cycles: true
  resume_stale_minutes: 90
  research:
    enabled: true
    max_rounds: 6
    shell: restricted   # restricted | none
    workspace: runtime/research
```

### 兼容性

- 旧配置文件（无 `architecture:` 段）行为与 0.4.0 完全一致，升级零风险；
- 新增运行时目录（`runtime/trading/evidence/`、`runtime/trading/checkpoints/`、
  `runtime/research/`）均已加入 .gitignore；
- 新功能在投资 Agent 进程重启后生效。

---

## 0.4.0 及更早

（未维护更新日志，历史见 git 提交记录。）
