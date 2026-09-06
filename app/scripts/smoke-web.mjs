/**
 * Headless acceptance smoke for the Investment Auto product shell.
 *
 * Launches headless Chrome, loads the investment-web profile, and verifies
 * what the browser actually renders:
 *   1. no visible DSH / DeepSeek / Harness / fish branding
 *   2. product left nav (Dashboard / 投资助手 / 分析流程 / 设置) works
 *   3. the conversation kernel area renders (投资助手 page)
 *   4. Dashboard and Settings pages render with the investment sections
 *   5. no uncaught page exceptions / failed plugin bundles
 *
 * Usage: node app/scripts/smoke-web.mjs <webUrl> [chromePath]
 * Exit 0 on success. No extra npm deps: CDP over Node's built-in WebSocket.
 */
import { spawn } from "node:child_process";
import { mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";

const webUrl = String(process.argv[2] ?? "http://127.0.0.1:4567").replace(/\/+$/, "");
const chromePath =
  process.argv[3] ??
  "C:\\Program Files\\Google\\Chrome\\Application\\chrome.exe";
const cdpPort = 9333;

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

class Cdp {
  constructor(ws) {
    this.ws = ws;
    this.nextId = 1;
    this.pending = new Map();
    this.events = [];
    ws.onmessage = (event) => {
      const message = JSON.parse(event.data);
      if (message.id !== undefined) {
        const entry = this.pending.get(message.id);
        if (entry) {
          this.pending.delete(message.id);
          message.error ? entry.reject(new Error(message.error.message)) : entry.resolve(message.result);
        }
      } else {
        this.events.push(message);
      }
    };
  }
  call(method, params = {}) {
    const id = this.nextId++;
    return new Promise((resolve, reject) => {
      this.pending.set(id, { resolve, reject });
      this.ws.send(JSON.stringify({ id, method, params }));
    });
  }
  close() {
    try {
      this.ws.close();
    } catch {}
  }
}

function connect(wsUrl) {
  return new Promise((resolve, reject) => {
    const ws = new WebSocket(wsUrl);
    const timer = setTimeout(() => reject(new Error("cdp connect timeout")), 15000);
    ws.onopen = () => {
      clearTimeout(timer);
      resolve(new Cdp(ws));
    };
    ws.onerror = () => {
      clearTimeout(timer);
      reject(new Error("cdp connect error"));
    };
  });
}

async function jsonGet(url) {
  const response = await fetch(url);
  if (!response.ok) throw new Error(`GET ${url} -> ${response.status}`);
  return response.json();
}

async function waitFor(predicate, what, timeoutMs = 20000) {
  const deadline = Date.now() + timeoutMs;
  while (Date.now() < deadline) {
    const value = await predicate();
    if (value) return value;
    await sleep(300);
  }
  throw new Error(`timeout waiting for ${what}`);
}

const failures = [];
const check = (ok, message) => {
  console.error(`  ${ok ? "PASS" : "FAIL"}  ${message}`);
  if (!ok) failures.push(message);
};

async function evaluate(cdp, expression) {
  const result = await cdp.call("Runtime.evaluate", {
    expression,
    returnByValue: true,
    awaitPromise: true,
  });
  if (result.exceptionDetails) {
    throw new Error(`evaluate failed [${expression.slice(0, 120)}]: ${result.exceptionDetails.exception?.description ?? result.exceptionDetails.text ?? "exception"}`);
  }
  return result.result?.value;
}

async function main() {
  console.error(`smoke-web: url=${webUrl}`);
  const profileDir = mkdtempSync(join(tmpdir(), "ia-smoke-"));

  const chrome = spawn(
    chromePath,
    [
      "--headless=new",
      `--remote-debugging-port=${cdpPort}`,
      `--user-data-dir=${profileDir}`,
      "--no-first-run",
      "--no-default-browser-check",
      "--disable-gpu",
      "--disable-extensions",
      "about:blank",
    ],
    { stdio: "ignore" },
  );
  const chromeExit = new Promise((resolve) => chrome.on("exit", resolve));

  try {
    const version = await waitFor(async () => {
      try {
        return await jsonGet(`http://127.0.0.1:${cdpPort}/json/version`);
      } catch {
        return null;
      }
    }, "chrome devtools endpoint");

    const target = await (
      await fetch(`http://127.0.0.1:${cdpPort}/json/new?${encodeURIComponent(webUrl + "/")}`, { method: "PUT" })
    ).json();
    const cdp = await connect(target.webSocketDebuggerUrl);
    await cdp.call("Runtime.enable");
    await cdp.call("Page.enable");
    await cdp.call("Log.enable");
    await cdp.call("Network.enable");

    // Collect failed requests while the page boots.
    await cdp.call("Page.navigate", { url: webUrl + "/" });
    await sleep(12000);

    const errors = cdp.events.filter(
      (e) => e.method === "Runtime.exceptionThrown" || e.method === "Log.entryAdded",
    );
    const failedRequests = cdp.events.filter(
      (e) => e.method === "Network.responseReceived" && e.params.response.status >= 400,
    );
    for (const req of failedRequests.slice(0, 10)) {
      console.error(`  HTTP ${req.params.response.status}  ${req.params.response.url}`);
    }

    // 1. Branding: page title and visible text.
    const title = await evaluate(cdp, "document.title");
    check(String(title).includes("Investment Auto"), `page title is Investment Auto (got: ${title})`);
    check(!String(title).includes("Harness"), `page title has no Harness (got: ${title})`);

    const visibleText = await evaluate(cdp, "document.body ? document.body.innerText : ''");
    const hasBrand = /deepseek|harness/i.test(String(visibleText));
    check(!hasBrand, "no DeepSeek/Harness visible in page text");

    // 2. Product left navigation.
    const navText = await evaluate(
      cdp,
      `Array.from(document.querySelectorAll('.ia-navbtn')).map(b => b.textContent.trim()).join('|')`,
    );
    check(String(navText).includes("Dashboard") && String(navText).includes("投资助手") && String(navText).includes("分析流程") && String(navText).includes("设置"), `left nav renders (${navText})`);
    check(!String(navText).includes("账户"), `left nav has no account entry (${navText})`);
    const brandText = await evaluate(cdp, `document.querySelector('.ia-rail-brand') ? document.querySelector('.ia-rail-brand').getAttribute('title') : ''`);
    check(String(brandText).includes("Investment Auto"), `product brand renders (${brandText})`);

    // 3. Conversation kernel (投资助手 default page): sidebar + conversation area.
    const sidebarText = await evaluate(cdp, `document.querySelector('.ia-sidebar') ? document.querySelector('.ia-sidebar').innerText.slice(0, 200) : ''`);
    check(String(sidebarText).includes("会话"), `assistant page shows session sidebar (${sidebarText.slice(0, 60).replace(/\\n/g, ' ')})`);
    const centerHtml = await evaluate(cdp, `document.querySelector('.ia-center') ? document.querySelector('.ia-center').innerHTML.length : 0`);
    check(Number(centerHtml) > 500, `conversation kernel mounted (${centerHtml} bytes of DOM)`);
    const contextText = await evaluate(cdp, `document.querySelector('.ia-context') ? document.querySelector('.ia-context').innerText : ''`);
    check(String(contextText).includes("投资上下文") && String(contextText).includes("分析流程"), "assistant page shows investment context");
    const residualSelectorsVisible = await evaluate(cdp, `Array.from(document.querySelectorAll('[aria-label="选择工作区"],[aria-label^="访问模式"]')).some(el => getComputedStyle(el).display !== 'none')`);
    check(!residualSelectorsVisible, "workspace and access-mode selectors are hidden");
    const composerPlaceholder = await evaluate(cdp, `document.querySelector('.ia-center textarea')?.placeholder ?? ''`);
    check(String(composerPlaceholder).includes("投资问题"), `conversation composer uses investment copy (${composerPlaceholder})`);
    check(!/workspace|工作区管理/i.test(String(visibleText)), "no workspace management UI in visible text");

    // 4. Dashboard page.
    await evaluate(cdp, `[...document.querySelectorAll('.ia-navbtn')].find(b => b.textContent.includes('Dashboard')).click()`);
    await sleep(1500);
    const dashText = await evaluate(cdp, "document.body.innerText");
    check(String(dashText).includes("投资总览"), "Dashboard renders 投资总览");
    check(String(dashText).includes("四市场账户"), "Dashboard renders 四市场账户");

    // 4b. Analysis workflow page.
    await evaluate(cdp, `[...document.querySelectorAll('.ia-navbtn')].find(b => b.textContent.includes('分析流程')).click()`);
    await sleep(1000);
    const workflowText = await evaluate(cdp, "document.body.innerText");
    check(String(workflowText).includes("目标完整分析链路"), "analysis page renders the target workflow");
    check(String(workflowText).includes("当前分析状态") && String(workflowText).includes("流程验收"), "analysis page renders real workflow telemetry");
    check(String(workflowText).includes("原生多角色") && !String(workflowText).includes("待恢复"), "analysis page presents the restored checkpointed workflow");

    // 5. Settings page.
    await evaluate(cdp, `[...document.querySelectorAll('.ia-navbtn')].find(b => b.textContent.includes('设置')).click()`);
    await sleep(1500);
    const settingsNav = await evaluate(
      cdp,
      `Array.from(document.querySelectorAll('.ia-settings-nav button')).map(b => b.textContent.trim()).join('|')`,
    );
    check(
      String(settingsNav).includes("投资策略与风控") && String(settingsNav).includes("自动轮次与调度") && String(settingsNav).includes("市场与选股") && String(settingsNav).includes("通知") && String(settingsNav).includes("应用设置"),
      `settings nav has all five product sections (${settingsNav})`,
    );
    check(String(settingsNav).includes("模型"), `settings nav keeps the model section (${settingsNav})`);
    const strategyShown = await evaluate(cdp, `document.body.innerText.includes('策略档位') || document.body.innerText.includes('投资策略与风控')`);
    check(Boolean(strategyShown), "settings page renders the strategy section content");

    // 5b. Model page (embedded conversation kernel's own models surface).
    await evaluate(cdp, `[...document.querySelectorAll('.ia-settings-nav button')].find(b => b.textContent.includes('模型')).click()`);
    const modelReady = await waitFor(async () => {
      const text = await evaluate(cdp, "document.body.innerText");
      return String(text).includes("添加提供方") ? text : null;
    }, "model settings page content", 10000);
    check(
      String(modelReady).includes("添加提供方") && String(modelReady).includes("DeepSeek"),
      `model settings page renders provider management (${modelReady.replace(/\\n/g, " ").slice(0, 120)})`,
    );
    check(!/harness|dsh\b/i.test(String(modelReady)), "model settings page has no harness/DSH strings");

    // 6. Back to assistant still renders conversation.
    await evaluate(cdp, `[...document.querySelectorAll('.ia-navbtn')].find(b => b.textContent.includes('投资助手')).click()`);
    await sleep(1200);
    const centerAgain = await evaluate(cdp, `document.querySelector('.ia-center') ? document.querySelector('.ia-center').innerHTML.length : 0`);
    check(Number(centerAgain) > 500, "assistant page restores conversation kernel");

    // 6b. Session search must map the wire shape {items:[{sessionId,snippet}]}.
    await evaluate(cdp, `(() => {
      const el = document.querySelector('.ia-search');
      if (!el) return 'no-search-box';
      const setter = Object.getOwnPropertyDescriptor(window.HTMLInputElement.prototype, 'value').set;
      setter.call(el, '投资');
      el.dispatchEvent(new Event('input', { bubbles: true }));
      return 'typed';
    })()`);
    const searchHit = await waitFor(async () => {
      const text = await evaluate(cdp, "document.body.innerText");
      const count = await evaluate(cdp, "document.querySelectorAll('.ia-session').length");
      return String(text).includes("搜索结果") && Number(count) >= 1 ? text : null;
    }, "session search results", 8000);
    check(Boolean(searchHit), "session search returns matching sessions (items mapping)");

    // 6c. The archive affordance exists on the flat session rows (the
    // archivedSessionIds filter itself is exercised through the store shape;
    // the archive flow opens a host confirmation dialog that headless CDP
    // cannot drive, so the click outcome is not asserted here).
    const archiveButton = await evaluate(cdp, `(() => {
      const row = document.querySelector('.ia-session');
      if (!row) return 'no-session';
      row.dispatchEvent(new MouseEvent('mouseover', { bubbles: true }));
      return row.querySelector('.ia-session-actions button') ? 'present' : 'missing';
    })()`);
    check(String(archiveButton) === "present", "session rows carry the archive affordance");

    // 7. No plugin bundle failures and no runtime exceptions.
    check(errors.length === 0, `zero page exceptions (${errors.length})`);
    if (errors.length > 0) {
      for (const error of errors.slice(0, 5)) {
        console.error("   " + JSON.stringify(error).slice(0, 400));
      }
    }
    check(!failedRequests.some((r) => String(r.params.response.url).includes("client.js") && r.params.response.status === 404), "all client bundles served (no 404)");

    cdp.close();
  } finally {
    try {
      chrome.kill();
    } catch {
      // Chrome already exited; nothing to kill.
    }
    await Promise.race([chromeExit, sleep(3000)]);
  }

  if (failures.length > 0) {
    console.error(`SMOKE FAILED (${failures.length} failures)`);
    process.exit(1);
  }
  console.log("SMOKE OK");
}

main().catch((error) => {
  console.error(`SMOKE FAILED: ${error.message}`);
  process.exit(1);
});
