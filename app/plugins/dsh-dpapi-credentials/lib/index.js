/**
 * DPAPI-backed credential provider for Investment Auto 2.0 desktop.
 *
 * Implements the harness `credentials` service (resolve/describe/set/unset +
 * the `credentials/updated` event) by delegating to the engine loopback API,
 * which stores secrets DPAPI-encrypted in the user data root (secrets.enc).
 * No secret ever lands in settings.yaml, .credentials.yaml or process env.
 *
 * The desktop shell applies this provider through a `--patch` overlay
 * (app/profiles/patches/dpapi-credentials.yml), so development keeps the
 * default `.credentials.yaml` provider while the installed product uses
 * DPAPI. Requires the engine process to be running (desktop lifecycle
 * guarantees it; resolution failures surface as missing credentials).
 */

export const name = "@investment-auto/dsh-dpapi-credentials";
export const inject = [];

function makeClient(baseUrl, token) {
  const url = String(baseUrl || "http://127.0.0.1:8790").replace(/\/+$/, "");
  const headers = token ? { "X-IA-Token": token } : {};
  const get = async (path) => {
    const response = await fetch(url + path, { headers });
    if (!response.ok) throw new Error(`engine ${path} -> HTTP ${response.status}`);
    return response.json();
  };
  const post = async (path, payload) => {
    const response = await fetch(url + path, {
      method: "POST",
      headers: { "Content-Type": "application/json", ...headers },
      body: JSON.stringify(payload ?? {}),
    });
    const body = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(`engine ${path} -> HTTP ${response.status}: ${body?.error ?? ""}`);
    return body;
  };
  return { get, post };
}

/** @param {import('@deepseek-ai/cordis').Context} ctx */
export function apply(ctx, config) {
  const engineUrl = process.env.INVESTMENT_ENGINE_URL ?? (typeof config?.engineUrl === "string" ? config.engineUrl : "http://127.0.0.1:8790");
  const token = process.env.IA_ACCESS_TOKEN ?? (typeof config?.token === "string" ? config.token : "");
  const client = makeClient(engineUrl, token);

  const service = {
    async resolve(ref) {
      const key = String(ref);
      try {
        const payload = await client.get(`/api/credentials/resolve?ref=${encodeURIComponent(key)}`);
        if (!payload?.configured || typeof payload.value !== "string" || payload.value.length === 0) return undefined;
        return { value: payload.value, source: "dpapi" };
      } catch {
        return undefined;
      }
    },
    async describe(ref) {
      const key = String(ref);
      try {
        const payload = await client.get(`/api/credentials/resolve?ref=${encodeURIComponent(key)}`);
        return {
          configured: Boolean(payload?.configured),
          ...(payload?.configured ? { source: "dpapi" } : {}),
          writable: true,
        };
      } catch {
        return { configured: false, writable: true };
      }
    },
    async set(ref, value) {
      const key = String(ref);
      await client.post("/api/credentials/set", { ref: key, value: String(value ?? "") });
      ctx.emit("credentials/updated", ref);
    },
    async unset(ref) {
      const key = String(ref);
      await client.post("/api/credentials/unset", { ref: key });
      ctx.emit("credentials/updated", ref);
    },
  };

  ctx.provide("credentials", service);
}
