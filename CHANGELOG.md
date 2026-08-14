# 更新日志

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
- **受限 shell**：可执行文件白名单 + argv 直执行（免疫 shell 元字符注入）+ 项目目录
  路径边界 + 最小化环境（不含生产密钥）。交易执行路径与管理对话 Agent 永远没有 shell；
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
