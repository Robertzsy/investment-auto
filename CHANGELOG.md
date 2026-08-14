# 更新日志

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
- 工具模块由文件路径直接加载（不受包路径缓存影响）；运行时安装的工具不入库。

### 其它

- 管理 Agent 上限默认放宽为 32 次模型请求 / 64 次工具调用，并可在设置页调整；
- 双模型（快速/深度）设置移至设置页顶部「投资者控制中心」；
- 新增 reset_paper_account 工具：原子化重置指定模拟市场账户（自动加锁、备份、
  仅模拟盘可用）；
- 收盘补跑默认不交易（trade_on_catch_up: false）；修复 Windows checkpoint
  瞬时文件占用。

全量测试 **198 项通过**。

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
