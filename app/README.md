# Investment Auto 2.0 — DSH 应用侧

本目录是 2.0 的 DeepSeek Harness 应用壳：

| 路径 | 内容 |
|---|---|
| `package.json` | 锁定 `@deepseek-ai/dsh` 版本；桌面安装包随行打包 `node_modules/` 实现离线 |
| `profiles/investment-web/` | 桌面对话 profile：`dsh-base` + `dsh-web-app` + 投资 patch（persona、默认 preset、遥测关闭） |
| `profiles/investment/` | 无头 profile：`dsh-base` + `dsh-headless`，供引擎调度器驱动自主投资轮次（P3） |
| `presets/investment/` | 投资 agent preset：投资 persona，Skills/plan/goal/子代理/工作流/web 搜索，无 Shell/编码工具 |
| `skills/` | 投资 Skills（SKILL.md 格式，P2 内容） |
| `scripts/seed.ps1` | 把 profiles/presets/skills 播种进 `$DSH_HOME`（开发与安装版共用；已存在不覆盖，`-Force` 刷新） |
| `scripts/dev.ps1` | 开发启动器：DSH_HOME=app/dev-home，安装依赖、播种、启动 investment-web |

用户数据（会话、设置、凭据、Skills、agent presets）全部位于 `$DSH_HOME`；
安装版为 `%LocalAppData%\InvestmentAuto`，开发版为 `app/dev-home`（gitignored）。

## 开发

```powershell
.\app\scripts\dev.ps1 -Port 4567
```

打开 http://127.0.0.1:4567 。首次启动在「设置 → 模型」中配置模型与 API Key
（写入 `$DSH_HOME/settings.yaml` + `.credentials.yaml`）。

## 检查组合后的配置（不启动）

```powershell
$env:DSH_HOME = 'D:\investment-auto\app\dev-home'
node app\node_modules\@deepseek-ai\dsh\lib\bin.js --profile investment-web --dump-config
```
