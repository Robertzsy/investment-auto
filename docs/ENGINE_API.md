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
