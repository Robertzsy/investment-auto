/** Minimal JSON client for the engine loopback API. */
export class EngineClient {
  /** @param {string} baseUrl engine API base, e.g. http://127.0.0.1:8790 */
  /** @param {string} token optional X-IA-Token value */
  constructor(baseUrl, token = "") {
    this.baseUrl = baseUrl.replace(/\/+$/, "");
    this.token = token;
  }

  async get(path, { timeoutMs = 30000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(this.baseUrl + path, {
        signal: controller.signal,
        headers: this.token ? { "X-IA-Token": this.token } : {},
      });
      const payload = await response.json();
      if (!response.ok) {
        throw new Error(`engine ${path} -> HTTP ${response.status}: ${payload?.error ?? JSON.stringify(payload)}`);
      }
      return payload;
    } finally {
      clearTimeout(timer);
    }
  }

  async issue(command, payload = {}, { requestedBy = "dsh-tools", timeoutMs = 120000 } = {}) {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), timeoutMs);
    try {
      const response = await fetch(this.baseUrl + "/api/commands/issue", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          ...(this.token ? { "X-IA-Token": this.token } : {}),
        },
        body: JSON.stringify({ command, payload, requested_by: requestedBy }),
        signal: controller.signal,
      });
      const body = await response.json();
      if (!response.ok) {
        throw new Error(`engine command ${command} -> HTTP ${response.status}: ${body?.error ?? JSON.stringify(body)}`);
      }
      return body;
    } finally {
      clearTimeout(timer);
    }
  }
}
