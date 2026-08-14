# investment-auto 架构升级方案（DSH 模式移植）

> 状态：**全部 7 个阶段已完成并发布 0.5.0** · 2026-08-14
> 原则：**分阶段、可回退、不破坏正在运行的进程**。每个阶段独立配置开关 + 独立测试。
>
> 实施结果：P0 基线 → P1 引用自动修复 → P2 工具中介 → P3 摘要跨界+证据库 →
> P4 可续工作单元 → P5 研究循环+受限 shell+能力注册 → P6 文档与发布。
> 最终全量测试 184+ 通过；回退冒烟验证未配置 architecture 段时行为与旧版一致。

## 0. 目标

将 DSH 的六项架构模式移植到本项目，解决已量化的痛点：

| 模式 | 要解决的问题（已实测） |
|---|---|
| 1. 工具中介交互 | 112 条 Agent 失败中 **94 条（84%）** 是引用校验失败，每条都烧一次重试（双倍 LLM 调用） |
| 2. 摘要跨界 | 单轮 audit 从 ~10KB 膨胀到 **600KB+**；证据目录拼进 prompt 上限 **42000 字符** |
| 3. 可续持久化工作单元 | 单轮超时（1200s）后整轮作废，只能整轮重跑；进程重启丢全部中间结果 |
| 4.（同 3） | — |
| 5. 能力/技能注册 | 已有 CapabilityRegistry，需扩展研究工具集并支持版本化 |
| 6. Ralph 离线研发循环 | 无离线回测/策略实验/自动修 Bug 能力；项目不能自我验证与进步 |
| 7. Shell（受限） | 离线研究需要执行环境；交易路径绝不放开 |

安全边界铁律（不变）：交易执行路径与管理对话 Agent **永不给 shell**；AI 不能绕过硬风控；研究循环永远不能直接修改生产配置/账户/下单。

---

## 1. 工具中介交互（Phase 1 + 2）

### 现状（已核实）
- `_call_role`（src/trading/agent_workflow.py）单次生成 JSON → `validate_citations` 事后校验 → 失败整次重试（默认 retries=1，最多 2 次）
- 失败原因分布：94× 缺直接上游引用、9× 引用不存在 ID（模型编造格式，如漏 `:R1` 后缀）、4× 只引了一个
- `mandatory_upstream_ids` 埋在超长 user prompt 中部，模型极易漏

### Phase 1：引用自动修复（低风险快赢）
**文件**：`src/trading/agent_workflow.py`
- 新增 `repair_citations(payload, allowed_ids, required_upstream_prefixes, require_all_upstreams) -> (payload, repairs)`
  - 情形 A（ID 格式错误）：对非法 ID 做规范化（大小写、去空白、补 `:R1` 后缀），匹配到 allowed 则替换，记 `repair`
  - 情形 B（缺必须上游）：只允许自动补 **系统提供的上游 Agent ID**（`allowed` 中 `AGENT:` 前缀且属于 required_upstream_prefixes 的），补进顶层 citations，记 `auto_attached`
  - 情形 C（完全无引用 / 引用全非法且无法修复）：**不修复**，保持现有重试（真失败）
- `_call_role` 校验失败后先尝试 repair，成功则跳过重试；payload 增加 `citation_repairs` 字段进 audit
- 配置：`architecture.citation_auto_repair: true`
- 测试：修复路径（缺前缀、后缀不匹配、无引用不修复、审计标记）

### Phase 2：工具中介节点（核心改造）
**新增**：`src/trading/toolchain.py`
- 用 **OpenAI SDK 原生 function calling**（复用现有 `src/llm/adapter.py`，deepseek/glm/kimi 均兼容），不引入 pydantic-ai Agent 每节点循环（成本考量）
- 两个工具：
  - `list_evidence_ids(prefix)`：返回合法证据 ID 子集（模型先查再引，消灭"编造 ID"）
  - `submit_analysis(payload_json)`：内部执行 `_parse_json_object` + `validate_citations` + Phase 1 的 `repair_citations`；失败返回**具体错误 + 缺失 ID 清单**作为 tool result，模型修正后重新提交
- `_call_role` 增加 `mode: "tool" | "json"` 分支；工具循环上限 `tool_retries`（默认 2）；模型不调工具直接输出文本时回退 json 解析
- 启用范围：`architecture.tool_mediated_roles: [research_manager, trader, risk_manager, portfolio_manager]`（失败率最高的关键节点）；base 分析师保持 json 模式（失败率低，单次成本最优）
- 需要验证：deepseek 的 `thinking.disabled` extra_body 在 function calling 模式下仍生效（adapter 已有 extra_body 支持，风险低）
- 测试：fake LLM 模拟"错误提交 → 工具拒绝 → 修正提交成功"循环

**收益**：引用修正成本从"整次重生成"降为"一次工具往返"；模型可见具体缺失 ID。

---

## 2. 摘要跨界（Phase 3）

### 现状（已核实）
- audit 全量塞入 `staged_workflow`（base_reports 全文、debate 全文、evidence 全量）→ 600KB+
- `_evidence_text` 上限 42000 字符；上游报告 `_json_text(value, 2600)` 全文传链

### 设计
**新增**：`src/trading/evidence_store.py`
- `save_cycle_evidence(cycle_id, market, evidence)` → `runtime/trading/evidence/<cycle_id>/<market>.json`（原子写）
- `load_cycle_evidence(cycle_id, market)`、`evidence_summary(evidence, role)`
- `_compact_report(payload)`：只留 `summary / stance / confidence / decisions / citations / findings(≤3条, 每条claim≤80字)`

**改动**：
- `run_analysis_workflow`：完整 evidence 落盘证据库，返回值只含摘要；链上传递用 `_compact_report`
- `_evidence_text` 上限 42000 → `architecture.evidence_text_max_chars: 16000`
- `controller.py` audit 的 `agent_workflow` 字段改为 `{summary, evidence_ref, timings_seconds, status}`
- 管理工具 `get_cycle_evidence`（src/ui/agent_runtime.py）改读证据库（可回溯能力保留）
- 测试：audit 体积断言（<100KB）、evidence 可回溯、旧 audit 兼容读取

**收益**：audit 体积降一个数量级；每节点 prompt 变小 → 直接降 token 成本与延迟。

---

## 3. 可续持久化工作单元（Phase 4）

### 现状（已核实）
- cycle 一次性执行（controller.run_autonomous_cycle），无中间落盘；超时/崩溃整轮丢失
- 已有 `atomic_claim` 锁 + audit 落盘可复用

### 设计
**新增**：`src/trading/checkpoints.py`
- `CycleCheckpoint`：`cycle_id, market, status, stages: Dict[stage→payload], updated_at, execution_completed: bool`
- `save_stage / load_stage / list_incomplete(stale_minutes) / mark_completed`（原子写 `runtime/trading/checkpoints/<cycle_id>.json`）
- `_run_symbol_research`：每阶段完成即落盘；启动时 `load_stage` 存在则跳过（幂等续跑），支持 `resume_from`
- `run_analysis_workflow`：portfolio/risk 层同样分阶段落盘

**controller 集成**：
- cycle 开始用 `generated_at` 作 cycle_id 初始化；完成→mark_completed；异常→保留
- 启动 / catchup 时检查 `list_incomplete`：未过期（默认 90 分钟）且市场行情新鲜 → 续跑研究；否则丢弃重来（审计记录 resume/rejected）
- **安全铁律**：恢复只恢复研究阶段；下单阶段永不恢复重放——`execution_completed` 标志保证 crash 后不重复下单（配合 broker 的 portfolio.lock）
- 风险/build_orders 始终用**当前** prices 重算，绝不复用旧价格决策
- 测试：中途抛异常→恢复→断言只重跑未完成部分；execution 幂等

**收益**：20 分钟超时不再全丢；进程重启（main.py 已有 restart 机制）后未完成轮次可续。

---

## 4. 能力/技能注册扩展（Phase 5 内）

### 现状（已核实）
- `CapabilityRegistry`（src/manager/capabilities.py）：skill（SKILL.md）+ tool（限 `src` 包内函数白名单）已可用，install 后下次对话生效

### 扩展
- 新增 `src/research/tools.py` 导出研究工具，注册进 registry：
  - `run_backtest`、`run_strategy_experiment`、`list_research_results`、`apply_experiment_to_config`（走 change_manager 版本化+全量测试+回滚）
- skill 文件增加 `version` 字段，更新保留历史版本（`runtime/manager/capabilities/skills/<name>/v<N>/`）
- 测试：注册→catalog→调用链

---

## 5. Ralph 离线研发循环（Phase 5 主体）

### 设计
**新增包**：`src/research/`
- `loop.py`：Ralph 核心
  - `run_research_loop(objective, task, workspace, max_rounds) -> ResearchOutcome`
  - 每轮 fresh agent（**无对话上下文**）：prompt = 不可变 objective + 工作区索引（文件清单+摘要）+ 前几轮 report.json 摘要
  - 每轮输出结构化 `report.json`：`{status: completed|blocked|needs_more_rounds, findings, changes_made, evidence, next_steps, blocker_reason}`
  - 终止：completed / blocked / max_rounds（默认 6）
- `workspace.py`：`runtime/research/workspace/` 共享记忆（round-<N>/ 子目录 + index.json）
- `tasks.py` 三种任务：
  - **backtest**：`src/research/backtest.py` 回测引擎。数据源用现有 `fetcher.history`（腾讯 K 线日线、qfq、最多 1023 根，已核实）。两种模式：①决策回放（用历史 audit decisions 回放 broker/risk）②规则模式（screening 因子分数→目标仓位，不调 LLM）；`--live-llm` 可选全链回测（昂贵，默认关）
  - **strategy_experiment**：参数空间（factor weights / risk 参数 / mandate 边界）→ 批量回测 → 对比表写入 workspace；**不改生产配置**，用户确认后才经 apply_experiment_to_config 应用
  - **bugfix**：复现（pytest 或复现脚本）→ 修改（change_manager 机制：SHA-256 读取、版本化备份、全量测试、失败自动回滚）→ 报告
- `sandbox.py`：受限命令执行（见第 6 节）
- CLI：`python -m src.main research --task backtest --market cn --objective "..." --max-rounds 6`
- 测试：loop 终止条件（fake LLM 完成/blocker）、sandbox 逃逸防护、backtest 基本正确性（构造价格序列断言账户演化）

**收益**：项目获得"离线自我验证"能力——策略改进、回测对比、Bug 修复都可以让 fresh agent 在隔离工作区里迭代，结论经用户确认后才进入生产路径。

---

## 6. Shell 受限执行（Phase 5 内）

**决定**：只给**离线研究循环**受限 shell；交易执行路径与管理对话 Agent 保持无 shell（现有 test_ui_security 边界不变）。

`src/research/sandbox.py`：
- `run_command(command: str, *, workdir, timeout=60) -> {stdout, stderr, exit_code}`
- **项目隔离**（用户要求）：
  - workdir 强制绑定研究 workspace（`Path.resolve()` 前缀校验，杜绝 `..` 逃逸/绝对路径访问项目外）
  - 用 subprocess **参数列表**执行（不经 shell）→ 天然免疫 `;` `&&` `|` `>` 注入
  - 命令白名单前缀：`python`（-m pytest / scripts/*.py / -m src.research.*）、`git`（diff/status/log 只读）、`node`（scripts/*.js）
  - 子进程 env 只含行情/LLM 所需最小集；**不注入** NOTIFY_WEBHOOK_URL、MONGODB_URI、生产账户路径
  - 输出截断（模拟 DSH pwsh 尾截断 2000 字符）+ 超时强杀
- 配置：`architecture.research.shell: restricted | none`（默认 restricted；设 none 即完全无 shell，研究 agent 仅靠工具工作）
- 测试：`..` 逃逸、绝对路径、白名单外命令、超时强杀

---

## 7. 配置总览（新增 `architecture:` 段）

```yaml
architecture:
  citation_auto_repair: true      # Phase 1
  tool_mediated: true             # Phase 2
  tool_mediated_roles: [research_manager, trader, risk_manager, portfolio_manager]
  tool_retries: 2
  evidence_store: true            # Phase 3
  evidence_text_max_chars: 16000
  audit_summary_only: true
  checkpoint_cycles: true         # Phase 4
  resume_stale_minutes: 90
  research:                       # Phase 5
    enabled: true
    max_rounds: 6
    workspace: runtime/research
    shell: restricted             # restricted | none
    shell_timeout_seconds: 60
```

---

## 8. 阶段计划与验收

| 阶段 | 内容 | 涉及文件 | 预计 |
|---|---|---|---|
| P0 | 修 2 个失败测试；版本单点化（0.4.0）；git checkpoint 提交未入库工作 | tests/*, pyproject.toml, src/main.py | 0.5 天 |
| P1 | 引用自动修复 | agent_workflow.py + tests | 1 天 |
| P2 | 工具中介节点 | **新增 toolchain.py** + agent_workflow.py | 2-3 天 |
| P3 | 摘要跨界 + 证据库 | **新增 evidence_store.py** + agent_workflow.py + controller.py + agent_runtime.py | 1-2 天 |
| P4 | 可续工作单元 | **新增 checkpoints.py** + agent_workflow.py + controller.py + broker.py(幂等) | 2-3 天 |
| P5 | 研究循环 + 受限 shell + 能力扩展 | **新增 src/research/ 包** + main.py + capabilities.py + stock-fetcher.js(若需) | 3-5 天 |
| P6 | 文档、配置示例、smoke 更新、全量测试、0.5.0 发布 | README.md, config.yaml, .env.example, smoke-agent-workflow.py | 1 天 |

依赖：P1→P2（repair 逻辑被工具复用）；P3/P4 可并行；P5 依赖 P3 的 evidence 目录结构（回测可读历史 audit）；P2 与 P3 有轻微耦合（toolchain 输出的 payload 也要压缩），建议顺序执行。

**阶段验收标准**：每阶段结束时全量 pytest 通过（含新增用例）、agent_failures 计数对比、audit 体积对比、feature flag 关闭时行为与旧版一致（回退测试）。

## 9. 风险与回退

| 风险 | 缓解 |
|---|---|
| function calling 在 deepseek/glm/kimi 的兼容差异 | toolchain 失败自动回退 json 模式（现有路径），开关可整体关闭 |
| 自动补引用弱化防幻觉 | 只补系统提供的上游 Agent ID（非原始事实），audit 记录 citation_repairs 可审计 |
| checkpoint 恢复用旧研究配新行情 | 恢复只续研究；风险/下单永远用当前 prices 重算；execution_completed 防重复下单 |
| 回测数据质量（qfq/停牌/新股） | 回测引擎标注研究用途，数据源标记来源与复权方式 |
| 改造期间旧进程 | 新代码只在重启后生效；新增目录（evidence/checkpoints/research）不影响旧逻辑 |
| 全部开关 | `architecture:` 段全关 = 旧行为，做回退冒烟测试 |

## 10. 一句话总结

分 7 个阶段落地：先修基线（P0）→ 用自动修复+工具中介消灭 84% 的引用失败（P1/P2）→ 摘要跨界与证据库压缩 audit 与 token（P3）→ checkpoint 让轮次可续（P4）→ Ralph 研究循环 + 受限 shell 让项目离线自我验证与进步（P5）→ 文档发布（P6）。每阶段独立开关、独立测试、可回退。
