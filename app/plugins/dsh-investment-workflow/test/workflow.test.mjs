import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { assertObjectJsonSchema } from "@deepseek-ai/dsh-tools";
import { apply, inject } from "../lib/index.js";
import {
  WORKFLOW_AGENT_SCHEMAS,
  normalizedDecisions,
  validBaseResearch,
  stableCycleId,
  resolveSymbolsSource,
  requestIdentity,
  contentFingerprint,
} from "../../dsh-investment-tools/lib/analysis-workflow.js";

test("every workflow agent schema matches the installed DSH subset", () => {
  for (const [name, schema] of Object.entries(WORKFLOW_AGENT_SCHEMAS)) {
    assert.doesNotThrow(() => assertObjectJsonSchema(schema), name);
  }
});

function fakeCtx() {
  const registered = [];
  return {
    registered,
    workflowEngine: {},
    tools: { register(tool) { registered.push(tool); } },
    on() { return () => {}; },
  };
}

function startMockEngine(state = {}) {
  const server = createServer((req, res) => {
    let body = "";
    req.on("data", (chunk) => (body += chunk));
    req.on("end", () => {
      state.calls = state.calls ?? [];
      state.calls.push({ url: req.url, body: body ? JSON.parse(body) : null });
      const send = (payload, status = 200) => {
        res.writeHead(status, { "Content-Type": "application/json" });
        res.end(JSON.stringify(payload));
      };
      if (req.url === "/api/analysis/rounds/start") {
        return send({ ok: true, started: true, analysis: { cycle_id: state.calls[state.calls.length - 1].body.cycle_id, status: "running", current_stage: "preparing" } });
      }
      if (req.url.startsWith("/api/analysis/run")) {
        return send({ ok: true, analysis: { cycle_id: "x", status: "completed", decisions: [] } });
      }
      send({ ok: false, error: "not found" }, 404);
    });
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      resolve({ server, state, url: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

test("registers the fixed workflow entry and its status poller", () => {
  const ctx = fakeCtx();
  apply(ctx, { engineUrl: "http://127.0.0.1:1" });
  assert.deepEqual(inject, ["tools", "workflowEngine"]);
  assert.deepEqual(ctx.registered.map((tool) => tool.name).sort(), ["investment_analysis_status", "investment_analysis_workflow"]);
});

test("manual sessions cannot ask the full workflow to submit", async () => {
  const ctx = fakeCtx();
  apply(ctx, { engineUrl: "http://127.0.0.1:1" });
  const tool = ctx.registered.find((t) => t.name === "investment_analysis_workflow");
  await assert.rejects(
    () => tool.execute({ market: "cn", submit: true }, { agent: {} }),
    /手动对话不能直接提交/,
  );
});

test("manual entry rejects empty symbols (screening must run first)", async () => {
  const ctx = fakeCtx();
  apply(ctx, { engineUrl: "http://127.0.0.1:1" });
  const tool = ctx.registered.find((t) => t.name === "investment_analysis_workflow");
  await assert.rejects(
    () => tool.execute({ market: "cn", symbols: [] }, { agent: { id: "sess-1", session: { events: [] } } }),
    /请先运行选股/,
  );
});

test("web sessions launch the durable engine round and return immediately", async () => {
  const { server, state, url } = await startMockEngine();
  const ctx = fakeCtx();
  try {
    apply(ctx, { engineUrl: url });
    const tool = ctx.registered.find((t) => t.name === "investment_analysis_workflow");
    const result = await tool.execute(
      { market: "cn", symbols: ["688981"], symbols_source: "user", cycle_id: "20260822-cn-user-0000001" },
      {},
    );
    assert.equal(result.mode, "async");
    assert.equal(result.cycle_id, "20260822-cn-user-0000001");
    assert.equal(result.status, "running");
    assert.match(result.hint, /investment_analysis_status/);
    const start = state.calls.find((call) => call.url === "/api/analysis/rounds/start");
    assert.ok(start, "launcher must POST the round start endpoint");
    assert.deepEqual(start.body.symbols, ["688981"]);
    assert.equal(start.body.symbols_source, "user");
    assert.equal(start.body.submit, undefined);
  } finally {
    server.close();
  }
});

test("status tool reads the durable run record", async () => {
  const { server, url } = await startMockEngine();
  const ctx = fakeCtx();
  try {
    apply(ctx, { engineUrl: url });
    const tool = ctx.registered.find((t) => t.name === "investment_analysis_status");
    const result = await tool.execute({ cycle_id: "20260822-cn-user-0000001" });
    assert.equal(result.status, "completed");
    assert.equal(result.done, true);
  } finally {
    server.close();
  }
});

test("stable cycle ids are content-deterministic (same request, same key)", () => {
  const a = stableCycleId("cn", "analysis", ["688981", "600519"]);
  const b = stableCycleId("cn", "analysis", ["600519", "688981"]);
  const c = stableCycleId("cn", "analysis", ["688981"]);
  assert.equal(a, b);
  assert.notEqual(a, c);
  assert.match(a, /^[A-Za-z0-9][A-Za-z0-9_.-]{0,119}$/);
});

test("request identity is session + newest direct user message", () => {
  const events = (messageIds) => messageIds.map((id) => ({ type: "user/message", message: { id, source: { kind: "user" } } }));
  const exec = (events) => ({ agent: { id: "session-abc", session: { events } } });
  const first = requestIdentity(exec(events(["m1", "m2"])));
  assert.equal(first, "session-abc-m2");
  // Retry inside the same user turn: identical identity.
  assert.equal(requestIdentity(exec(events(["m1", "m2"]))), first);
  // A NEW user message: new identity, even with identical analysis content.
  assert.equal(requestIdentity(exec(events(["m1", "m2", "m3"]))), "session-abc-m3");
  // Injected context (plugin/model/tool sources) never counts as a user request.
  const injected = [
    { type: "user/message", message: { id: "m1", source: { kind: "user" } } },
    { type: "user/message", message: { id: "injected", source: { kind: "plugin", plugin: "x" } } },
  ];
  assert.equal(requestIdentity(exec(injected)), "session-abc-m1");
  // No agent context -> null (engine TTL fallback takes over).
  assert.equal(requestIdentity({}), null);
  assert.equal(requestIdentity(null), null);
});

test("launcher derives its cycle id from the request identity", async () => {
  const { server, state, url } = await startMockEngine();
  const ctx = fakeCtx();
  try {
    apply(ctx, { engineUrl: url });
    const tool = ctx.registered.find((t) => t.name === "investment_analysis_workflow");
    const events = [{ type: "user/message", message: { id: "user-msg-9", source: { kind: "user" } } }];
    const result = await tool.execute(
      { market: "cn", symbols: ["688981"], symbols_source: "user" },
      { agent: { id: "session-abc", session: { events } } },
    );
    assert.equal(result.cycle_id, "req-session-abc-user-msg-9");
    const start = state.calls.find((call) => call.url === "/api/analysis/rounds/start");
    assert.ok(start);
    assert.equal(start.body.cycle_id, "req-session-abc-user-msg-9");
    // Identity present: no TTL fingerprint is needed.
    assert.equal(start.body.dedupe_fingerprint, undefined);
  } finally {
    server.close();
  }
});

test("symbols provenance is explicit and defaults honestly", () => {
  assert.equal(resolveSymbolsSource("user", ["x"], false), "user");
  assert.equal(resolveSymbolsSource("screening", ["x"], false), "screening");
  assert.equal(resolveSymbolsSource(undefined, ["x"], false), "user");
  assert.equal(resolveSymbolsSource(undefined, [], false), "screening");
  assert.equal(resolveSymbolsSource(undefined, [], true), "autonomous");
});

test("research validation and safe HOLD normalization are deterministic", () => {
  const rows = [{
    symbol: "HELD",
    analyses: Array.from({ length: 4 }, (_, index) => ({ summary: "ok", evidence: [{ id: "E" + index }] })),
  }, { symbol: "BROKEN", analyses: [{ summary: "missing evidence", evidence: [] }] }];
  const checked = validBaseResearch(rows, 4, 1, true);
  assert.deepEqual(checked.good.map((row) => row.symbol), ["HELD"]);
  assert.deepEqual(checked.failed, ["BROKEN"]);
  const decisions = normalizedDecisions(
    [{ symbol: "HELD", action: "BUY", target_weight: 2, confidence: 1.5, reason: "draft" }],
    ["HELD"],
    ["HELD"],
  );
  assert.equal(decisions[0].action, "HOLD");
  assert.equal(decisions[0].target_weight, 1);
  assert.equal(decisions[0].confidence, 1);
});

test("user-specified symbols never absorb screening-pool symbols into decisions", () => {
  // The decision normalizer keeps only the analyzed universe: passing an
  // allowed list of user symbols must drop pool symbols the model smuggles in.
  const decisions = normalizedDecisions(
    [
      { symbol: "USER1", action: "BUY", target_weight: 0.1, confidence: 0.8, reason: "ok", evidence_ids: [] },
      { symbol: "POOL9", action: "BUY", target_weight: 0.1, confidence: 0.8, reason: "must not appear", evidence_ids: [] },
    ],
    ["USER1"],
    [],
  );
  assert.deepEqual(decisions.map((decision) => decision.symbol), ["USER1"]);
});
