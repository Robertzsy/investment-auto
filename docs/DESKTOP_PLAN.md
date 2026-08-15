# Investment Auto 桌面应用改造实施计划

> 目标：普通用户安装后双击桌面「Investment Auto」图标，在独立桌面窗口内使用整个系统，全程不需要浏览器、CMD、PowerShell、Python、Node。

## 0. 现状调研结论（已核实）

1. **launcher 已存在且成熟**：`windows/InvestmentAutoLauncher.cs` 是 WinForms 启动器（启动/停止/开机自启/状态检测），但用旧 CodeDom 编译（拿不到 WebView2），且最后 `OpenDashboard()` 跳外部浏览器。
2. **路径依赖是最大改造点**：约 **36 处** `ROOT / "runtime"`（用户数据：审计/账户/记忆/checkpoint/报告/选股缓存/bus/mandate/锁）+ 3 处 `ROOT / "scripts"`（代码）+ 5 处 `ROOT / "config"`（用户配置），其中 `ROOT = Path(__file__).resolve().parents[2]` 分布在 ~25 个模块。**代码和数据耦合在同一个根**。
3. **端口已支持环境变量**：`CHAT_PORT`（main.py）、`CHAT_HOST`（默认 localhost）已存在；launcher 可用动态端口传给 python。
4. **进程锁已具备**：`scheduler.lock` + `runtime_lock.py` 的 ProcessLease/atomic_claim 已防重复调度器；launcher 读 `worker.json` 判 agent 存活。
5. **健康检查已具备**：`GET /api/history` 被 launcher 用于判 chat 存活。
6. **打包链已具备但过时**：`build-windows-release.ps1`（CodeDom 编译 launcher + 拷 src + 压 zip）、`Setup-Windows.cmd`（调 PS）。无 Inno Setup、无运行时捆绑、无数据分离。
7. **依赖**：Python 3.10+（.venv 3.11.7）、Node 18+、pandas/numpy/pydantic-ai-slim/pymongo/openai。numpy/pandas 有 embeddable wheel，其余为纯 wheel，**可离线安装**（需验证）。

## 1. 核心架构决策

### A. 统一路径（AppPaths）—— P0 关键

新增 `src/paths.py`，全项目唯一路径权威：

```python
ROOT = Path(__file__).resolve().parents[2]        # 代码根（安装目录，只读）
def data_root() -> Path:                            # 用户数据根
    env = os.getenv("INVESTMENT_AUTO_DATA_DIR")
    return Path(env) if env else ROOT
```

| 路径类别 | 归属 | 桌面安装位置 |
|---|---|---|
| 代码/静态 | `ROOT/src`、`ROOT/scripts`、`ROOT/config/market`（模板） | `%LocalAppData%\Programs\InvestmentAuto\` |
| 用户数据 | `data_root()/runtime`、`data_root()/config`、`data_root()/data`、`data_root()/logs`、`data_root()/workspace` | `%LocalAppData%\InvestmentAuto\` |

- 所有 `ROOT / "runtime"` → `data_root() / "runtime"`（36 处机械替换，各模块改为从 `src.paths` import）
- `ROOT / "scripts"` 保持代码根（stock-fetcher.js、macro run.js）
- `config.yaml` 位置：默认 `data_root()/config/config.yaml`，`CONFIG_PATH` 仍可覆盖；`market/*.yaml` 首次从代码根模板拷到数据目录，之后读写数据目录
- `.env` 位置：`data_root()/.env`（开发模式 = ROOT/.env 不变）
- **开发模式零改动**：不设 `INVESTMENT_AUTO_DATA_DIR` 时 `data_root()==ROOT`，D:\investment-auto 直接跑行为完全不变

### B. 进程模型 —— P0/P1

- launcher（WPF）用 `pythonw.exe` 启动：`-m src.main run`（投资 Agent+调度器）、`-m src.main chat`（对话服务），`CreateNoWindow`
- 动态端口：launcher 选可用回环端口 → `CHAT_PORT` env 传给 chat；health check 就绪后再让 WebView 加载
- 单实例：命名 Mutex（重复双击激活已有窗口）
- 子进程生命周期：**Windows Job Object**（JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE）——退出时自动杀全部后台 python/node，无孤儿进程
- 防重复调度器：复用现有 scheduler.lock + worker.json 检测；launcher 启动前先查

### C. 桌面窗口 —— P1

- .NET 8 WPF 外壳 + WebView2（加载 `http://127.0.0.1:<port>`，不跳浏览器）
- 导航：仪表盘 / AI 对话 / 报告 / 持仓 / 设置（复用现有 HTML，通过页面内导航/锚点）
- 底部状态栏：agent 状态、chat 状态、手动/自动模式、策略档位、最近轮次、下一轮次（从现有 `runtime_status` API + worker.json 拉取）
- 关闭弹选择：最小化到托盘（继续后台）/ 停止并退出
- 托盘菜单：打开主窗口、启动服务、暂停投资、查看日志、退出

### D. 嵌入式运行时 —— P2

- Python：python.org **embeddable x64**（3.11）+ `get-pip.py` + 离线 wheel（`pip download -r requirements-lock.txt` 预下载到 `wheels/`）
- Node：nodejs.org 官方 Windows 便携 zip（解压即用）
- WebView2：Evergreen bootstrapper（`MicrosoftEdgeWebview2Setup.exe`，~2MB，仅无运行时才触发）
- **不用 PyInstaller**（项目需要动态导入/自建工具/改代码/运行时 Skill，指示已明确）

### E. 安装器 —— P3

- Inno Setup 6，每用户安装（`PrivilegesRequired=lowest`），无管理员、无 PowerShell
- 安装目录 `%LocalAppData%\Programs\InvestmentAuto`；数据目录 `%LocalAppData%\InvestmentAuto`
- `[Files]`：代码 + 运行时 + 依赖；`[Icons]`：桌面 + 开始菜单；`[Run]`：SW_HIDE 静默 pip 安装 + 首次初始化
- 卸载：询问是否保留数据（默认保留模拟账户/报告/配置/API）

### F. 首次向导 + 密钥 —— P4

- 窗口内 WebView 加载 `setup.html`（新页面），十步：模型商/API Key 测试/快慢模型/策略/模式/市场/风控确认/webhook/初始化账户/启动
- API Key 用 **DPAPI**（`CryptProtectData`，Windows 内置，ctypes 调用，无需额外依赖）加密存 `data_root()/secrets.enc`，不写安装目录、不进日志、不进 .env 明文
- 首次检测 `D:\investment-auto` → 允许导入 config/.env/runtime/账户/报告/记忆，导入前备份，不删原目录

## 2. 阶段计划（每阶段可回退提交 + 测试）

### P0 路径与进程契约（无 UI 改动，先立根基）

| 项 | 内容 |
|---|---|
| 新增 | `src/paths.py`（app_root/data_root + 各子目录 helper） |
| 修改 | ~25 个模块：`ROOT/"runtime"`→`paths.data_root()/"runtime"`；config.py 的 .env/config/market 加载改走 paths；ui/server.py+chat_server.py 的 PROJECT_ROOT 拆分 |
| 修改 | main.py：chat 端口支持任意回环端口（已支持）、新增 `--setup` 或 setup 服务端点（供向导） |
| 测试 | ①路径分离（设 env 后 runtime 落到数据目录、scripts 仍在代码根）；②开发模式无 env 时行为不变（跑现有全量测试）；③config/market 从数据目录读 |

### P1 WPF/WebView2 桌面窗口

| 项 | 内容 |
|---|---|
| 新增 | `windows/desktop/`：.NET 8 WPF 项目（App.xaml、MainWindow.xaml、WebView2、ProcessManager、TrayIcon、SingleInstance、HealthCheck、StatusBar） |
| 新增 | `scripts/build-desktop.ps1`（`dotnet build` 替代 CodeDom，引入 Microsoft.Web.WebView2） |
| 修改 | main.py/chat 健康端点（若有缺口，补一个轻量 `/api/health`） |
| 测试 | ③子进程启动/健康/停止；⑤窗口关闭后托盘后台续跑；⑥退出无孤儿进程（Job Object）；②单实例重复双击激活；⑫WebView2 内部导航（不跳外部浏览器） |

### P2 嵌入式 Python/Node

| 项 | 内容 |
|---|---|
| 新增 | `scripts/fetch-embeddable-runtime.ps1`（下载 embeddable python + node + webview2 bootstrapper） |
| 新增 | `scripts/bundle-runtime.ps1`（注入 pip、离线安装依赖到 `runtime-python/`） |
| 修改 | build-desktop 集成运行时拷贝 |
| 测试 | ④离线 wheel 安装后 pandas/numpy/pydantic-ai/pymongo 可 import；⑩干净环境无 Python/Node 可启动（容器/VM 验证） |

### P3 安装器、快捷方式、托盘、自启动

| 项 | 内容 |
|---|---|
| 新增 | `installer/InvestmentAuto.iss`（每用户、图标、卸载保留数据、[Run] 静默 pip） |
| 修改 | launcher 托盘 + 开机自启（复用现有 Run 键逻辑，改为 WPF 托盘） |
| 测试 | ⑪桌面快捷方式启动；⑧升级保留配置/账户；⑦旧 D 盘数据迁移；⑨ API Key 不进日志/安装目录 |

### P4 首次向导 + 旧数据迁移

| 项 | 内容 |
|---|---|
| 新增 | `src/ui/setup.html`（十步向导 UI）+ `src/secret_store.py`（DPAPI 封装） |
| 新增 | setup 服务端点（读写加密密钥、初始化账户、测试连接） |
| 修改 | config.py 的 `llm_api_key` 优先从 secret_store 读 |
| 测试 | ⑨密钥加密不落盘明文；⑦D 盘导入备份；向导各步 |

### P5 测试、文档、发行候选

| 项 | 内容 |
|---|---|
| 新增 | 中英文安装文档、CHANGELOG、`InvestmentAuto-Setup-x64.exe` + `Portable-x64.zip` + SHA-256 |
| 验证 | 干净 Win10/11 x64 VM 全流程：安装→向导→桌面窗口→模拟分析→报告→托盘→重启→自动恢复→卸载 |
| 回归 | ⑭现有 Python 全量测试全部通过；开发启动方式 `python -m src.main run/chat`、Docker 不变 |

## 3. 预计修改/新增文件清单

**新增（约 20 个）**：`src/paths.py`、`src/secret_store.py`、`src/ui/setup.html`、`windows/desktop/`（WPF 项目 ~8 文件）、`installer/InvestmentAuto.iss`、`scripts/build-desktop.ps1`、`scripts/fetch-embeddable-runtime.ps1`、`scripts/bundle-runtime.ps1`、文档 ×2。

**修改（约 30 个）**：`src/config.py`、`src/main.py` + ~25 个 `ROOT/"runtime"` 模块（改为 `paths.data_root()`）；`scripts/build-windows-release.ps1`（保留旧链直到验收）。

## 4. 关键风险与对策

| 风险 | 对策 |
|---|---|
| embeddable Python 缺 pip/venv、`._pth` 坑 | 用官方 embeddable + get-pip 注入；或退化为"复制完整安装版 Python"（winpython）。P2 单独验证 numpy/pandas 离线 import |
| WebView2 Runtime 缺失 | bootstrapper 兜底；启动前检测，缺失时引导安装 |
| 端口冲突 | launcher 选可用回环端口 → CHAT_PORT 传递（已支持） |
| 数据分离后旧路径遗漏 | 全量测试 + P0 的 env 路径测试；grep 复核 `ROOT/"(runtime|data|config|logs)"` 清零 |
| 新旧启动重复调度器 | scheduler.lock + worker.json 检测；launcher 启动前判活 |
| DPAPI 用户上下文绑定 | 每用户安装（PrivilegesRequired=lowest），DPAPI 与用户一致 |
| CodeDom 编译 WebView2 失败 | 迁移 dotnet build + NuGet WebView2（P1 第一步先验证能编译） |

## 5. 验收门槛

全部 14 项测试通过 + 干净 VM 全流程通过，才生成正式 `InvestmentAuto-Setup-x64.exe`。旧 `InvestmentAuto.exe` 与 build-windows-release.ps1 保留到新版验收，GitHub 现有 EXE Release 不动。
