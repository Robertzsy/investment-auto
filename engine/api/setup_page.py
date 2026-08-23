"""First-run setup page served by the engine API (GET /setup).

The desktop shell opens this page before the wizard finished; completing it
writes runtime/setup.complete, after which the shell starts the autonomous
engine and switches the window to the DSH assistant.
"""

SETUP_PAGE_HTML = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>Investment Auto 2.1 — 首次配置</title>
<style>
  body { font-family: "Segoe UI", "Microsoft YaHei", sans-serif; background: #0f1420; color: #e8ecf4;
         display: flex; justify-content: center; padding: 40px 16px; margin: 0; }
  .card { width: 640px; max-width: 100%; background: #171d2b; border: 1px solid #2a3350;
          border-radius: 14px; padding: 32px; box-shadow: 0 8px 40px rgba(0,0,0,.45); }
  h1 { margin: 0 0 6px; font-size: 24px; }
  h1 .v2 { color: #4cc2ff; }
  p.sub { color: #9aa7c7; margin: 0 0 24px; font-size: 14px; }
  label { display: block; font-size: 13px; color: #b8c3e0; margin: 16px 0 6px; }
  input[type=text] { width: 100%; box-sizing: border-box; padding: 10px 12px; border-radius: 8px;
         border: 1px solid #2a3350; background: #0f1420; color: #e8ecf4; font-size: 14px; }
  .hint { color: #6f7d9f; font-size: 12px; margin-top: 6px; }
  button { margin-top: 24px; width: 100%; padding: 12px; border: 0; border-radius: 8px;
         background: #2f6fed; color: #fff; font-size: 15px; cursor: pointer; }
  button:disabled { background: #33405c; cursor: wait; }
  .status { margin-top: 16px; font-size: 13px; min-height: 20px; }
  .status.ok { color: #4cd98a; }
  .status.err { color: #ff7b7b; }
  ul { color: #9aa7c7; font-size: 13px; padding-left: 18px; line-height: 1.8; }
  code { background: #0d1220; padding: 2px 6px; border-radius: 4px; color: #7fd4ff; }
</style>
</head>
<body>
<div class="card">
  <h1>Investment Auto <span class="v2">2.1</span></h1>
  <p class="sub">多市场模拟投资助手 · 首次配置</p>
  <ul>
    <li>初始化四个市场（A股/港股/美股/ETF）的模拟账户，每账户 500,000 初始资金。</li>
    <li>可选择导入旧版（1.x）数据：账户、报告、记忆、选股缓存与策略授权书，导入前自动备份。</li>
    <li>模型与 API Key 在完成本页后，于助手「设置 → 模型」中配置。</li>
    <li>完成配置前不会启动任何自动投资轮次。</li>
  </ul>
  <label for="importFrom">旧版数据目录（可选，留空跳过）</label>
  <input type="text" id="importFrom" placeholder="D:\\investment-auto（自动检测，也可手动填写）">
  <div class="hint">检测到旧版项目时会自动填入；数据只复制、不删除。</div>
  <button id="finish">完成初始化</button>
  <div class="status" id="status"></div>
</div>
<script>
  const params = new URLSearchParams(location.search);
  const token = params.get("token") || "";
  const headers = { "Content-Type": "application/json" };
  if (token) headers["X-IA-Token"] = token;

  async function detectLegacy() {
    try {
      const response = await fetch("/api/setup/status", { headers });
      const payload = await response.json();
      const input = document.getElementById("importFrom");
      if (payload.suggested_source && !input.value) input.value = payload.suggested_source;
      if (payload.first_run === false) {
        setStatus("已完成初始化，正在切换到助手…", true);
        setTimeout(() => location.reload(), 2500);
      }
    } catch (err) {
      setStatus("无法连接初始化服务，请稍后重试。", false);
    }
  }

  function setStatus(text, ok) {
    const el = document.getElementById("status");
    el.textContent = text;
    el.className = "status " + (ok ? "ok" : "err");
  }

  document.getElementById("finish").addEventListener("click", async () => {
    const button = document.getElementById("finish");
    button.disabled = true;
    setStatus("正在初始化…", true);
    try {
      const importFrom = document.getElementById("importFrom").value.trim();
      const response = await fetch("/api/setup/complete", {
        method: "POST", headers,
        body: JSON.stringify({ import_from: importFrom }),
      });
      const payload = await response.json();
      if (!response.ok) throw new Error(payload.error || ("HTTP " + response.status));
      setStatus("初始化完成。桌面窗口即将切换到 AI 助手；模型与 API Key 请在「设置 → 模型」中配置。", true);
      setTimeout(() => location.reload(), 3000);
    } catch (err) {
      setStatus("初始化失败：" + err.message, false);
      button.disabled = false;
    }
  });

  detectLegacy();
</script>
</body>
</html>
"""
