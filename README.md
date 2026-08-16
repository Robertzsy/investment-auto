# investment-auto

**多市场（A股/港股/美股/ETF）自动化模拟交易系统**

- 支持 A股、港股、美股、场内ETF 独立账户模拟交易
- 内置多 Agent 分析（技术面、基本面、情绪、宏观）与辩论决策
- AI 自主模拟交易闭环：目标仓位决策、硬风控审批、撮合、资金与持仓更新
- 全市场自主选股：流动性/价格/市值/估值硬筛选，多因子评分后交给 Agent 复筛
- 组合优化层：马科维茨、Black-Litterman、风险平价、压力测试
- 多模型接入：OpenAI (GPT)、DeepSeek、GLM (智谱)、Kimi (月之暗面)
- Python + Docker 一键部署，MongoDB 加速选股查询并在不可用时自动回退 JSON

> **Windows 桌面版 v0.7.0 已发布：** 安装后可直接从桌面启动，内置 Python、Node.js 和全部运行依赖，无需浏览器或 PowerShell。
>
> [下载安装包与查看更新说明](https://github.com/Robertzsy/investment-auto/releases/tag/v0.7.0)

## v0.7.0 重大更新

从 v0.4.0 到 v0.7.0，Investment Auto 完成了投资 Agent 架构、管理 Agent、自主交易安全机制和 Windows 桌面应用的系统性升级。本次升级包含 44 个提交、107 个文件变更，新增约 11,000 行代码。

### 1. Agent 工作流架构升级

关键投资角色不再一次性生成整段 JSON，而是通过原生 Function Calling 完成决策提交和校验：

- 研究经理、逐标的交易员、风险经理和投资组合经理采用工具中介交互；
- `list_evidence_ids` 用于查询合法证据目录，`submit_analysis` 在提交时立即校验证据引用和输出结构；
- 校验失败后模型可以原地修正，不再重新运行整个分析角色；
- 自动修复漏写轮次后缀、裸角色名和漏引上游证据等常见引用错误；
- 所有自动修复都会写入 `citation_repairs` 审计记录。

这套机制减少了重复模型调用、Token 消耗和格式错误造成的整轮失败。

### 2. 证据库与上下文隔离

完整分析证据独立归档到 `runtime/trading/evidence/`。每个下游 Agent 只接收压缩摘要和必要的证据引用，不再携带所有上游原始输出。

- 不同股票的研究上下文相互隔离；
- 完整证据仍可通过 `evidence_ref` 回溯；
- 审计文件体积显著降低；
- 减少长上下文带来的 Token 成本与延迟；
- 避免不同标的和角色之间的信息污染。

### 3. 可恢复的投资工作单元

每轮投资分析按照阶段原子写入 `runtime/trading/checkpoints/`。发生超时、程序崩溃或系统重启后，只继续未完成阶段，不再从选股开始整轮重跑。

```text
running → research_completed → execution_pending → completed
```

系统分别记录研究完成、准备下单和成交确认状态。

### 4. 下单 Fail-safe 安全机制

恢复投资轮次时，如果发现上一轮已经进入下单阶段、但无法确认是否成交，系统不会重复提交模拟订单。

- 所有未确认的 `execution_pending` 状态永久可见，不受 90 分钟普通恢复窗口限制；
- 检测到未确认执行时冻结整个新轮次，且不会调用模拟 Broker；
- `pending` 状态写入失败或恢复扫描异常时，按照 Fail-closed 原则停止执行；
- 成交完成状态写入失败时发出警告，并让下一轮自动冻结；
- 账户现金、持仓、授权书、市场规则和全部风险参数都会进入恢复指纹；
- 指纹变化后不会继续使用旧研究结果。

该机制用于防止程序重启后出现重复成交。

### 5. 管理 Agent 自动创建工具

管理对话 Agent 现在可以根据任务自动发现能力缺口，并为自己创建项目内工具：

```text
识别能力缺口 → 生成工具代码和参数 Schema → 校验权限与签名
→ 原子写入 → 编译与测试 → 注册 → 强制试调用 → 完成当前请求
```

- 创建、测试、注册和调用在一个事务内完成；
- 新工具创建后可以在同一轮完成用户当前需求；
- 试调用失败时自动删除源码和注册记录，并核验回滚结果；
- 支持完整卸载工具及重新创建同名工具；
- 拒绝异步函数、可变参数和不匹配的 Schema；
- 创建与卸载共享操作系统级文件锁：Windows 使用 `msvcrt.locking`，Linux 使用 `fcntl.flock`；
- 进程退出后锁自动释放，不存在陈旧锁竞争窗口；
- 使用真实 API Eval 验证模型能主动发现能力缺口并完成同轮回答。

管理 Agent 仍不能绕过模拟交易边界，也不能直接操作真实证券账户。

### 6. 离线研究循环

新增独立的离线研究平面，可用于策略规则回测、参数组合实验和自动复现/修复 Bug：

```bash
python -m src.main research --task backtest --market cn --objective "验证5日动量规则" --max-rounds 6
python -m src.main research --task strategy_experiment --objective "寻找低波动因子权重组合" --max-rounds 6
python -m src.main research --task bugfix --objective "修复指定模块问题" --max-rounds 6
```

每轮研究使用全新的 Agent 上下文，长期信息只通过受控工作区传递。研究结论进入生产配置前，必须经过版本化变更管理器：生成变更、SHA-256 校验、备份、测试，测试失败自动回滚。

研究 Shell 采用命令白名单、参数边界和最小化环境，但属于启发式限制，不是操作系统级沙箱，只应在可信环境中运行。

### 7. 自主交易控制增强

- 手动模式与全自动模式使用同一条完整投资链；
- 一次触发自动完成选股、研究、辩论、组合决策、风控、模拟成交和报告；
- 自动轮次完成后将报告发送到管理对话，并支持外部 Webhook；
- 支持暂停、恢复、紧急停止和一键重置指定市场的模拟账户；
- 收盘补跑默认只生成分析，不执行交易；
- 保守、中立和激进策略同时使用提示词目标与代码硬限制；
- 每轮保存不可变的投资授权书快照；
- 快速模型和深度模型可按角色分别配置；
- 管理 Agent 的模型请求和工具调用额度可在设置中调整。

### 8. Windows 桌面应用

Investment Auto 现在提供真正的 Windows 桌面安装版。安装完成后可直接双击桌面图标打开独立窗口，不再依赖浏览器、CMD 或 PowerShell。

- .NET 8 WPF + WebView2 桌面外壳；
- 仪表盘、AI 对话、持仓、报告和设置页面；
- 单实例运行、系统托盘和开机自动启动；
- 自动选择动态回环端口，每次启动生成随机访问令牌；
- 自动启动和管理后台投资 Agent，退出时清理关联进程；
- 更新为专业投资主题应用图标。

### 9. 完整自包含运行环境

安装包已经捆绑 Python 3.11、全部 Python 依赖、Node.js 20、.NET 8 桌面运行环境和 WebView2 兜底安装程序。

后台 Python 统一使用 `pythonw.exe -s -m src.main` 并设置 `PYTHONNOUSERSITE=1`，确保不会借用用户电脑上已经安装的 Python 包。用户无需预装 Python、Node.js，也不需要运行 `Setup-Windows.cmd`。

### 10. 首次启动配置向导

首次启动时可以在桌面窗口中完成：

- 检测并导入旧版 `D:\investment-auto` 数据；
- 配置模型服务商、API Key、快速模型、深度模型和角色映射；
- 配置投资策略目标、手动/自动模式和启用市场；
- 初始化模拟账户并配置通知 Webhook；
- 完成后自动启动投资 Agent。

旧数据只复制、不删除，覆盖前自动备份。首次配置完成前只启动管理服务，不会提前启动自动投资。

### 11. 数据分离与安全

```text
程序目录：%LocalAppData%\Programs\InvestmentAuto
用户数据：%LocalAppData%\InvestmentAuto
```

模拟账户、持仓、成交历史、配置、Agent 记忆、报告、审计记录和自建工具均保存在用户数据目录。

- API Key 使用 Windows DPAPI 加密保存，不写入普通配置文件或日志；
- 服务只监听本机动态回环端口；
- WebView2 只向本次启动的服务地址注入访问令牌；
- 日志中的令牌会被隐藏；
- 软件升级不会覆盖用户数据；
- 卸载时可以选择保留或删除用户数据，静默卸载默认保留。

### 12. 桌面版关键问题修复

- `localhost` 被解析为 IPv6 导致 WebView2 无法连接，统一改为 `127.0.0.1`；
- WebView2 只注册在部分注册表视图时无法发现，增加 32/64 位双视图检测；
- 隐藏窗口中初始化 WebView2 导致启动卡死，调整为先显示窗口再初始化；
- 配置向导保存覆盖其他配置，改为递归深合并；
- 补齐服务商、快速/深度模型和角色映射设置；
- 修复旧数据迁移路径、账户初始化和首次配置前提前启动投资进程；
- 修复捆绑 Python 借用构建机 User Site、令牌注入范围过大和静默卸载弹窗；
- 升级校验排除 WebView2 缓存并正确处理同版本覆盖安装；
- 安装包 SHA-256 与实际文件同步生成。

### 13. 安装、升级与卸载

- 每用户安装，不需要管理员权限；
- 自动创建桌面和开始菜单快捷方式；
- 支持覆盖安装和版本升级；
- 更新程序时保留账户、报告、配置、记忆和自建工具；
- 卸载时可以选择是否删除用户数据；
- 提供中文和英文安装界面。

### 14. 测试与发行门禁

新增一键发行检查：

```powershell
powershell -ExecutionPolicy Bypass -File scripts\release-check.ps1
```

门禁覆盖 C# 桌面测试、开发环境 Python 全量测试、捆绑 Python `pip check`、禁用 User Site 后的关键模块导入、捆绑运行时全量测试、静默安装、覆盖升级、用户数据逐文件 SHA-256 对比，以及安装包大小和哈希清单。

当前 v0.7.0 安装包已经通过完整自动化发行门禁：C# 测试 18/18、开发环境与捆绑运行时 Python 测试均为 261 项通过。

### 15. 兼容性

- 系统仍然只支持模拟交易，不连接真实券商；
- 原有命令行和 Docker 部署方式继续保留；
- 没有 `architecture:` 配置段的旧配置可以维持旧版行为；
- MongoDB 不可用时仍会自动回退本地 JSON；
- 原有 `D:\investment-auto` 项目不会在数据导入时被删除；
- v0.4.0 Release 继续保留，可用于旧版启动器部署。

### Windows 桌面版下载

[下载 Investment Auto v0.7.0](https://github.com/Robertzsy/investment-auto/releases/tag/v0.7.0)

安装包：`InvestmentAuto-Setup-x64.exe`

SHA-256：

```text
F2EDB6DF789BC825E7C3B05289A4D6AB8EA5C25AAD4E2777CE540864799D0A3E
```

## 从源码运行

**运行依赖：** Python 3.10+ 与 Node.js 18+。Windows 可先执行 `python --version`、`node --version` 确认；系统启动时也会主动检查 Node.js。宏观采集默认使用 Node 内置 HTTPS；检测到 HTTP(S) 代理时会使用系统 `curl` 作为代理后端（Windows 10/11 自带，Linux 精简系统需另行安装）。

```bash
# 1. 克隆仓库
git clone <repo-url> && cd investment-auto

# 2. 配置环境
cp .env.example .env          # 填入你的 API Key
vim config/config.yaml        # 选择市场、轮次、模型

# 3. 安装 Python 依赖
pip install -r requirements.txt

# 4. 初始化模拟账户
python -m src.main init

# 5. 启动独立投资 Agent（调度、完整投资链、反思和命令 Worker）
python -m src.main run

# 6. 在另一个终端启动管理对话面板
python -m src.main chat
# 打开浏览器访问 http://localhost:8080
# chat 不再启动调度器；关闭或重启对话窗口不会打断自动投资。
# 默认使用共享命令队列与独立投资进程通信；仅测试或兼容旧环境时可设置 INVESTMENT_AGENT_TRANSPORT=local。

# 7. 手动运行组合优化器（不传 symbols 时使用持仓 + 默认标的池）
python -m src.main optimizer --market cn
python -m src.main optimizer --market cn --symbols 600519,000858,601318,600030

# 7a. 只刷新选股池（读取行情并评分，不会下单）
python -m src.main screen --market cn

# 8. 手动补跑今天已错过的轮次 / 生成宏观日报
python -m src.main catchup --market cn
python -m src.main macro

# 9. 单次分析
python -m src.main once --market cn

# 10. 手动运行一次自主决策（先用 dry-run 检查）
python -m src.main autonomous --market cn --dry-run
python -m src.main autonomous --market cn

# 11. 少量人工干预：状态、暂停、恢复和紧急停止
python -m src.main status
python -m src.main pause --reason "人工检查"
python -m src.main resume
python -m src.main kill --reason "异常行情"
python -m src.main reset-kill
```

## 配置要点

- `config/config.yaml`：总控制文件，决定市场开关、轮次、启动补跑、宏观日报和模型选择
- 环境变量 `.env`：存储 API Key，切勿提交至 Git
- `config/market/`：各市场风控参数、手续费、交易规则
- `runtime/optimizer/`：组合优化结果；仪表盘会读取最新结果
- `runtime/screener/`：全市场候选缓存、因子分数和各市场最新入选结果
- MongoDB（可选）：`securities`、`market_snapshots`、`screening_factors`、`screening_runs` 保存可索引的选股数据；连接异常不阻断调度
- `runtime/macro/`：独立宏观日报数据与报告
- `runtime/trading/control.json`：人工暂停与紧急停止状态
- `runtime/trading/audit/`：每轮 Agent 意见、主席决策、风控拒绝与真实模拟成交审计
- `runtime/logs/investment-auto.log`：聊天与调度统一日志
- APScheduler 的星期编号以周一为 `0`。A股/港股/ETF 和美股北京时间晚间轮次使用 `0-4`（周一至周五）；美股北京时间凌晨轮次使用 `schedule.us_early_morning_days`，默认 `1-5`（周二至周六）。设置 `weekdays_only: false` 可允许每日触发。

## AI 自主模拟交易

自主模块采用“确定性选股、AI 决策、代码风控、模拟经纪执行”的分层结构：

1. A 股/港股/ETF 从新浪市场中心、美股从 Nasdaq Screener 获取按流动性排序的全市场候选；接口异常时回退到优化器默认标的。
2. 代码先排除 ST/退市、特殊证券、低价、低流动性、过小市值、极端涨跌和超出估值门槛的标的，再按动量、趋势、流动性、估值、量能和低波动综合评分。
3. 每只证券建立独立研究状态：市场技术、市场情绪、新闻事件和基本面 Agent 并行形成证据化报告；多头先陈述、空头回应，研究经理裁决后由逐标的交易员输出 BUY/HOLD/SELL 候选建议。不同股票的中间报告不会互相污染。
4. 投资组合经理先汇总全部交易员建议形成组合草案；激进、保守、中立风险角色依次回应前一意见，风险经理裁决后，投资组合经理才输出允许标的的最终目标仓位、置信度与依据，不能直接修改账户。
5. 硬风控重新计算订单，执行允许池、最低置信度、单标的仓位、单笔金额、单轮换手、现金储备、当日交易次数和最大回撤限制。
6. 模拟经纪处理滑点、佣金、印花税、整手、T+1 可卖数量、现金、持仓成本和成交历史；代码止损止盈优先于 AI 主观决策。

“跑一轮美股 / A 股 / 港股 / ETF”会一次执行完整投资轮次，不再停在选股步骤：系统先从全市场筛出优质候选，将候选与已有持仓一起交给 13 角色工作流逐只给出买入、观望/持有或卖出决策，再经过硬风控与模拟撮合，最后生成逐标的报告并尝试投递通知。聊天页和设置页顶部均提供醒目的双模式开关：`manual` 仅在人工一次触发时运行整轮；`automatic` 按计划时间自动运行同一条完整链。两种模式均不要求逐步确认。

在设置页的“自主选股”区域可修改候选数量、刷新频率、分市场价格/成交额/市值门槛、估值上限和因子权重。系统固定从全市场发现候选，再应用这些筛选条件。最近结果会显示在 Dashboard 的“AI 自主选股池”，也会写入每轮交易审计。

13 个不同角色组成 14 个执行节点（投资组合经理分别生成风控前草案和风控后终稿），强制证据引用及多空/风险顺序辩论采用内置推荐配置，不在设置页暴露。系统为行情、技术指标、逐标的新闻、基本面、宏观、筛选、账户和上游 Agent 报告生成稳定证据 ID；每条事实判断及每个组合决策必须引用有效 ID，伪造、漏引或跳过直接上游会重试后阻断。Agent 链失败时所有主观交易关闭，但硬止损、移动止损和最大回撤清仓仍可由代码独立执行。

投资反思采用结果型记忆：本轮结论只先写为待评估记录，系统在后续轮次获得真实 T+1/T+5/T+20 行情后才生成可供 Agent 使用的经验；旧版未经结果验证的自我总结不会再加载。研究数据与反思记录始终保留 JSON 审计副本，配置 `MONGODB_URI` 后也写入 MongoDB 加速查询。美股逐标的新闻和扩展基本面可通过设置页填写 `FINNHUB_API_KEY`；未配置或接口失败时形成明确数据缺口，模型不得补写公司事件。

只读验证完整 Agent 图（使用虚构空账户，不下单）可运行：`python scripts/smoke-agent-workflow.py --market us --symbol NVDA`。

同一区域可以选择选股存储后端。默认 `auto`：设置 `MONGODB_URI` 后优先使用 MongoDB，并自动创建复合唯一索引和市场/时间/分数查询索引；未配置、连接超时或写入失败时继续使用 `runtime/screener/` 中的 JSON，不会阻塞交易。直接使用 Docker Compose 时已内置 MongoDB，无需单独配置 URI；本地 Python 运行可填 `mongodb://127.0.0.1:27017`。

整轮报告始终保存在 `runtime/reports/`。需要推送时启用 `notify.enabled` 并设置 `NOTIFY_WEBHOOK_URL`；系统会向 webhook 发送报告标题、正文、文件名和市场元数据。推送失败会写入轮次结果，但不会回滚已经完成的模拟成交。

如果本机 27017 已被其他项目占用，可以单独启动在其他仅回环端口，例如 `docker run -d --name investment-auto-mongodb --restart unless-stopped -p 127.0.0.1:27018:27017 -v investment-auto-mongodb-data:/data/db mongo:7 --bind_ip_all --quiet`，再设置 `MONGODB_URI=mongodb://127.0.0.1:27018`。

公开仓库默认 `autonomous.enabled: false`。首次启用建议：

```yaml
autonomous:
  enabled: true
  auto_execute: true
```

也可通过 `AUTONOMOUS_TRADING_ENABLED=true` 覆盖开关。建议先运行
`python -m src.main autonomous --market cn --dry-run`，检查
`runtime/trading/audit/` 后再允许自动成交。

安全边界：

- 自主执行强制要求 `trading.mode: paper`，代码拒绝其他模式。
- 启动补跑默认不交易，避免使用历史计划时间执行过期订单。
- `kill` 会同时暂停并锁定恢复；必须先 `reset-kill`，再执行 `resume`。
- AI 不能绕过硬风控，也不能交易本轮全市场筛选池和已有持仓以外的代码。
- 当前行情来自公开第三方接口，因此系统适合研究与模拟，不应用于真实资金。

## Docker 部署

```bash
docker compose up -d --build
# 同时启动 scheduler 和 chat 两个服务
docker compose ps
docker compose logs -f scheduler chat

# AI 对话面板 → http://127.0.0.1:8080
```

Compose 中聊天服务在容器内监听 `0.0.0.0:8080`，但端口只发布到宿主机回环地址 `127.0.0.1:8080`，不会直接暴露到局域网或公网。`config/` 与 `runtime/` 由两个服务共享挂载；`.env` 仅作为运行时环境文件使用，不会进入镜像构建上下文。

## 架构升级（0.5.0）

系统采用 DSH 式的分层架构，全部由 config/config.yaml 的 architecture: 段控制，未配置该段时行为与旧版完全一致（可整体回退）：

- **工具中介交互**：tool_mediated_roles 中的角色通过原生 function calling 工作——list_evidence_ids 查询合法证据目录，submit_analysis 当场校验引用并返回具体错误，模型原地修正，不再整次重试。
- **引用自动修复**：citation_auto_repair 对可判定的引用格式错误（漏轮次后缀、裸角色名、漏引必须上游）自动修复并在审计中记录 citation_repairs；不可修复才重试。
- **摘要跨界**：完整证据图归档到 runtime/trading/evidence/，审计与下游提示词只携带压缩摘要（evidence_text_max_chars），管理工具可经 evidence_ref 回溯任意决策的原始证据。
- **可续工作单元**：checkpoint_cycles 开启后每个研究阶段原子落盘 runtime/trading/checkpoints/，超时、崩溃或重启只续跑未完成阶段；下单执行采用 fail-safe——未确认成交的恢复轮次绝不重放下单，由下一轮自然补上。

## 离线研究循环（Ralph 模式）

```bash
python -m src.main research --task backtest --market cn --objective 验证5日动量规则 --max-rounds 6
python -m src.main research --task strategy_experiment --objective 寻找低波动因子权重组合 --max-rounds 6
python -m src.main research --task bugfix --objective 修复XX模块缺陷 --max-rounds 6
```

- 每轮启动全新 Agent（无对话记忆），工作区 runtime/research/workspace/<run>/ 是唯一长期记忆，轮间只传递有界结构化报告。
- 任务类型：backtest（确定性规则回测，不复用生产账户）、strategy_experiment（参数对比实验）、bugfix（复现→修改→全量测试→失败自动回滚）。
- **受限 shell**：研究循环的命令执行采用可执行文件白名单 + argv 直执行（无 shell 元字符）+ 路径参数边界校验 + 最小化环境（不含生产密钥）。注意：这是启发式约束而非 OS 级沙箱——python -c 代码字符串中的路径不受边界校验约束，研究 Agent 理论上可读取项目外文件（包括 .env 密钥文件）；请仅在可信环境手动运行研究循环。交易执行路径与管理对话 Agent 永远没有 shell。
- 研究结论要进入生产配置时，必须经 apply_experiment_to_config（版本化变更管理器：SHA-256 记录、版本备份、全量测试、失败自动回滚）。

## 多模型接入

系统默认接入以下主流模型，可在 `config.yaml` 中直接指定：

- OpenAI: `gpt-5.6-sol` / `gpt-5.6-terra` / `gpt-5.6-luna`
- DeepSeek: `deepseek-v4-pro` / `deepseek-v4-flash`
- GLM (智谱): `glm-5.2`
- Kimi (月之暗面): `kimi-k3`

## 管理对话与投资 Agent 边界

对话面板是管理控制面，独立投资 Agent 是执行面。对话层只负责使用、管理和修改投资 Agent，
不再保留关键词业务路由或另一套选股/风控/交易流程。完整轮次、暂停恢复、模式与策略切换都通过
统一命令契约执行；Docker 中通过共享命令队列跨进程通信。

- 对话管理 Agent 可以读取和修改投资 Agent 的代码、配置、提示词与工作流，但只能修改受控目录。
- 代码修改要求读取后的 SHA-256，保存版本化备份并执行全量测试；测试失败自动回滚。
- 管理 Agent 没有任意 Shell、实盘交易、密钥、账户历史或控制审计覆盖权限。
- 对话请求全部使用原生语义 function calling，不再使用中文关键词匹配分流。
- 每轮限制模型请求数、工具调用数与总 Token；相同工具、参数和结果重复两次会安全停止。
- 交易建议仍必须经过标的池、仓位、现金、换手、每日次数、止损止盈、最大回撤及撮合规则。

## 管理 Agent 自建工具闭环（create_manager_tool）

管理 Agent 可以**自动为自己添加缺少的工具**。当用户的需求现有工具无法完成、且该需求是稳定可复用的能力时，
Agent 会自行生成工具代码，并在同一条消息内完成创建与回答，无需人工参与：

```text
校验输入（名称/Schema/禁止导入清单/async 禁用）
→ 写入独立工具模块 src/manager/tools/<name>.py（原子写，拒绝覆盖）
→ 导入并核对函数签名与 Schema（属性⊆参数、无默认值参数必须 required、拒绝 *args/仅位置参数）
→ 编译/测试（白名单命令）
→ 注册工具清单 runtime/manager/capabilities/tools/
→ 强制试调用（失败即整体回滚，回滚后核验代码与清单确实消失）
→ fulfill：携带用户当前需求的调用参数立即执行，结果同轮回传模型作答
```

**使用方式**：直接在对话中说“给自己增加一个查询 ×× 的工具”，或直接提出需求（如“帮我对比茅台和宁德时代
过去 30 天的走势相关性”），Agent 会自主判断缺能力、创建工具并用 fulfill_result 在同一轮回答。
新工具从下一条消息起自动出现在工具集中，永久可用；uninstall_manager_tool 可完整卸载
（清单 + 源码 + import 缓存），同名工具可重新创建。

**安全边界**：

- 工具代码禁止导入交易/账户/命令执行模块（AST 检查在写盘前拦截：src/trading、src/portfolio、
  src/investment、src.research.sandbox、subprocess），管理 Agent 的无实盘权限边界不因自建工具而放宽；
- 试调用是强制步骤：没有验证用例的工具不允许注册；只有 fulfill 参数时一次执行兼任试调用，
  带副作用的工具每次创建最多执行一次；
- 创建与卸载共用按工具名线程锁 + 操作系统级文件锁（msvcrt.locking / fcntl.flock），
  内核原子、进程退出自动释放，无陈旧锁竞争窗口；
- 运行时安装的工具不入库（gitignore 只保留包 __init__.py）。

**已知边界**：模型是否“自主决定”创建工具依赖其遵循指令的能力，不能由代码硬保证。真实 API 评估：

```bash
python scripts/eval-manager-tooling.py --request "帮我对比贵州茅台和宁德时代过去30个交易日的价格走势相关性"
```

脚本注入 nonce 校验码，要求其同时出现在工具返回与最终答案（证明同轮闭环），pass/fail 以退出码表示，
并自动卸载评估期间创建的工具（--keep 保留）。实测（DeepSeek）：模型自主判断缺能力、一次调用完成创建、
用真实历史数据计算相关性并同轮回答，PASS；对现有工具已能覆盖的需求则正确选择不创建。


## 投资授权书与双反思

对话页提供保守、中立、激进三档策略目标。它不是单纯提示词：每档同时包含目标提示、最低置信度、
总仓位、单股仓位、单笔金额、现金储备、换手、订单数量与最大回撤等硬边界。选择结果版本化保存在
`runtime/investment/mandate.json`，并在每轮开始形成不可变快照写入审计和报告。

投资 Agent 每轮生成即时流程反思，并预留 T+1/T+5/T+20 结果评价；对话管理 Agent 每次任务后记录
工具路径、错误与是否验证外部状态。两套记忆隔离保存在 `runtime/memory/`，配置 MongoDB 时同步建立
可查询副本。反思只能提出待验证的策略改进，不能自行改变用户选择的风险档位。

## 许可证

MIT
