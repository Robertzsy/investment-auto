/** Fixed, DSH-native multi-role investment analysis workflow. */

import { assertObjectJsonSchema } from "@deepseek-ai/dsh-tools";

const MARKETS = new Set(["cn", "hk", "us", "etf"]);
const STAGES = ["base_research", "research_debate", "portfolio_draft", "risk_review", "final_decision"];

const evidenceSchema = {
  type: "object",
  additionalProperties: false,
  properties: {
    id: { type: "string" },
    title: { type: "string" },
    source: { type: "string" },
    claim: { type: "string" },
  },
  required: ["id", "title", "source", "claim"],
};

const decisionSchema = {
  type: "object",
  additionalProperties: false,
  properties: {
    symbol: { type: "string" },
    action: { type: "string", enum: ["BUY", "SELL", "HOLD"] },
    target_weight: { type: "number" },
    confidence: { type: "number" },
    reason: { type: "string" },
    evidence_ids: { type: "array", items: { type: "string" } },
  },
  required: ["symbol", "action", "target_weight", "confidence", "reason", "evidence_ids"],
};

/** Every raw JSON Schema passed to workflow agent(). Keeping the schemas as
 * ordinary exported values lets startup/release checks run DSH's public
 * assertObjectJsonSchema() validator before a costly analysis round starts. */
export const WORKFLOW_AGENT_SCHEMAS = Object.freeze({
  baseResearch: {
    type: "object",
    additionalProperties: false,
    properties: {
      symbol: { type: "string" },
      role: { type: "string" },
      summary: { type: "string" },
      stance: { type: "string", enum: ["bullish", "bearish", "neutral", "unknown"] },
      confidence: { type: "number" },
      evidence: { type: "array", items: evidenceSchema },
      risks: { type: "array", items: { type: "string" } },
      data_quality: { type: "string" },
    },
    required: ["symbol", "role", "summary", "stance", "confidence", "evidence", "risks", "data_quality"],
  },
  debate: {
    type: "object",
    additionalProperties: false,
    properties: {
      symbol: { type: "string" },
      side: { type: "string" },
      thesis: { type: "string" },
      confidence: { type: "number" },
      evidence_ids: { type: "array", items: { type: "string" } },
      risks: { type: "array", items: { type: "string" } },
    },
    required: ["symbol", "side", "thesis", "confidence", "evidence_ids", "risks"],
  },
  researchManager: {
    type: "object",
    additionalProperties: false,
    properties: {
      symbol: { type: "string" },
      verdict: { type: "string" },
      confidence: { type: "number" },
      evidence_ids: { type: "array", items: { type: "string" } },
      open_risks: { type: "array", items: { type: "string" } },
    },
    required: ["symbol", "verdict", "confidence", "evidence_ids", "open_risks"],
  },
  trader: decisionSchema,
  portfolio: {
    type: "object",
    additionalProperties: false,
    properties: {
      summary: { type: "string" },
      cash_reserve_rationale: { type: "string" },
      decisions: { type: "array", items: decisionSchema },
    },
    required: ["summary", "cash_reserve_rationale", "decisions"],
  },
  riskView: {
    type: "object",
    additionalProperties: false,
    properties: {
      role: { type: "string" },
      assessment: { type: "string" },
      approved: { type: "boolean" },
      adjustments: { type: "array", items: { type: "string" } },
      evidence_ids: { type: "array", items: { type: "string" } },
    },
    required: ["role", "assessment", "approved", "adjustments", "evidence_ids"],
  },
  riskManager: {
    type: "object",
    additionalProperties: false,
    properties: {
      summary: { type: "string" },
      decisions: { type: "array", items: decisionSchema },
    },
    required: ["summary", "decisions"],
  },
  finalDecision: {
    type: "object",
    additionalProperties: false,
    properties: {
      summary: { type: "string" },
      decisions: { type: "array", items: decisionSchema },
      risks: { type: "array", items: { type: "string" } },
    },
    required: ["summary", "decisions", "risks"],
  },
});

// Fail during plugin composition, before an analysis run or any model call,
// using the exact public validator consumed by the workflow worker.
for (const schema of Object.values(WORKFLOW_AGENT_SCHEMAS)) {
  assertObjectJsonSchema(schema);
}

const BASE_RESEARCH_SCRIPT = `
const schema = ${JSON.stringify(WORKFLOW_AGENT_SCHEMAS.baseResearch)};
phase("基础研究");
const rows = await pipeline(args.symbols, async (_previous, symbol) => {
  const analyses = await parallel(args.roles.map((role) => () => agent(
    "你是" + args.role_instructions[role] + "。只研究 " + symbol + "（" + args.market.toUpperCase() + "）。必须先调用 investment_market_snapshot 和 investment_market_history；新闻/情绪角色还应调用 web_search 查近期事件。不得猜测数据。输出证据时每条给出稳定 id、标题、来源 URL 或投资工具名称、以及该证据支持的事实。结合授权书与以下有限历史记忆，但当前数据优先：" + JSON.stringify(args.memory).slice(0,3000),
    {label:symbol + ":" + role,phase:"基础研究",schema}
  )));
  return {symbol, analyses};
});
return rows.filter(Boolean);
`;

const RESEARCH_DEBATE_SCRIPT = `
const debateSchema = ${JSON.stringify(WORKFLOW_AGENT_SCHEMAS.debate)};
const managerSchema = ${JSON.stringify(WORKFLOW_AGENT_SCHEMAS.researchManager)};
const traderSchema = ${JSON.stringify(WORKFLOW_AGENT_SCHEMAS.trader)};
phase("研究辩论与个股决策");
const rows = await pipeline(args.research, async (_previous, item) => {
  const source = JSON.stringify(item).slice(0,18000);
  const sides = [];
  for (let round = 1; round <= args.rounds; round += 1) {
    const prior = JSON.stringify(sides).slice(0,8000);
    const pair = await parallel([
      () => agent("你是多头研究员。第 " + round + " 轮针对 " + item.symbol + " 提出最强多头论证并回应既有观点；只能引用已有 evidence id。基础材料：" + source + " 既有辩论：" + prior,{label:item.symbol+":bull-r"+round,phase:"研究辩论与个股决策",schema:debateSchema}),
      () => agent("你是空头研究员。第 " + round + " 轮针对 " + item.symbol + " 提出最强空头论证并回应既有观点；只能引用已有 evidence id，指出数据缺口。基础材料：" + source + " 既有辩论：" + prior,{label:item.symbol+":bear-r"+round,phase:"研究辩论与个股决策",schema:debateSchema})
    ]);
    sides.push(...pair.filter(Boolean));
  }
  const manager = await agent("你是研究经理。裁决 " + item.symbol + " 的多空辩论，检查证据是否真的支持结论，不得新增事实。基础研究与辩论：" + JSON.stringify({item,sides}).slice(0,22000),{label:item.symbol+":research-manager",phase:"研究辩论与个股决策",schema:managerSchema});
  if (!manager) return null;
  const trader = await agent("你是个股交易员。根据研究经理裁决和当前授权书给出 BUY/SELL/HOLD 建议。target_weight 是组合目标权重 0-1；证据不足必须 HOLD。材料：" + JSON.stringify({manager,mandate:args.mandate,holding:args.holdings[item.symbol]||null}).slice(0,16000),{label:item.symbol+":trader",phase:"研究辩论与个股决策",schema:traderSchema});
  return trader ? {symbol:item.symbol,sides,manager,trader} : null;
});
return rows.filter(Boolean);
`;

const PORTFOLIO_SCRIPT = `
const schema = ${JSON.stringify(WORKFLOW_AGENT_SCHEMAS.portfolio)};
phase("组合草案");
const draft = await agent("你是组合构建经理。把逐只交易建议合并为相互一致的组合草案，遵守授权书的仓位、现金和换手边界；不能引用材料中不存在的 evidence id。现有账户、授权书、建议：" + JSON.stringify({portfolio:args.portfolio,mandate:args.mandate,recommendations:args.recommendations}).slice(0,30000),{label:"portfolio-draft",phase:"组合草案",schema});
return draft;
`;

const RISK_SCRIPT = `
const viewSchema = ${JSON.stringify(WORKFLOW_AGENT_SCHEMAS.riskView)};
const managerSchema = ${JSON.stringify(WORKFLOW_AGENT_SCHEMAS.riskManager)};
phase("风险辩论");
const material = JSON.stringify({draft:args.draft,portfolio:args.portfolio,mandate:args.mandate}).slice(0,30000);
const views = [];
for (let round = 1; round <= args.rounds; round += 1) {
  const prior = JSON.stringify(views).slice(0,8000);
  const trio = await parallel([
    () => agent("你是激进风险分析师。第 "+round+" 轮在不突破硬边界的前提下评估机会成本与仓位不足风险，并回应既有观点。材料："+material+" 既有观点："+prior,{label:"risk-aggressive-r"+round,phase:"风险辩论",schema:viewSchema}),
    () => agent("你是保守风险分析师。第 "+round+" 轮重点检查回撤、集中度、流动性、现金储备、数据缺口和极端情景，并回应既有观点。材料："+material+" 既有观点："+prior,{label:"risk-conservative-r"+round,phase:"风险辩论",schema:viewSchema}),
    () => agent("你是中立风险分析师。第 "+round+" 轮平衡收益风险，检查一致性和换手成本，并回应既有观点。材料："+material+" 既有观点："+prior,{label:"risk-neutral-r"+round,phase:"风险辩论",schema:viewSchema})
  ]);
  views.push(...trio.filter(Boolean));
}
const manager = await agent("你是风险经理。裁决三方风险意见并给出修订后的决策清单；引擎稍后仍会独立执行不可绕过的硬风控。不得新增事实或证据。材料：" + JSON.stringify({material,views}).slice(0,32000),{label:"risk-manager",phase:"风险辩论",schema:managerSchema});
return {views,manager};
`;

const FINAL_SCRIPT = `
const schema = ${JSON.stringify(WORKFLOW_AGENT_SCHEMAS.finalDecision)};
phase("最终决策");
const result = await agent("你是最终投资组合经理。根据研究链路、组合草案和风险经理裁决给出最终清单。不得越过授权书，不得新增事实或 evidence id；不确定就 HOLD。材料：" + JSON.stringify({risk:args.risk,recommendations:args.recommendations,mandate:args.mandate}).slice(0,34000),{label:"portfolio-manager",phase:"最终决策",schema});
return result;
`;

const ROLE_INSTRUCTIONS = {
  technical_analyst: "技术分析师，负责价格趋势、动量、波动率、成交与关键价位",
  fundamentals_analyst: "基本面分析师，负责估值、财务质量、业务驱动与可验证的基本面事实",
  news_analyst: "新闻分析师，负责近期公司/行业/政策事件及时间敏感性",
  sentiment_analyst: "市场情绪分析师，负责资金、交易拥挤度、舆情与预期差",
};

function compact(value, depth = 0) {
  if (typeof value === "string") return value.length > 1600 ? value.slice(0, 1600) + "…" : value;
  if (value === null || typeof value !== "object") return value;
  if (depth > 7) return "[truncated]";
  if (Array.isArray(value)) return value.slice(0, 160).map((item) => compact(item, depth + 1));
  return Object.fromEntries(Object.entries(value).slice(0, 80).map(([key, item]) => [key, compact(item, depth + 1)]));
}

function uniqueSymbols(items) {
  const result = [];
  for (const item of items) {
    const symbol = String(item ?? "").trim();
    if (symbol && !result.includes(symbol)) result.push(symbol);
  }
  return result;
}

/** Stable idempotency id for one logical user request (same content -> same id). */
function stableCycleId(market, label, symbols) {
  const day = new Date().toISOString().slice(0, 10).replaceAll("-", "");
  let hash = 5381;
  for (const char of `${market}:${[...symbols].sort().join(",")}`) {
    hash = ((hash * 33) ^ char.codePointAt(0)) >>> 0;
  }
  return `${day}-${market}-${label}-${hash.toString(36).padStart(7, "0")}`.slice(0, 120);
}

/** Content fingerprint for the engine's short-TTL retry fallback only. */
function contentFingerprint(market, label, symbols) {
  let hash = 5381;
  for (const char of `${market}:${label}:${[...symbols].sort().join(",")}`) {
    hash = ((hash * 33) ^ char.codePointAt(0)) >>> 0;
  }
  return hash.toString(36).padStart(8, "0");
}

/**
 * Stable request identity from the DSH platform: the session id plus the
 * newest direct user message id. Retries inside the same user turn produce
 * the same identity; a NEW user message always produces a new one — even
 * when the content is identical. This is the primary cycle-id source; the
 * content hash is never used for it.
 */
function requestIdentity(exec) {
  const agent = exec?.agent;
  if (!agent || typeof agent !== "object") return null;
  const sessionId = String(agent.id ?? "").replace(/[^A-Za-z0-9_.-]+/g, "-").slice(0, 48);
  if (!sessionId) return null;
  const events = Array.isArray(agent.session?.events) ? agent.session.events : [];
  for (let index = events.length - 1; index >= 0; index -= 1) {
    const event = events[index];
    if (
      event?.type === "user/message" &&
      event?.message?.source?.kind === "user" &&
      typeof event.message.id === "string" &&
      event.message.id
    ) {
      const messageId = String(event.message.id).replace(/[^A-Za-z0-9_.-]+/g, "-").slice(0, 40);
      if (messageId) return `${sessionId}-${messageId}`.slice(0, 120);
      break;
    }
  }
  return sessionId;
}

/** Normalize the symbols provenance: explicit targets are user-specified
 * unless the caller says they came from a screening pass. */
function resolveSymbolsSource(requested, symbols, autonomous) {
  const source = String(requested ?? "").trim().toLowerCase();
  if (source === "user" || source === "screening" || source === "autonomous") return source;
  if (symbols.length > 0) return "user";
  return autonomous ? "autonomous" : "screening";
}

function holdingMap(portfolio) {
  const result = {};
  for (const holding of Array.isArray(portfolio?.holdings) ? portfolio.holdings : []) {
    const symbol = String(holding?.code ?? holding?.symbol ?? "").trim();
    if (symbol) result[symbol] = holding;
  }
  return result;
}

function evidenceCount(value) {
  if (!value || typeof value !== "object") return 0;
  if (Array.isArray(value)) return value.reduce((total, item) => total + evidenceCount(item), 0);
  return (Array.isArray(value.evidence) ? value.evidence.length : 0) +
    Object.entries(value).filter(([key]) => key !== "evidence").reduce((total, [, item]) => total + evidenceCount(item), 0);
}

function validBaseResearch(rows, minimumAnalysts, minimumCitations, requireCitations) {
  const good = [];
  const failed = [];
  for (const row of Array.isArray(rows) ? rows : []) {
    const analyses = (Array.isArray(row?.analyses) ? row.analyses : []).filter((item) => {
      if (!item || typeof item.summary !== "string") return false;
      return !requireCitations || (Array.isArray(item.evidence) && item.evidence.length >= minimumCitations);
    });
    if (analyses.length >= minimumAnalysts) good.push({ symbol: String(row.symbol), analyses });
    else failed.push(String(row?.symbol ?? ""));
  }
  return { good, failed: uniqueSymbols(failed) };
}

function normalizedDecisions(raw, allowedSymbols, forcedHolds) {
  const allowed = new Set(allowedSymbols);
  const forced = new Set(forcedHolds);
  const bySymbol = new Map();
  for (const item of Array.isArray(raw) ? raw : []) {
    const symbol = String(item?.symbol ?? "").trim();
    if (!allowed.has(symbol)) continue;
    let action = String(item?.action ?? "HOLD").toUpperCase();
    if (!["BUY", "SELL", "HOLD"].includes(action) || forced.has(symbol)) action = "HOLD";
    const weight = Number(item?.target_weight);
    const confidence = Number(item?.confidence);
    bySymbol.set(symbol, {
      symbol,
      action,
      target_weight: Number.isFinite(weight) ? Math.max(0, Math.min(1, weight)) : 0,
      confidence: Number.isFinite(confidence) ? Math.max(0, Math.min(1, confidence)) : 0,
      reason: forced.has(symbol) ? "基础研究未达到完整性阈值，按故障安全规则保持持仓。" : String(item?.reason ?? "") .slice(0, 1000),
      evidence_ids: uniqueSymbols(Array.isArray(item?.evidence_ids) ? item.evidence_ids : []).slice(0, 30),
    });
  }
  for (const symbol of forced) {
    if (allowed.has(symbol) && !bySymbol.has(symbol)) bySymbol.set(symbol, {symbol,action:"HOLD",target_weight:0,confidence:0,reason:"基础研究未达到完整性阈值，按故障安全规则保持持仓。",evidence_ids:[]});
  }
  return [...bySymbol.values()];
}

async function executeStage(ctx, client, exec, activeRuns, cycleId, stage, script, args, maxAgents) {
  await client.post("/api/analysis/runs/update", {cycle_id:cycleId,stage,event:"stage_start"});
  const run = ctx.workflowEngine.start({
    script,
    meta:{name:"investment-" + stage.replaceAll("_", "-"),description:"Investment Auto fixed " + stage + " stage",phases:[{title:{base_research:"基础研究",research_debate:"研究辩论与个股决策",portfolio_draft:"组合草案",risk_review:"风险辩论",final_decision:"最终决策"}[stage]}]},
    args,
    parent:exec.agent,
    signal:exec.signal,
    maxTotalAgents:Math.max(1, maxAgents),
  });
  activeRuns.set(String(run.id), {cycleId,stage});
  try {
    const settled = await run.result;
    if (settled.stopReason !== "completed") throw new Error(settled.error ?? ("workflow stage " + stage + " " + settled.stopReason));
    const result = compact(settled.value);
    await client.post("/api/analysis/runs/update", {cycle_id:cycleId,stage,event:"checkpoint",result,agents_started:settled.agentsStarted,evidence_count:evidenceCount(result)});
    return result;
  } finally {
    activeRuns.delete(String(run.id));
    await run.dispose();
  }
}

/** Register the deterministic multi-stage analysis tool. */
export function registerAnalysisWorkflow(ctx, client, registerTool) {
  const activeRuns = new Map();
  if (typeof ctx.on === "function") {
    ctx.on("workflow/agent-start", (info) => {
      const active = activeRuns.get(String(info.id));
      if (active) void client.post("/api/analysis/runs/update", {cycle_id:active.cycleId,stage:active.stage,event:"agent_start"}).catch(() => {});
    });
    ctx.on("workflow/agent-end", (info, agent) => {
      const active = activeRuns.get(String(info.id));
      if (active) void client.post("/api/analysis/runs/update", {cycle_id:active.cycleId,stage:active.stage,event:"agent_end",outcome:agent.outcome}).catch(() => {});
    });
  }

  registerTool(
    ctx,
    "investment_analysis_workflow",
    "Investment Auto 固定的多角色投资分析流程入口，是唯一可信的完整/深度分析实现。用户点名证券时把 symbols 传入（symbols_source=user），只分析这些标的、绝不混入选股池；用户要求先选股时，先用选股工具得到标准化列表，再把结果以 symbols_source=screening 传入。手动对话中本工具是启动器：返回 cycle_id 后立即用 investment_analysis_status 轮询进度，不要重复调用本工具，也不要自己用行情/新闻工具重写分析。流程固定为：四类基础研究→多空辩论→研究经理→个股交易员→组合草案→三方风险辩论→风险经理→最终组合经理，各阶段持久化并可续跑；分析只生成方案，成交必须等用户批准后用 investment_submit_decisions（带同一 idempotency_key）提交。",
    {
      market:{type:"string",required:true,description:"市场代码：cn、hk、us 或 etf",default:"cn"},
      symbols:{type:"array",description:"目标证券代码列表；用户点名或选股结果。留空则只允许在自主调度轮次中由流程内部选股",items:{type:"string"}},
      symbols_source:{type:"string",description:"symbols 的来源：user（用户指定）或 screening（选股结果）；留空自动推断"},
      submit:{type:"boolean",description:"是否提交最终决策；手动会话必须为 false",default:false},
      label:{type:"string",description:"轮次标签"},
      cycle_id:{type:"string",description:"可选稳定幂等键；同一键只启动一个轮次，超时重试不会重复启动"},
    },
    async ({market,symbols:requestedSymbols,symbols_source:requestedSource,submit=false,label="analysis",cycle_id:requestedCycleId}, exec) => {
      const normalizedMarket = String(market ?? "").trim().toLowerCase();
      if (!MARKETS.has(normalizedMarket)) throw new Error("market 必须是 cn、hk、us 或 etf");
      const autonomous = process.env.IA_AUTONOMOUS_ROUND === "1";
      if (submit && !autonomous) throw new Error("手动对话不能直接提交；请先展示方案并取得用户确认，再使用受控提交工具");
      const symbols = uniqueSymbols(Array.isArray(requestedSymbols) ? requestedSymbols : []);
      const symbolsSource = resolveSymbolsSource(requestedSource, symbols, autonomous);
      const safeLabel = String(label || "analysis").replace(/[^A-Za-z0-9_.-]+/g,"-").slice(0,40) || "analysis";

      // ── two-block boundary, enforced in code: the MANUAL entry requires an
      // explicit standardized symbol list (user-named or screening-derived).
      // Only autonomous scheduling may fall back to internal screening. ──
      if (!autonomous && symbols.length === 0) {
        throw new Error("手动分析必须明确标的：请先运行选股（investment_run_screening / investment_screening）得到标准化候选列表，再把结果以 symbols 传入本工具（symbols_source=\"screening\"）；用户点名的股票直接以 symbols 传入（symbols_source=\"user\"）。本入口不允许空股票列表。");
      }

      // ── cycle id: platform request identity is the PRIMARY source; the
      // engine-injected id covers headless rounds; a random id covers the
      // rare executor without session context. ──
      const identity = requestIdentity(exec);
      const cycleId = String(
        requestedCycleId ||
        process.env.INVESTMENT_CYCLE_ID ||
        (identity ? "req-" + identity : "auto-" + String(typeof crypto !== "undefined" && crypto.randomUUID ? crypto.randomUUID() : Date.now()).replace(/[^A-Za-z0-9_.-]+/g, "-"))
      ).replace(/[^A-Za-z0-9_.-]+/g,"-").slice(0,120);

      if (!autonomous) {
        // Web/manual session: launch the durable engine round and return
        // immediately. The heavy pipeline runs in the engine's headless
        // worker — the SAME fixed workflow implementation the scheduler
        // uses — so no browser or tool HTTP request has to stay open for
        // the analysis duration, and a retry with the same cycle_id never
        // starts a second round.
        const started = await client.post("/api/analysis/rounds/start", {
          cycle_id: cycleId,
          market: normalizedMarket,
          symbols,
          symbols_source: symbolsSource,
          label: safeLabel,
          // Only used by the engine's short-TTL retry fallback when the
          // launcher lacked session context (identity-less ids).
          dedupe_fingerprint: identity ? undefined : contentFingerprint(normalizedMarket, safeLabel, symbols),
        }, { timeoutMs: 60000 });
        const run = started?.analysis ?? {};
        return {
          mode: "async",
          cycle_id: cycleId,
          started: Boolean(started?.started),
          duplicate: Boolean(started?.duplicate),
          status: run.status ?? "running",
          current_stage: run.current_stage ?? "preparing",
          symbols: run.symbols ?? symbols,
          symbols_source: run.symbols_source ?? symbolsSource,
          hint: "固定分析流程已在后台启动。请用 investment_analysis_status(cycle_id=\"" + cycleId + "\") 轮询进度，直到状态为 ready_for_execution（分析完成，等待用户批准）或 failed；不要再次调用本工具，也不要自行调用行情/新闻工具重写分析。",
        };
      }

      if (!exec?.agent) throw new Error("全流程分析需要从一个有效的 DSH 会话调用");
      let started = false;
      try {
        const [status, portfolio, mandatePayload, configPayload, previousPayload] = await Promise.all([
          client.get("/api/status"),
          client.get("/api/portfolio/"+normalizedMarket),
          client.get("/api/mandate"),
          client.get("/api/config"),
          client.get("/api/analysis/runs?market="+normalizedMarket+"&limit=3").catch(() => ({runs:[]})),
        ]);
        if (status?.control?.kill_switch || status?.control?.kill_switch_active || status?.control?.paused) throw new Error("引擎已暂停或紧急停止，分析轮次未启动");
        const holdings = holdingMap(portfolio);
        const holdingSymbols = Object.keys(holdings);
        const config = configPayload?.config ?? {};
        const workflowConfig = config?.autonomous?.agent_workflow ?? {};
        const maxUniverse = Math.max(1, Number(config?.autonomous?.max_universe_size ?? 30));

        // ── target universe: explicit symbols win; screening is the fallback ──
        let targets = uniqueSymbols(symbols);
        let screeningNote = null;
        if (targets.length === 0) {
          let screeningPayload = await client.get("/api/screening/"+normalizedMarket);
          if (!screeningPayload?.cached || !screeningPayload?.screening) {
            screeningPayload = await client.issue("run_screening", {market:normalizedMarket}, {requestedBy:"dsh-analysis-workflow",timeoutMs:300000});
          }
          const screening = screeningPayload?.screening ?? screeningPayload;
          const selected = uniqueSymbols(screening?.selected_symbols ?? screening?.allowed_symbols ?? []);
          screeningNote = "目标来自选股池与持仓（自动轮次内部选股）";
          targets = uniqueSymbols([...holdingSymbols,...selected]).slice(0,maxUniverse);
        } else {
          screeningNote = "目标来自调用方显式指定（" + symbolsSource + "），未混入选股池标的";
        }
        if (targets.length === 0) throw new Error("选股与持仓均为空，无法启动分析");
        if (targets.length > maxUniverse) targets = targets.slice(0, maxUniverse);

        const researchRounds = Math.max(1,Math.min(3,Number(workflowConfig.research_debate_rounds ?? 1)));
        const riskRounds = Math.max(1,Math.min(3,Number(workflowConfig.risk_debate_rounds ?? 1)));
        const expectedAgents = targets.length * (6 + 2 * researchRounds) + 3 * riskRounds + 3;
        const startPayload = await client.post("/api/analysis/runs/start", {cycle_id:cycleId,market:normalizedMarket,label:safeLabel,symbols:targets,symbols_source:symbolsSource,submit:Boolean(submit),holding_symbols:holdingSymbols,expected_agents:expectedAgents});
        started = true;
        const runState = startPayload.analysis ?? {};
        if (runState.status === "completed") return {...runState,resumed:true};
        const checkpoints = runState.checkpoints ?? {};
        const mandate = mandatePayload?.mandate ?? {};
        const memory = (previousPayload?.runs ?? []).filter((row) => row.cycle_id !== cycleId).slice(0, Number(workflowConfig.memory_entries_per_role ?? 6)).map((row) => ({decisions:row.decisions??[],warnings:row.warnings??[],completed_at:row.completed_at}));
        const minimumAnalysts = Math.max(4, Number(workflowConfig.minimum_base_analysts ?? 4));
        const minimumCitations = Math.max(1, Number(workflowConfig.minimum_citations ?? 1));
        const requireCitations = workflowConfig.require_citations !== false;

        const base = checkpoints.base_research?.result ?? await executeStage(ctx,client,exec,activeRuns,cycleId,"base_research",BASE_RESEARCH_SCRIPT,{symbols:targets,market:normalizedMarket,roles:Object.keys(ROLE_INSTRUCTIONS),role_instructions:ROLE_INSTRUCTIONS,memory},targets.length*4);
        const validation = validBaseResearch(base,minimumAnalysts,minimumCitations,requireCitations);
        const successRatio = validation.good.length / targets.length;
        const requiredRatio = Math.max(0,Math.min(1,Number(workflowConfig.minimum_symbol_research_success_ratio ?? 0.8)));
        if (successRatio < requiredRatio) throw new Error("基础研究成功率 " + successRatio.toFixed(2) + " 未达到阈值 " + requiredRatio.toFixed(2) + "，本轮按故障安全规则停止提交");
        const failedHoldings = validation.failed.filter((symbol) => holdingSymbols.includes(symbol));
        const debate = checkpoints.research_debate?.result ?? await executeStage(ctx,client,exec,activeRuns,cycleId,"research_debate",RESEARCH_DEBATE_SCRIPT,{research:validation.good,mandate,holdings,rounds:researchRounds},Math.max(1,validation.good.length*(2*researchRounds+2)));
        const recommendations = (Array.isArray(debate) ? debate : []).map((row) => row?.trader).filter(Boolean);
        const draft = checkpoints.portfolio_draft?.result ?? await executeStage(ctx,client,exec,activeRuns,cycleId,"portfolio_draft",PORTFOLIO_SCRIPT,{portfolio,mandate,recommendations},1);
        const risk = checkpoints.risk_review?.result ?? await executeStage(ctx,client,exec,activeRuns,cycleId,"risk_review",RISK_SCRIPT,{draft,portfolio,mandate,rounds:riskRounds},3*riskRounds+1);
        const finalResult = checkpoints.final_decision?.result ?? await executeStage(ctx,client,exec,activeRuns,cycleId,"final_decision",FINAL_SCRIPT,{risk,recommendations,mandate},1);
        const decisions = normalizedDecisions(finalResult?.decisions,targets,failedHoldings);
        const warnings = validation.failed.map((symbol) => holdingSymbols.includes(symbol) ? symbol+" 基础研究不完整，已强制 HOLD" : symbol+" 基础研究不完整，已从候选决策排除");
        // Persist the final decisions FIRST and enter ready_for_execution:
        // the engine canonicalizes the decisions, computes the decision
        // fingerprint and stores both. A later submission bound to this
        // cycle_id must carry exactly this content.
        const readyPayload = await client.post("/api/analysis/runs/update",{cycle_id:cycleId,stage:"final_decision",event:"execution_ready",decisions,result:compact(finalResult)});
        const readyRun = readyPayload?.analysis ?? {};
        let execution = {status:"not_submitted",reason:"本次仅生成研究方案，未进入模拟撮合"};
        if (submit) {
          // Autonomous scheduling: ready_for_execution + submit=true is the
          // one path that may execute. The engine verifies the fingerprint
          // against the ready record, applies hard risk controls and the
          // paper broker, and its execution receipt (same atomic write as
          // the account mutation) guarantees exactly-once fills; the bound
          // run is finalized by the engine.
          execution = await client.issue("submit_decisions",{market:normalizedMarket,decisions,label:safeLabel,note:String(finalResult?.summary ?? "全流程多角色分析完成").slice(0,1800),idempotency_key:cycleId},{requestedBy:"dsh-analysis-workflow",timeoutMs:300000});
          await client.post("/api/analysis/runs/update",{cycle_id:cycleId,stage:"execution",event:"checkpoint",result:compact(execution),agents_started:0,evidence_count:evidenceCount(base)});
          await client.post("/api/analysis/runs/complete",{cycle_id:cycleId,decisions,execution:compact(execution),report:execution?.report??null,audit_file:execution?.audit_file??null,warnings});
          return {...readyRun,decisions,execution:compact(execution),base_research_success_ratio:successRatio,screening_note:screeningNote,symbols_source:symbolsSource,resumed:Object.keys(checkpoints).length>0};
        }
        // Manual rounds END at ready_for_execution: analysis is done, trading
        // waits for the user's explicit approval on a later submission call.
        return {...readyRun,decisions,execution,base_research_success_ratio:successRatio,screening_note:screeningNote,symbols_source:symbolsSource,resumed:Object.keys(checkpoints).length>0};
      } catch (error) {
        if (started) await client.post("/api/analysis/runs/fail",{cycle_id:cycleId,error:String(error?.message??error)}).catch(() => {});
        throw error;
      }
    },
    {timeoutMs:1800000},
  );

  registerTool(
    ctx,
    "investment_analysis_status",
    "查询固定分析流程轮次（investment_analysis_workflow 启动）的实时状态：阶段、检查点、Agent 进度与最终决策/成交。分析轮次在后台执行，启动后应轮询本工具直到状态为 completed 或 failed，再向用户总结。",
    {
      cycle_id:{type:"string",description:"轮次幂等键；留空返回最近一次轮次"},
      market:{type:"string",description:"可选：只查某市场最近的轮次（cn/hk/us/etf）"},
    },
    async ({cycle_id, market}) => {
      let payload;
      if (cycle_id) {
        payload = await client.get("/api/analysis/run?cycle_id="+encodeURIComponent(cycle_id));
        if (!payload?.ok && payload?.analysis === undefined) throw new Error(payload?.error ?? "轮次不存在");
      } else {
        payload = await client.get("/api/analysis/latest"+(market ? "?market="+encodeURIComponent(String(market).toLowerCase()) : ""));
      }
      const run = payload?.analysis ?? null;
      if (!run) return {status:"none",hint:"尚无分析轮次记录；请先调用 investment_analysis_workflow 启动固定流程"};
      const compactRun = compact(run);
      const terminal = run.status === "completed" || run.status === "failed" || run.status === "ready_for_execution";
      let hint;
      if (run.status === "ready_for_execution") {
        hint = "分析已完成（ready_for_execution）。把最终决策清单呈现给用户并等待明确批准；批准后用 investment_submit_decisions 原样提交，idempotency_key 必须等于本轮的 cycle_id（" + String(run.cycle_id ?? "") + "）。";
      } else if (run.status === "running") {
        hint = "轮次仍在运行（" + String(run.current_stage ?? "preparing") + "）。稍后再次调用本工具查询，不要重复启动轮次。";
      } else if (run.status === "failed") {
        hint = "轮次失败：" + String(run.error ?? "未知错误") + "。可用同一 cycle_id 重新调用 investment_analysis_workflow 安全重试（从已有检查点继续）。";
      } else {
        hint = "轮次已结束（" + run.status + "）。向用户复述最终决策、成交/拒绝与风险提示；未经用户批准不得提交任何买卖。";
      }
      return {
        ...compactRun,
        done: terminal,
        hint,
      };
    },
    {timeoutMs: 30000},
  );
}

export { STAGES, validBaseResearch, normalizedDecisions, stableCycleId, resolveSymbolsSource, requestIdentity, contentFingerprint };
