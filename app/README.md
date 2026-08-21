# Investment Auto 2.0 — DSH 应用侧

本目录是 2.0 的 DeepSeek Harness 应用壳：

| 路径 | 内容 |
|---|---|
| `package.json` + `package-lock.json` | 锁定 `@deepseek-ai/dsh@0.1.0-rc.6` 完整依赖树（npm 默认会把 `^0.1.0-rc.6` 解析成混装 rc.8，必须用 lockfile 精确复刻）；桌面安装包随行打包 `node_modules/` |
| `profiles/investment-web/` | 桌面对话 profile：`dsh-base` + `dsh-web-app` + 投资 patch（persona、默认 preset、遥测关闭、工具桥行） |
| `profiles/investment/` | 无头 profile：`dsh-base` + `dsh-headless` + 投资 patch；shell/文件/编码工具与 plan 模式已禁用，供引擎调度器驱动自主轮次 |
| `profiles/patches/dpapi-credentials.yml` | 桌面专用 `--patch` overlay：凭据换成 DPAPI 存储（仅安装版应用） |
| `presets/investment/` | 投资 agent preset：投资 persona，Skills/plan/goal/子代理/工作流/web 搜索，无 Shell/编码工具 |
| `skills/` | 7 个投资 Skills（SKILL.md）：security-analysis、market-overview、stock-screening、portfolio-review、portfolio-optimization、complete-investment-cycle、account-management |
| `plugins/dsh-investment-tools/` | 引擎桥工具插件：16 个 `investment_*` 工具（只读事实 + 纸面边界写操作），`ctx.tools.register` 注册 |
| `plugins/dsh-dpapi-credentials/` | DPAPI 凭据 provider：实现 harness `credentials` 服务，密钥经引擎 DPAPI 加密存储 |
| `plugins/dsh-investment-ui/` | 客户端 UI 插件：`tool.call.toolview` 键控卡片（investment_status / portfolio / mandate） |
| `scripts/seed.ps1` | 把 profiles/presets/skills/plugins 播种进 `$DSH_HOME`（开发与安装版共用；已存在不覆盖，`-Force` 刷新） |
| `scripts/dev.ps1` | 开发启动器：DSH_HOME=app/dev-home，安装依赖、播种、启动 investment-web |
| `scripts/check-skills.mjs` | 技能结构校验 + 被引用工具与插件注册表一致性 |

用户数据（会话、设置、凭据、Skills、agent presets）全部位于 `$DSH_HOME`；
安装版为 `%LocalAppData%\InvestmentAuto`（引擎启动时自动播种），开发版为
`app/dev-home`（gitignored）。

## 开发

```powershell
# 终端 1：投资引擎（工具桥依赖它）
.\.venv\Scripts\python.exe -m engine.main serve

# 终端 2：DSH 助手（首次自动 npm ci + 播种）
.\app\scripts\dev.ps1 -Port 4567
```

打开 http://127.0.0.1:4567 ，在「设置 → 模型」配置模型与 API Key
（开发模式写入 `app/dev-home/settings.yaml` 与 `.credentials.yaml`）。

## 检查组合后的配置（不启动）

```powershell
$env:DSH_HOME = 'D:\investment-auto\app\dev-home'
node app\node_modules\@deepseek-ai\dsh\lib\bin.js --profile investment-web --dump-config
node app\node_modules\@deepseek-ai\dsh\lib\bin.js --profile investment --dump-config
```

## 测试

```powershell
node --test "app/plugins/dsh-investment-tools/test/*.test.mjs" "app/plugins/dsh-dpapi-credentials/test/*.test.mjs"
node app/scripts/check-skills.mjs
```
