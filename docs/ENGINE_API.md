# 引擎 API 与 DSH 桥契约（Investment Auto 2.0）

引擎（`engine/`）是 2.0 的执行面：行情、选股、组合优化、硬风控、纸面经纪、
账户、调度、报告与通知。决策面是 DSH 应用（`app/`）。两面通过两条通道连接：

1. **HTTP 命令 API**（`engine.api.server`，默认 `127.0.0.1:8790`）：
   DSH 侧 MCP 桥（P1）与桌面壳调用。
2. **周期执行器（cycle runner）**（P3）：引擎调度器在轮次时刻调用 DSH
   桥注册的执行器，由后者拉起 `investment`（headless）profile 完成一轮
   AI 决策，结果交回引擎写报告、反思、通知；硬风控与纸面撮合始终在引擎内。

## HTTP 命令 API

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/api/health` | 服务与版本信息 |
| GET | `/api/status` | `runtime_status()`：控制状态、授权书、各市场会话与最新报告 |
| GET | `/api/analysis/latest` | 最近分析轮次、阶段、Agent/证据计数与检查点 |
| GET | `/api/analysis/runs` | 分析轮次列表（支持 `market` / `limit`） |
| GET | `/api/analysis/run` | 按 `cycle_id` 读取一轮完整状态 |
| POST | `/api/analysis/runs/start` | 创建或恢复同一 cycle id 的工作流 |
| POST | `/api/analysis/runs/update` | 更新阶段、Agent 事件或原子检查点 |
| POST | `/api/analysis/runs/complete` | 保存最终决策与引擎执行真值 |
| POST | `/api/analysis/runs/fail` | 保存失败原因，保留已有检查点供续跑 |
| POST | `/api/analysis/rounds/start` | **Web 启动固定分析轮次（异步、幂等）**：`{cycle_id, market, symbols?, symbols_source?, label}`；同一 cycle_id 最多一个轮次（运行中返回现状、终态返回结果、孤儿轮次从检查点续跑）；引擎强制 `submit=false`（分析只生成方案，成交必须等用户随后批准的 `submit_decisions`） |
| GET | `/api/analysis/rounds/active` | 当前活跃的异步轮次列表 |
| POST | `/api/commands/issue` | 派发投资命令（见 `engine/investment/contracts.py`） |

请求体：`{"command": "<InvestmentCommand>", "payload": {...}, "requested_by": "..."}`

鉴权：仅绑定回环；设置 `IA_ACCESS_TOKEN` 后要求请求头 `X-IA-Token` 匹配。

## 周期执行器契约（`engine.scheduler.set_cycle_runner`）

```python
runner(market: str, cycle_type: str, context: dict) -> dict
# cycle_type: "intraday" | "close"
# context: {
#   label, time_str, scheduled_at(datetime), catch_up(bool), now(datetime),
#   macro_excerpt(str), optimizer_hint(dict|None, close 轮次), account(dict),
#   progress_callback(callable|None),
# }
# 返回（引擎消费的键）：
# {
#   status: "generated" | "skipped" | "error",
#   reason/error: str,                       # skipped/error 时
#   report_text: str|None,                   # AI 摘要（可选）
#   decisions: [ {symbol, action, confidence, reason}, ... ],
#   execution: {fills: [...], rejected: [...]},
#   screening: {...}, mandate: {...}, audit_file: str|None,
#   warnings: [...], degraded_mode: bool|None, control: {...}|None,
# }
```

未注册执行器时轮次返回 `status: "skipped"`，并写入说明性报告，不中断调度。
fail-safe 语义不变：订单只有在引擎的硬风控 + 纸面经纪路径中才会被撮合；
执行器返回的 `execution` 只是对引擎执行结果的回显。

自主轮次为 DSH 原生固定工作流：四类基础分析 → 多空辩论/研究经理/个股交易员 →
组合草案 → 三方风险辩论/风险经理 → 最终组合经理 → 引擎硬风控与纸面撮合。
每个阶段完成后才写检查点；Python 调度器和 API 不直接调用 LLM，也不生成投资决策。

### 分析流程的两块路由（2.1）

- **选股**：独立块，由 `investment_screening` / `investment_run_screening`
  输出标准化候选列表；用户点名证券时完全跳过。
- **股票分析**：唯一入口 `investment_analysis_workflow`。手动会话中是异步
  启动器（`POST /api/analysis/rounds/start`，立即返回 cycle_id，用
  `investment_analysis_status` 轮询）；headless 自主轮次（引擎 worker 与
  调度器，`IA_AUTONOMOUS_ROUND=1`）中同步执行同一套阶段脚本并把检查点
  写回引擎。`symbols` + `symbols_source`（user/screening）决定目标范围，
  用户指定的标的绝不混入选股池；持仓只作为上下文。**手动入口（工具与端点
  双重）拒绝空 symbols**，自主轮次才允许内部选股。
- **cycle_id 身份**：`会话ID + 最新直接用户消息ID` 派生（重试复用、新请求
  新 ID）；headless 用引擎注入的 `INVESTMENT_CYCLE_ID`；无会话上下文时用
  随机 ID + 短 TTL 内容指纹兜底（仅传输故障重试）。
- **轮次状态机**：`running → ready_for_execution → completed / failed`。
  `execution_ready` 事件持久化最终决策与引擎计算的决策指纹；自主轮次在
  `ready_for_execution + submit=true` 时提交，手动轮次等待用户批准后以
  `investment_submit_decisions`（同 cycle_id）提交；执行结束后引擎把绑定
  轮次置为 completed。failed 允许同键安全重试；serve 进程在 API 监听后
  扫描并续跑被重启孤儿的 running 轮次。
- **提交幂等（账户文件为唯一事实）**：`submit_decisions` 强制
  `idempotency_key`（分析轮次用其 cycle_id）。引擎先对归一化决策计算
  内容指纹并在认领中记录（同键不同内容一律拒绝）；`execute_orders` 把
  成交与**执行回执**在账户锁内同一次原子替换写入 `portfolio.json`——
  崩溃于「已成交、未写簿记」之间时，重试从账户回执重放，绝不再成交。
  审计文件仅为外部记录。四类失败处理：参数/内容错误→终态 rejected 并
  重放；确定性硬风控拒绝→保存并重放；进入撮合前的暂时性故障→释放认领；
  已进入撮合→绝不删除，凭回执恢复。
- **运行隔离**：headless 轮次使用独立内部 DSH home（`<home>/agent-home`，
  引擎同步播种 profiles/presets/skills/plugins + settings storages +
  开发模式凭据镜像），其会话不会出现在用户会话栏。
- 旧 `investment_run_cycle` 工具已从模型可见面移除（引擎内部命令保留给
  调度器使用），不再与新流程竞争。
