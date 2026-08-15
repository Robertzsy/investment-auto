# Investment Auto 桌面版 - 使用与验收文档

## 一、构建产物

`release\InvestmentAuto-Setup-x64.exe` - 每用户安装包（约 100-200MB，内含：

- InvestmentAuto.Desktop.exe（.NET 8 自包含 WPF + WebView2 外壳）
- 完整可迁移 Python 3.11 运行时（全部锁定依赖离线预装）
- 便携版 Node.js 20
- 项目源码与静态页面
- WebView2 Evergreen 安装器（系统缺少运行时才会触发）

## 二、用户安装与使用

1. 双击 `InvestmentAuto-Setup-x64.exe`，全程图形向导（无需管理员、无需 PowerShell、无需预装 Python/Node）
2. 安装完成自动创建桌面与开始菜单「Investment Auto」快捷方式
3. 双击图标 → 独立桌面窗口打开（不启动任何外部浏览器）
4. 首次启动显示 10 步配置向导：旧数据导入 → API Key（DPAPI 加密）→ 模型 → 策略 → 模式 → 市场 → 风控确认 → 通知 → 初始化账户 → 完成
5. 之后每次双击直接进入应用主界面；点窗口 X 会弹出三选一：最小化到托盘（后台继续）/ 停止全部服务并退出 / 取消
6. 托盘菜单：打开主窗口 / 启动服务 / 暂停投资 / 查看日志 / 开机自启（勾选开关）/ 退出
7. 登录 Windows 后自动启动（托盘静默运行）：托盘菜单「开机自启」勾选后生效，写入 HKCU Run 键，无需管理员

### 目录布局

| 内容 | 位置 |
|---|---|
| 程序文件（只读） | `%LocalAppData%\Programs\InvestmentAuto` |
| 用户数据（配置/账户/报告/记忆/自建工具） | `%LocalAppData%\InvestmentAuto` |
| 加密密钥 | `%LocalAppData%\InvestmentAuto\secrets.enc`（DPAPI） |

升级覆盖程序文件、保留全部用户数据；卸载默认保留数据。

## 三、开发模式（不变）

```bash
python -m src.main run    # 投资 Agent + 调度器
python -m src.main chat   # 管理对话（浏览器开发模式）
docker compose up -d      # Docker 部署
```

开发模式不设 `INVESTMENT_AUTO_DATA_DIR` 时数据根=代码根，行为与桌面化改造前完全一致。

## 四、构建流程（开发人员）

```powershell
# 1. 下载运行时组件（python nupkg + node zip + webview2）
powershell scripts\fetch-runtime.ps1
# 2. 解压并离线安装依赖到捆绑 python
powershell scripts\bundle-runtime.ps1
# 3. 编译桌面外壳（需要 .NET 8 SDK）
powershell scripts\build-desktop.ps1
# 4. 编译安装器（需要 Inno Setup 6）
& "$env:LOCALAPPDATA\Programs\Inno Setup 6\ISCC.exe" installer\InvestmentAuto.iss
# 5. 校验
Get-FileHash release\InvestmentAuto-Setup-x64.exe -Algorithm SHA256
```

## 五、干净 Windows 虚拟机验收清单

在全新 Win10/11 x64 虚拟机（无 Python/Node/.NET/Inno）上执行：

1. [ ] 复制安装包运行，图形向导完成安装（无 UAC 弹窗、无 PowerShell 窗口）
2. [ ] 桌面与开始菜单出现快捷方式
3. [ ] 双击快捷方式：弹出 Investment Auto 独立窗口，不出现 Chrome/Edge/控制台
4. [ ] 首次向导 10 步走完（旧数据导入项显示"未检测到"）
5. [ ] 填写 API Key 后密钥加密文件生成、明文不可见
6. [ ] 发起一次模拟分析，窗口内查看报告
7. [ ] 关闭窗口 → 弹出三选一 → 选「最小化到托盘」→ 托盘图标存在、后台继续；选「停止并退出」→ 窗口、托盘、pythonw 全部消失；「取消」→ 窗口保持
8. [ ] 重启虚拟机重新登录 → 托盘自动出现（若开了自启）→ 双击恢复窗口
9. [ ] 卸载：数据保留询问 → 重装：配置/账户仍在
10. [ ] 任务管理器无孤儿 python/pythonw/node 进程

以上全部通过后，可发布正式版并更新 GitHub Release。
