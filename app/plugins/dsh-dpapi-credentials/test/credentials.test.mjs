/**
 * Unit tests for the DPAPI credentials provider (node:test, mock engine).
 */
import { test } from "node:test";
import assert from "node:assert/strict";
import { createServer } from "node:http";
import { apply } from "../lib/index.js";

function startMockEngine() {
  const store = new Map();
  const server = createServer((req, res) => {
    const send = (payload, status = 200) => {
      res.writeHead(status, { "Content-Type": "application/json" });
      res.end(JSON.stringify(payload));
    };
    if (req.method === "GET" && req.url.startsWith("/api/credentials/resolve")) {
      const ref = new URL(req.url, "http://x").searchParams.get("ref");
      const value = store.get(ref);
      return send({ ok: true, ref, configured: value !== undefined, value: value ?? "" });
    }
    if (req.method === "POST" && req.url === "/api/credentials/set") {
      let body = "";
      req.on("data", (chunk) => (body += chunk));
      req.on("end", () => {
        const parsed = JSON.parse(body);
        if (parsed.value) store.set(parsed.ref, parsed.value);
        else store.delete(parsed.ref);
        return send({ ok: true, ref: parsed.ref, configured: !!parsed.value });
      });
      return;
    }
    if (req.method === "POST" && req.url === "/api/credentials/unset") {
      let body = "";
      req.on("data", (chunk) => (body += chunk));
      req.on("end", () => {
        const parsed = JSON.parse(body);
        store.delete(parsed.ref);
        return send({ ok: true, ref: parsed.ref, configured: false });
      });
      return;
    }
    send({ ok: false, error: "not found" }, 404);
  });
  return new Promise((resolve) => {
    server.listen(0, "127.0.0.1", () => resolve({ server, url: `http://127.0.0.1:${server.address().port}` }));
  });
}

function fakeCtx() {
  const events = [];
  let provided;
  return {
    events,
    get service() {
      return provided;
    },
    provide(key, service) {
      assert.equal(key, "credentials");
      provided = service;
    },
    emit(name, ...args) {
      events.push([name, ...args]);
    },
  };
}

test("resolve/describe/set/unset roundtrip through the engine", async () => {
  const { server, url } = await startMockEngine();
  const ctx = fakeCtx();
  try {
    apply(ctx, { engineUrl: url });
    const service = ctx.service;

    assert.equal(await service.resolve("DEEPSEEK_API_KEY"), undefined);
    const described = await service.describe("DEEPSEEK_API_KEY");
    assert.deepEqual(described, { configured: false, writable: true });

    await service.set("DEEPSEEK_API_KEY", "sk-test");
    assert.deepEqual(await service.resolve("DEEPSEEK_API_KEY"), { value: "sk-test", source: "dpapi" });
    assert.equal((await service.describe("DEEPSEEK_API_KEY")).configured, true);

    await service.unset("DEEPSEEK_API_KEY");
    assert.equal(await service.resolve("DEEPSEEK_API_KEY"), undefined);

    assert.deepEqual(ctx.events.map((event) => event[0]), ["credentials/updated", "credentials/updated"]);
  } finally {
    server.close();
  }
});

test("engine down degrades to unconfigured without throwing", async () => {
  const ctx = fakeCtx();
  apply(ctx, { engineUrl: "http://127.0.0.1:1" });
  const service = ctx.service;

  assert.equal(await service.resolve("DEEPSEEK_API_KEY"), undefined);
  assert.deepEqual(await service.describe("DEEPSEEK_API_KEY"), { configured: false, writable: true });
});

test("environment variables override configured engine URL and token", async () => {
  const { server, url } = await startMockEngine();
  const previousUrl = process.env.INVESTMENT_ENGINE_URL;
  process.env.INVESTMENT_ENGINE_URL = url;
  const ctx = fakeCtx();
  try {
    apply(ctx, { engineUrl: "http://127.0.0.1:1234" });
    const service = ctx.service;
    await service.set("KIMI_API_KEY", "sk-kimi");
    assert.deepEqual(await service.resolve("KIMI_API_KEY"), { value: "sk-kimi", source: "dpapi" });
  } finally {
    if (previousUrl === undefined) delete process.env.INVESTMENT_ENGINE_URL;
    else process.env.INVESTMENT_ENGINE_URL = previousUrl;
    server.close();
  }
});
