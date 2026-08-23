/**
 * Investment Auto product shell — node half.
 *
 * 1. Ensures the single fixed Investment Auto workspace exists (the product
 *    has no workspace UI; sessions live in one workspace rooted at the
 *    harness home / user data directory).
 * 2. Serves the investment data proxy the Dashboard and settings pages use:
 *    /api/investment/summary   — aggregated engine state (Dashboard)
 *    /api/investment/config    — GET/POST engine configuration
 *    /api/investment/command   — POST engine command dispatch
 *    The engine loopback URL and access token stay on the host side; they
 *    never reach browser JavaScript.
 */
import { homedir } from "node:os";
import { join } from "node:path";
import { mkdirSync } from "node:fs";

export const name = "@investment-auto/dsh-product-shell";
export const inject = ["webServer", "workspaceRegistry"];

const engineBase = () =>
  (process.env.INVESTMENT_ENGINE_URL ?? "http://127.0.0.1:8790").replace(/\/+$/, "");
const engineToken = () => process.env.IA_ACCESS_TOKEN ?? "";

async function engineFetch(path, { method = "GET", body } = {}) {
  const response = await fetch(engineBase() + path, {
    method,
    headers: {
      "Content-Type": "application/json",
      ...(engineToken() ? { "X-IA-Token": engineToken() } : {}),
    },
    ...(body !== undefined ? { body: JSON.stringify(body) } : {}),
  });
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(`engine ${path} -> HTTP ${response.status}: ${payload?.error ?? ""}`);
  }
  return payload;
}

function sendJson(res, status, payload) {
  const body = JSON.stringify(payload);
  res.writeHead(status, {
    "Content-Type": "application/json; charset=utf-8",
    "Content-Length": Buffer.byteLength(body),
  });
  res.end(body);
}

async function readBody(req, limit = 1_000_000) {
  const chunks = [];
  let size = 0;
  for await (const chunk of req) {
    size += chunk.length;
    if (size > limit) throw new Error("request body too large");
    chunks.push(chunk);
  }
  const text = Buffer.concat(chunks).toString("utf-8");
  return text ? JSON.parse(text) : {};
}

export function apply(ctx) {
  // Fixed product workspace (idempotent by path; the record survives reboots).
  const home = process.env.DSH_HOME || join(homedir(), ".dsh");
  ctx.effect(() => {
    mkdirSync(home, { recursive: true });
    ctx.workspaceRegistry
      .create(home, "Investment Auto")
      .catch((error) => ctx.logger.warn("investment workspace create failed: %s", error.message));
  }, "investment-auto: fixed workspace");

  // Long-running conversation turns (a fixed analysis round takes minutes)
  // must never be killed by Node's default 5-minute requestTimeout. The
  // server instance is created asynchronously by the webserver service;
  // disarm the timeout as soon as it exists. Analysis rounds themselves are
  // asynchronous engine jobs, so no request should actually hold that long —
  // this removes the last transport-level failure mode behind the observed
  // "fetch failed at ~5 minutes, backend kept running" retry storms.
  ctx.effect(() => {
    let stopped = false;
    const timer = setInterval(() => {
      if (stopped) return;
      const server = ctx.webServer.server;
      if (server === undefined || server === null) return;
      try {
        if (server.requestTimeout > 0) {
          server.requestTimeout = 0;
          ctx.logger.info("investment-auto: web server requestTimeout disabled (async rounds own their deadlines)");
        }
      } catch {
        // Non-configurable or already destroyed; nothing to do.
      }
      clearInterval(timer);
    }, 250);
    return () => {
      stopped = true;
      clearInterval(timer);
    };
  }, "investment-auto: request-timeout hardening");

  ctx.effect(() => {
    const summary = ctx.webServer.register({
      kind: "exact",
      path: "/api/investment/summary",
      handler: async (req, res) => {
        if (req.method !== "GET" && req.method !== "HEAD") {
          sendJson(res, 405, { ok: false, error: "method not allowed" });
          return;
        }
        try {
          const [status, reports, macro, analysis, analysisRuns] = await Promise.all([
            engineFetch("/api/status").catch((error) => ({ error: error.message })),
            engineFetch("/api/reports?limit=12").catch((error) => ({ error: error.message })),
            engineFetch("/api/macro/latest").catch((error) => ({ error: error.message })),
            engineFetch("/api/analysis/latest").catch((error) => ({ error: error.message })),
            engineFetch("/api/analysis/runs?limit=8").catch((error) => ({ error: error.message })),
          ]);
          const portfolios = {};
          for (const market of ["cn", "hk", "us", "etf"]) {
            portfolios[market] = await engineFetch(`/api/portfolio/${market}`).catch((error) => ({ error: error.message }));
          }
          sendJson(res, 200, {
            ok: true,
            status,
            portfolios,
            reports,
            macro,
            analysis: analysis?.analysis ?? null,
            analysisRuns: analysisRuns?.runs ?? [],
            engine: engineBase(),
          });
        } catch (error) {
          sendJson(res, 500, { ok: false, error: String(error?.message ?? error) });
        }
      },
    });
    const configRoute = ctx.webServer.register({
      kind: "exact",
      path: "/api/investment/config",
      handler: async (req, res) => {
        try {
          if (req.method === "GET") {
            sendJson(res, 200, await engineFetch("/api/config"));
          } else if (req.method === "POST") {
            const body = await readBody(req);
            sendJson(res, 200, await engineFetch("/api/config/update", { method: "POST", body }));
          } else {
            sendJson(res, 405, { ok: false, error: "method not allowed" });
          }
        } catch (error) {
          sendJson(res, 400, { ok: false, error: String(error?.message ?? error) });
        }
      },
    });
    const webhookRoute = ctx.webServer.register({
      kind: "exact",
      path: "/api/investment/webhook",
      handler: async (req, res) => {
        try {
          if (req.method === "GET") {
            // Never leak the stored value back to the browser: only report
            // whether a webhook is configured.
            const resolved = await engineFetch("/api/credentials/resolve?ref=NOTIFY_WEBHOOK_URL");
            sendJson(res, 200, { ok: true, configured: Boolean(resolved.configured), value: "" });
          } else if (req.method === "POST") {
            const body = await readBody(req);
            const value = String(body.value ?? "").trim();
            if (value !== "" && !/^https?:\/\//i.test(value)) {
              sendJson(res, 400, { ok: false, error: "webhook 地址必须以 http(s):// 开头" });
              return;
            }
            const result = await engineFetch("/api/credentials/set", {
              method: "POST",
              body: { ref: "NOTIFY_WEBHOOK_URL", value },
            });
            sendJson(res, 200, { ok: true, configured: Boolean(result.configured) });
          } else {
            sendJson(res, 405, { ok: false, error: "method not allowed" });
          }
        } catch (error) {
          sendJson(res, 400, { ok: false, error: String(error?.message ?? error) });
        }
      },
    });
    const commandRoute = ctx.webServer.register({
      kind: "exact",
      path: "/api/investment/command",
      handler: async (req, res) => {
        try {
          if (req.method !== "POST") {
            sendJson(res, 405, { ok: false, error: "method not allowed" });
            return;
          }
          const body = await readBody(req);
          const result = await engineFetch("/api/commands/issue", {
            method: "POST",
            body: { command: body.command, payload: body.payload ?? {}, requested_by: "product-settings" },
          });
          sendJson(res, 200, result);
        } catch (error) {
          sendJson(res, 400, { ok: false, error: String(error?.message ?? error) });
        }
      },
    });
    return () => {
      summary();
      configRoute();
      webhookRoute();
      commandRoute();
    };
  }, "investment-auto: investment data proxy");
}
