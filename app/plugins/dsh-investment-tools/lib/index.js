/**
 * Investment Auto 2.0 engine bridge tools.
 *
 * Registers the investment tool surface on `ctx.tools`:
 *   - read-only facts: status, portfolio, market data, screening, optimizer,
 *     reports, macro, mandate
 *   - engine actions (paper-trading boundary): run_cycle, set_strategy,
 *     control (pause/resume/kill/reset_kill), run_screening, reset_account
 *
 * All execution goes through the engine loopback HTTP API; this plugin owns
 * schemas, argument validation and result shaping only. Write actions are
 * paper-trading operations on the local engine and stay subject to its hard
 * risk controls; the model can never bypass them here.
 *
 * Row config: { engineUrl, token } — env overrides INVESTMENT_ENGINE_URL /
 * IA_ACCESS_TOKEN.
 */
import { defineTool } from "@deepseek-ai/dsh-tools";
import { EngineClient } from "./engine-client.js";

export const name = "@investment-auto/dsh-investment-tools";
export const inject = ["tools"];

/** Permissive object result schema for free-form engine payloads. */
const FREE_OBJECT = {
  type: "object",
  additionalProperties: true,
};

const jsonRender = (value) => {
  const text = JSON.stringify(value, null, 2);
  return text.length > 32000 ? text.slice(0, 32000) + "\n…(截断)" : text;
};

/**
 * Register one engine-backed tool.
 * @param {import('@deepseek-ai/cordis').Context} ctx
 * @param {string} toolName
 * @param {string} description
 * @param {object} parameters schemastery parameter schema
 * @param {(args: any) => Promise<any>} executor
 * @param {object} options { timeoutMs }
 */
function registerTool(ctx, toolName, description, parameters, executor, options = {}) {
  ctx.tools.register(
    defineTool({
      name: toolName,
      description,
      parameters,
      output: { schema: FREE_OBJECT, render: jsonRender },
      timeoutMs: options.timeoutMs ?? 60000,
      execute: executor,
    }),
  );
}

/** @param {import('@deepseek-ai/cordis').Context} ctx */
export function apply(ctx, config) {
  const engineUrl =
    process.env.INVESTMENT_ENGINE_URL ??
    (typeof config?.engineUrl === "string" ? config.engineUrl : "http://127.0.0.1:8790");
  const token = process.env.IA_ACCESS_TOKEN ?? (typeof config?.token === "string" ? config.token : "");
  const client = new EngineClient(engineUrl, token);

  registerTool(
    ctx,
    "investment_status",
    "投资引擎总状态：手动/自动模式、暂停/紧急停止、当前策略授权书、各市场交易时段、下一轮次与最近报告。",
    {},
    async () => {
      const payload = await client.get("/api/status");
      return { engine_url: engineUrl, ...payload };
    },
    { timeoutMs: 20000 },
  );

  registerTool(
    ctx,
    "investment_portfolio",
    "指定市场（cn/hk/us/etf）模拟账户的现金、持仓与成交历史。",
    {
      market: {
        type: "string",
        required: true,
        description: "市场代码：cn、hk、us 或 etf",
        default: "cn",
      },
    },
    async ({ market }) => client.get(`/api/portfolio/${market.toLowerCase()}`),
    { timeoutMs: 20000 },
  );

  registerTool(
    ctx,
    "investment_market_snapshot",
    "单只证券的实时行情快照：现价、成交额、技术指标（均线/动量/MACD/RSI/波动率）与近期历史。",
    {
      symbol: { type: "string", required: true, description: "证券代码，如 600519 / 00700 / AAPL / 510300" },
    },
    async ({ symbol }) => client.get(`/api/market/snapshot?symbol=${encodeURIComponent(symbol)}`),
    { timeoutMs: 60000 },
  );

  registerTool(
    ctx,
    "investment_market_history",
    "单只证券的日线历史（前复权），用于趋势与回测判断。",
    {
      symbol: { type: "string", required: true, description: "证券代码" },
      lookback: { type: "number", description: "K线根数，默认 60，最大 1023", default: 60 },
    },
    async ({ symbol, lookback = 60 }) =>
      client.get(`/api/market/history?symbol=${encodeURIComponent(symbol)}&lookback=${Math.trunc(lookback)}`),
    { timeoutMs: 60000 },
  );

  registerTool(
    ctx,
    "investment_security_search",
    "按名称/代码搜索证券，返回候选代码列表。",
    {
      keyword: { type: "string", required: true, description: "名称或代码关键字" },
    },
    async ({ keyword }) => client.get(`/api/market/search?q=${encodeURIComponent(keyword)}`),
    { timeoutMs: 30000 },
  );

  registerTool(
    ctx,
    "investment_screening",
    "查看指定市场最近一次全市场选股结果（缓存）。刷新选股请用 investment_run_screening。",
    {
      market: { type: "string", required: true, description: "市场代码：cn、hk、us 或 etf", default: "cn" },
    },
    async ({ market }) => client.get(`/api/screening/${market.toLowerCase()}`),
    { timeoutMs: 30000 },
  );

  registerTool(
    ctx,
    "investment_optimizer_latest",
    "查看指定市场最近一次组合优化结果（马科维茨/Black-Litterman/风险平价/压力测试）。",
    {
      market: { type: "string", required: true, description: "市场代码：cn、hk、us 或 etf", default: "cn" },
    },
    async ({ market }) => client.get(`/api/optimizer/${market.toLowerCase()}`),
    { timeoutMs: 20000 },
  );

  registerTool(
    ctx,
    "investment_reports",
    "列出引擎生成的轮次报告文件（按时间倒序）。",
    {
      market: { type: "string", description: "可选：只列某市场（cn/hk/us/etf）的报告" },
      limit: { type: "number", description: "最多返回条数，默认 10", default: 10 },
    },
    async ({ market, limit = 10 }) => {
      const suffix = market ? `&market=${encodeURIComponent(market.toLowerCase())}` : "";
      return client.get(`/api/reports?limit=${Math.trunc(limit)}${suffix}`);
    },
    { timeoutMs: 20000 },
  );

  registerTool(
    ctx,
    "investment_report_latest",
    "读取某市场最近一份轮次报告的元信息与内容。",
    {
      market: { type: "string", required: true, description: "市场代码：cn、hk、us 或 etf", default: "cn" },
    },
    async ({ market }) => client.get(`/api/reports/latest?market=${encodeURIComponent(market.toLowerCase())}`),
    { timeoutMs: 20000 },
  );

  registerTool(
    ctx,
    "investment_macro_latest",
    "读取最近的宏观日报（财经日历、政策、全球市场摘要）。",
    {},
    async () => client.get("/api/macro/latest"),
    { timeoutMs: 20000 },
  );

  registerTool(
    ctx,
    "investment_mandate",
    "读取当前投资授权书：策略档位（保守/中立/激进）与硬风险边界。",
    {},
    async () => client.get("/api/mandate"),
    { timeoutMs: 20000 },
  );

  registerTool(
    ctx,
    "investment_set_strategy",
    "切换投资策略档位（conservative/neutral/aggressive）。写操作：更新授权书并立即作用于后续轮次。",
    {
      profile: {
        type: "string",
        required: true,
        description: "策略档位：conservative、neutral 或 aggressive",
      },
    },
    async ({ profile }) => client.issue("set_strategy", { profile }, { requestedBy: "dsh-tools" }),
    { timeoutMs: 30000 },
  );

  registerTool(
    ctx,
    "investment_control",
    "投资引擎控制：pause（暂停）、resume（恢复）、kill（紧急停止）、reset_kill（解除紧急停止）。",
    {
      action: {
        type: "string",
        required: true,
        description: "pause | resume | kill | reset_kill",
      },
      reason: { type: "string", description: "操作原因（写入控制状态审计）" },
    },
    async ({ action, reason = "" }) => {
      const normalized = String(action).trim().toLowerCase();
      const command = { pause: "pause", resume: "resume", kill: "kill", reset_kill: "reset_kill" }[normalized];
      if (!command) throw new Error("action 必须是 pause、resume、kill 或 reset_kill");
      return client.issue(command, { reason }, { requestedBy: "dsh-tools" });
    },
    { timeoutMs: 30000 },
  );

  registerTool(
    ctx,
    "investment_run_cycle",
    "对指定市场执行一次完整投资轮次（选股→研究→决策→硬风控→模拟成交→报告）。模拟交易；受引擎硬风控约束。",
    {
      market: { type: "string", required: true, description: "市场代码：cn、hk、us 或 etf", default: "cn" },
      label: { type: "string", description: "轮次标签（用于报告文件名）" },
    },
    async ({ market, label = "dsh" }) =>
      client.issue("run_cycle", { market, label: String(label || "dsh").slice(0, 40) }, { requestedBy: "dsh-tools", timeoutMs: 600000 }),
    { timeoutMs: 600000 },
  );

  registerTool(
    ctx,
    "investment_run_screening",
    "刷新指定市场的全市场选股并返回结果（耗时数十秒）。",
    {
      market: { type: "string", required: true, description: "市场代码：cn、hk、us 或 etf", default: "cn" },
    },
    async ({ market }) => client.issue("run_screening", { market }, { requestedBy: "dsh-tools", timeoutMs: 300000 }),
    { timeoutMs: 300000 },
  );

  registerTool(
    ctx,
    "investment_submit_decisions",
    "把本轮研究得出的买卖决策提交给引擎执行：引擎自行获取行情、按当前授权书硬边界重算仓位、执行硬风控与纸面撮合，返回成交与拒绝清单。只允许纸面模式；决策前先用 plan 模式或 ask_user 取得用户确认。",
    {
      market: { type: "string", required: true, description: "市场代码：cn、hk、us 或 etf", default: "cn" },
      decisions: {
        type: "array",
        required: true,
        description: "决策清单（每个决策带 symbol/action/target_weight/confidence/reason）",
        items: {
          type: "object",
          additionalProperties: true,
          properties: {
            symbol: { type: "string", required: true, description: "证券代码" },
            action: { type: "string", required: true, description: "BUY / SELL / HOLD" },
            target_weight: { type: "number", description: "目标仓位权重 0-1（引擎会重新计算并硬封顶）" },
            confidence: { type: "number", description: "置信度 0-1（低于授权书阈值会被拒绝）" },
            reason: { type: "string", description: "决策依据（写入审计与报告）" },
          },
        },
      },
      label: { type: "string", description: "轮次标签（用于报告文件名，默认 dsh-manual）" },
      note: { type: "string", description: "本轮分析摘要，写入报告正文" },
    },
    async ({ market, decisions, label, note = "" }) =>
      client.issue(
        "submit_decisions",
        { market, decisions, label: String(label || "dsh-manual").slice(0, 40), note },
        { requestedBy: "dsh-tools", timeoutMs: 300000 },
      ),
    { timeoutMs: 300000 },
  );

  registerTool(
    ctx,
    "investment_reset_account",
    "重置指定市场的模拟账户（备份后清空为初始资金）。写操作，且只允许纸面模式。",
    {
      market: { type: "string", required: true, description: "市场代码：cn、hk、us 或 etf", default: "cn" },
      reason: { type: "string", description: "重置原因（写入备份记录）" },
    },
    async ({ market, reason = "" }) => client.issue("reset_paper_account", { market, reason }, { requestedBy: "dsh-tools" }),
    { timeoutMs: 30000 },
  );
}
