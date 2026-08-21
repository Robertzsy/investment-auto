/**
 * Unit tests for the investment tools plugin (node:test, no model needed).
 *
 * Runs against an in-process mock engine HTTP server and a fake `ctx.tools`
 * registry. Registration validity against the real registry is covered by the
 * profile boot test (the web app mounts this row on startup).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { apply } from "../lib/index.js";

function startMockEngine() {
  const state = {
    calls: [],
    commands: [],
  };
  const server = createServer((req, res) => {
    state.calls.push(req.url);
    const send = (payload, status = 200) => {
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(payload));
    };
    if (req.url.startsWith("/api/status")) return send({ ok: true, operation_mode: "manual", control: { paused: false } });
    if (req.url.startsWith("/api/portfolio/")) {
      const market = req.url.split("/").pop();
      return send({ ok: true, market, cash: 100000, holdings: [] });
    }
    if (req.url.startsWith("/api/market/snapshot")) return send({ ok: true, symbol: "600519", realtime: { price: 1500 } });
    if (req.url.startsWith("/api/commands/issue")) {
      let body = "";
      req.on("data", (chunk) => (body += chunk));
      req.on("end", () => {
        const parsed = JSON.parse(body);
        state.commands.push(parsed);
        send({ ok: true, command: parsed.command, status: "generated" });
      });
      return;
    }
    send({ ok: false, error: "not found" }, 404);
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => {
      resolve({ server, state, url: `http://127.0.0.1:${server.address().port}` });
    });
  });
}

function fakeCtx() {
  const registered = [];
  return {
    registered,
    tools: {
      register(definition) {
        registered.push(definition);
        return () => {};
      },
    },
  };
}

test("registers the full investment tool surface", async () => {
  const { server, url } = await startMockEngine();
  const ctx = fakeCtx();
  try {
    apply(ctx, { engineUrl: url });

    const names = ctx.registered.map((tool) => tool.name);
    for (const expected of [
      "investment_status",
      "investment_portfolio",
      "investment_market_snapshot",
      "investment_market_history",
      "investment_security_search",
      "investment_screening",
      "investment_optimizer_latest",
      "investment_reports",
      "investment_report_latest",
      "investment_macro_latest",
      "investment_mandate",
      "investment_set_strategy",
      "investment_control",
      "investment_run_cycle",
      "investment_submit_decisions",
      "investment_run_screening",
      "investment_reset_account",
    ]) {
      assert.ok(names.includes(expected), `missing tool ${expected}`);
    }

    for (const tool of ctx.registered) {
      assert.ok(tool.name.startsWith("investment_"), tool.name);
      assert.ok(typeof tool.description === "string" && tool.description.length > 0, `${tool.name} description`);
      assert.ok(tool.output && tool.output.schema && typeof tool.output.render === "function", `${tool.name} output`);
      assert.ok(typeof tool.execute === "function", `${tool.name} execute`);
      assert.ok(Number.isFinite(tool.timeoutMs) && tool.timeoutMs > 0, `${tool.name} timeoutMs`);
    }
  } finally {
    server.close();
  }
});

test("read tools hit the engine endpoints", async () => {
  const { server, state, url } = await startMockEngine();
  const ctx = fakeCtx();
  try {
    apply(ctx, { engineUrl: url });

    const status = await ctx.registered.find((t) => t.name === "investment_status").execute({});
    assert.equal(status.operation_mode, "manual");

    const portfolio = await ctx.registered.find((t) => t.name === "investment_portfolio").execute({ market: "CN" });
    assert.equal(portfolio.market, "cn");
    assert.equal(portfolio.cash, 100000);

    const snapshot = await ctx.registered.find((t) => t.name === "investment_market_snapshot").execute({ symbol: "600519" });
    assert.equal(snapshot.realtime.price, 1500);

    assert.ok(state.calls.some((path) => path.startsWith("/api/portfolio/cn")));
    assert.ok(state.calls.some((path) => path.includes("symbol=600519")));
  } finally {
    server.close();
  }
});

test("write tools dispatch engine commands", async () => {
  const { server, state, url } = await startMockEngine();
  const ctx = fakeCtx();
  try {
    apply(ctx, { engineUrl: url });

    await ctx.registered.find((t) => t.name === "investment_set_strategy").execute({ profile: "conservative" });
    await ctx.registered.find((t) => t.name === "investment_control").execute({ action: "pause", reason: "test" });
    await ctx.registered.find((t) => t.name === "investment_run_cycle").execute({ market: "us", label: "smoke" });

    assert.deepEqual(
      state.commands.map((command) => command.command),
      ["set_strategy", "pause", "run_cycle"],
    );
    assert.equal(state.commands[0].payload.profile, "conservative");
    assert.equal(state.commands[1].payload.reason, "test");
    assert.equal(state.commands[2].payload.market, "us");
    assert.equal(state.commands[2].payload.label, "smoke");
    for (const command of state.commands) assert.equal(command.requested_by, "dsh-tools");
  } finally {
    server.close();
  }
});

test("investment_control rejects unknown actions without hitting the engine", async () => {
  const { server, state, url } = await startMockEngine();
  const ctx = fakeCtx();
  try {
    apply(ctx, { engineUrl: url });
    const tool = ctx.registered.find((t) => t.name === "investment_control");
    await assert.rejects(() => tool.execute({ action: "explode" }), /pause、resume、kill/);
    assert.equal(state.calls.length, 0);
  } finally {
    server.close();
  }
});

test("output render returns content blocks from the value, never the arguments", async () => {
  const { server, url } = await startMockEngine();
  const ctx = fakeCtx();
  try {
    apply(ctx, { engineUrl: url });
    const tool = ctx.registered.find((t) => t.name === "investment_status");
    // Registry contract: render(args, value) -> ContentBlock[]. A render
    // that mistakes the first argument for the value collapses every result
    // to "{}"; one that returns a bare string corrupts the tool-result
    // message shape.
    const rendered = tool.output.render({ market: "cn" }, { ok: true, hello: "世界" });
    assert.ok(Array.isArray(rendered));
    assert.equal(rendered[0].type, "text");
    assert.ok(rendered[0].text.includes('"hello"'));
    assert.ok(rendered[0].text.includes("世界"));
    assert.ok(!rendered[0].text.includes("market"));
  } finally {
    server.close();
  }
});

test("environment variable overrides the configured engine URL", async () => {
  const previous = process.env.INVESTMENT_ENGINE_URL;
  process.env.INVESTMENT_ENGINE_URL = "http://127.0.0.1:9999";
  const ctx = fakeCtx();
  try {
    apply(ctx, { engineUrl: "http://127.0.0.1:1234" });
    const tool = ctx.registered.find((t) => t.name === "investment_status");
    await assert.rejects(() => tool.execute({}), /fetch failed|ECONNREFUSED/);
  } finally {
    if (previous === undefined) delete process.env.INVESTMENT_ENGINE_URL;
    else process.env.INVESTMENT_ENGINE_URL = previous;
  }
});
