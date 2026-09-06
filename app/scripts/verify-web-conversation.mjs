/**
 * Web conversation-plane verification driver (P5c automated equivalent).
 *
 * Boots the investment-web profile against a live engine, then drives the
 * SAME wire protocol the browser uses (session.create / session.prompt /
 * session.history unary RPCs) to run one real conversation:
 *
 *   1. create a session -> the response must report the mounted preset
 *      ("investment" — the chat agent composition)
 *   2. prompt a tool task -> the investment tool executes against the
 *      engine and the final assistant answer must reference the result
 *
 * Usage:
 *   node app/scripts/verify-web-conversation.mjs <webUrl> <engineUrl>
 *
 * Exit 0 on success; prints the transcript summary. Requires a configured
 * model key (settings/credentials of the booted profile's DSH home).
 */

const [webUrlRaw, engineUrl] = [process.argv[2], process.argv[3]];
const webUrl = String(webUrlRaw ?? "http://127.0.0.1:4567").replace(/\/+$/, "");

let rpcCounter = 0;
const mintRpcId = () => `verify-${Date.now()}-${rpcCounter++}`;

async function unary(method, payload) {
  const envelope = { type: "client-request", rpcId: mintRpcId(), method, payload };
  const response = await fetch(`${webUrl}/api/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(envelope),
  });
  if (!response.ok) throw new Error(`${method} -> HTTP ${response.status}`);
  const body = await response.json();
  if (body.rpcId !== envelope.rpcId) throw new Error(`${method}: rpcId mismatch`);
  return body.result;
}

async function createSession() {
  const result = await unary("session.create", {});
  if (!result?.ok) throw new Error(`session.create failed: ${JSON.stringify(result)}`);
  return result.value;
}

async function prompt(sessionId, text) {
  const result = await unary("session.prompt", {
    sessionId,
    mode: "queue",
    content: [{ type: "text", text }],
    clientTimeZone: "Asia/Shanghai",
  });
  if (!result?.ok) throw new Error(`session.prompt failed: ${JSON.stringify(result)}`);
  return result.value;
}

async function history(sessionId) {
  const result = await unary("session.history", { sessionId });
  return result?.ok ? result.value : null;
}

function lastAssistantText(historyValue) {
  // Defensive: assistant messages carry `role: "assistant"` with `content`
  // blocks (not a `text` field). Walk for those and return the newest text.
  const texts = [];
  const visit = (node) => {
    if (node === null || typeof node !== "object") return;
    if (Array.isArray(node)) return node.forEach(visit);
    if (node.role === "assistant" && Array.isArray(node.content)) {
      const text = node.content.filter((b) => b?.type === "text").map((b) => b.text).join("");
      if (text) texts.push({ text, seq: node.seq ?? 0 });
    }
    for (const value of Object.values(node)) visit(value);
  };
  visit(historyValue);
  texts.sort((a, b) => a.seq - b.seq);
  return texts.length > 0 ? texts[texts.length - 1].text : "";
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

async function waitForAnswer(sessionId, expectedFragment, timeoutMs = 240000) {
  const deadline = Date.now() + timeoutMs;
  let last = "";
  while (Date.now() < deadline) {
    const value = await history(sessionId);
    last = lastAssistantText(value);
    if (last.includes(expectedFragment)) return last;
    await sleep(1500);
  }
  throw new Error(`answer timeout; last assistant text: ${last.slice(0, 300)}`);
}

async function main() {
  console.error(`verify-web-conversation: web=${webUrl} engine=${engineUrl}`);

  console.error("1) create session…");
  const created = await createSession();
  const sessionId = created?.sessionId;
  if (!sessionId) throw new Error(`session.create returned no sessionId: ${JSON.stringify(created)}`);
  console.error(`   sessionId=${sessionId}`);
  console.error(`   agentPreset=${created.agentPreset ?? "(absent)"}`);
  if (created.agentPreset !== "investment") {
    throw new Error(`expected preset "investment", got ${JSON.stringify(created.agentPreset)}`);
  }

  console.error("2) prompt tool task…");
  const task = "调用 investment_status 工具，然后用中文一句话告诉我当前的 operation_mode，并在句中说明引擎是否紧急停止。";
  await prompt(sessionId, task);
  // The model may rephrase; require the mode value instead of a literal key.
  const answer = await waitForAnswer(sessionId, "automatic");
  console.error(`   answer: ${answer.slice(0, 400)}`);

  console.log("WEB CONVERSATION OK");
  console.log(`preset: ${created.agentPreset}`);
  console.log(`answer: ${answer.slice(0, 500)}`);
}

main().catch((error) => {
  console.error(`VERIFY FAILED: ${error.message}`);
  process.exit(1);
});
