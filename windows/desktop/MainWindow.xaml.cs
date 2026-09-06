using System;
using System.ComponentModel;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Threading;
using InvestmentAuto.Desktop.Services;
using Microsoft.Web.WebView2.Core;
using Application = System.Windows.Application;
using MessageBox = System.Windows.MessageBox;

namespace InvestmentAuto.Desktop;

public partial class MainWindow : Window
{
    private readonly ProcessManager _processManager;
    private TrayIcon? _tray;
    private DispatcherTimer? _statusTimer;
    private bool _closingViaTray;
    private ChatReady? _ready;

    internal MainWindow(ProcessManager processManager)
    {
        InitializeComponent();
        _processManager = processManager;
        _tray = new TrayIcon(
            onOpen: () => RestoreFromTray(),
            onStart: () => _ = Task.Run(() => _processManager.StartPublicServices()),
            onPause: () => _ = Task.Run(PauseInvestmentAsync),
            onViewLogs: () => OpenLogs(),
            isAutoStartEnabled: () => AutoStart.IsEnabled(),
            setAutoStart: ToggleAutoStart,
            onExit: () => Dispatcher.Invoke(ExitFully));
    }

    public async Task StartAsync()
    {
        LogLine("start: ensuring services...");
        var ready = await _processManager.EnsureRunningAsync(new System.Threading.CancellationToken());
        _ready = ready;
        LogLine("start: services ready at " + ready.Url);

        StartStatusTimer();

        // The window must already be realized (App shows it before this call)
        // for WebView2 to initialize; autostart/tray-only runs defer to
        // RestoreFromTray.
        if (IsVisible) await EnsureWebViewAsync(ready);
    }

    private bool _webViewStarted;

    private async Task EnsureWebViewAsync(ChatReady ready)
    {
        if (_webViewStarted) return;

        // A hard-killed previous run leaves orphaned msedgewebview2.exe groups
        // that hold the WebView2 user-data-folder lock; without cleanup the
        // next EnsureCoreWebView2Async hangs forever ("stuck at 启动中").
        var cleaned = WebView2Guard.KillOrphanedBrowsers();
        if (cleaned > 0) LogLine("webview2: cleaned " + cleaned + " orphaned browser process(es)");

        var userDataFolder = System.IO.Path.Combine(_processManager.DataRoot, "runtime", "webview2");
        System.IO.Directory.CreateDirectory(userDataFolder);

        // Some machines register the WebView2 runtime only in the 32-bit
        // registry view while this app is x64; default discovery then fails
        // or hangs. Resolve the runtime folder explicitly when possible.
        var browserFolder = WebView2Locator.FindRuntimeFolder();
        LogLine("start: webview2 runtime folder: " + (browserFolder ?? "(default discovery)"));

        var createTask = CoreWebView2Environment.CreateAsync(
            browserExecutableFolder: browserFolder, userDataFolder: userDataFolder, options: null);
        var createDone = await Task.WhenAny(createTask, Task.Delay(TimeSpan.FromSeconds(45)));
        if (createDone != createTask)
            throw new TimeoutException("WebView2 环境创建超时（45 秒）。请重启应用；若反复出现请安装 WebView2 Runtime。");
        var environment = await createTask;
        LogLine("start: webview2 environment ready");

        var initTask = WebView.EnsureCoreWebView2Async(environment);
        var finished = await Task.WhenAny(initTask, Task.Delay(TimeSpan.FromSeconds(45)));
        if (finished != initTask)
            throw new TimeoutException("WebView2 组件初始化超时（45 秒）。请重启应用；若反复出现请安装 WebView2 Runtime。");
        LogLine("start: core webview2 ready");

        // Inject the per-launch token ONLY on requests to our own loopback
        // ENGINE API - never on the DSH web origin or external domains. The
        // DSH web app itself is token-free (loopback-only binding).
        WebView.CoreWebView2.WebResourceRequested += (_, args) =>
        {
            args.Request.Headers.SetHeader("X-IA-Token", ready.Token);
        };
        WebView.CoreWebView2.AddWebResourceRequestedFilter(_processManager.EngineUrl + "/*", CoreWebView2WebResourceContext.All);

        var firstRun = _processManager.IsFirstRun;
        var startUrl = firstRun
            ? _processManager.EngineUrl + "/setup?token=" + Uri.EscapeDataString(ready.Token)
            : ready.Url + "/";
        LogLine("start: navigating to " + (firstRun ? "setup page" : ready.Url));
        WebView.CoreWebView2.Navigate(startUrl);
        LogLine("start: navigation issued");

        _webViewStarted = true;
    }

    private void StartStatusTimer()
    {
        _statusTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(5) };
        _statusTimer.Tick += async (_, _) => await RefreshStatusAsync();
        _statusTimer.Start();
        _ = RefreshStatusAsync();
    }

    private bool _agentStartAttempted;

    private async Task RefreshStatusAsync()
    {
        try
        {
            if (_ready == null) return;
            var status = await _processManager.FetchStatusAsync(_ready);
            AgentStatusText.Text = status.AgentRunning ? "投资引擎: ● 运行中" : "投资引擎: 未运行";
            ChatStatusText.Text = status.WebRunning ? "对话服务: ● 运行中" : "对话服务: 未运行";
            ModeText.Text = "模式: " + status.OperationMode;
            MandateText.Text = "策略: " + status.Mandate;
            RoundText.Text = "轮次: " + status.LastRound;
            HarnessText.Text = status.ControlNote.Length > 0
                ? "风控: " + status.ControlNote
                : "风控: 正常";

            // The wizard just completed: bring the autonomous engine up now
            // and switch the WebView from the setup page to the assistant.
            // One attempt per launch avoids a pythonw spawn storm.
            if (!_processManager.IsFirstRun && !_agentStartAttempted)
            {
                _agentStartAttempted = true;
                try
                {
                    _processManager.StartAgent();
                    LogLine("status: setup complete, starting investment engine");
                }
                catch (Exception ex)
                {
                    LogLine("status: FAILED to start investment engine: " + ex.Message);
                }
                if (_ready != null && WebView.CoreWebView2 != null)
                {
                    var current = WebView.Source?.ToString() ?? "";
                    if (!current.StartsWith(_ready.Url, StringComparison.OrdinalIgnoreCase))
                    {
                        LogLine("status: setup done, navigating to " + _ready.Url);
                        WebView.CoreWebView2.Navigate(_ready.Url + "/");
                    }
                }
            }
        }
        catch
        {
            ChatStatusText.Text = "对话服务: 未连接";
            HarnessText.Text = "风控: 未知";
        }
    }

    public void RestoreFromTray()
    {
        Show();
        WindowState = WindowState.Normal;
        Activate();

        // Tray-only (autostart) runs defer WebView2 init until the window
        // is actually shown; initialize lazily on first restore.
        if (!_webViewStarted && _ready != null)
            _ = EnsureWebViewAsync(_ready);
    }

    public void DisposeTray() => _tray?.Dispose();

    /// <summary>Explicit WebView teardown so the browser process group exits with us.</summary>
    public void ShutdownWebView()
    {
        try { WebView.Dispose(); } catch { }
    }

    private void LogLine(string message)
    {
        try
        {
            System.IO.File.AppendAllText(
                System.IO.Path.Combine(_processManager.DataRoot, "runtime", "logs", "desktop.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + Environment.NewLine);
        }
        catch { }
    }

    private void OnClosing(object? sender, CancelEventArgs e)
    {
        if (_closingViaTray)
        {
            // Final exit: tray already asked to stop everything.
            _processManager.StopAll();
            Application.Current.Shutdown();
            return;
        }

        var choice = MessageBox.Show(
            this,
            "关闭窗口后，Investment Auto 要继续做什么？\n\n" +
            "是 - 最小化到托盘：窗口隐藏，后台自动投资继续运行（推荐）\n" +
            "否 - 停止全部后台服务并退出\n" +
            "取消 - 保持窗口打开",
            "Investment Auto",
            MessageBoxButton.YesNoCancel,
            MessageBoxImage.Question,
            MessageBoxResult.Yes);

        // The current close never proceeds directly; the user's choice decides.
        e.Cancel = true;

        if (choice == MessageBoxResult.Yes)
        {
            Hide();
            _tray?.ShowMinimizedBalloon();
        }
        else if (choice == MessageBoxResult.No)
        {
            // Full exit re-enters OnClosing through the _closingViaTray path.
            Dispatcher.BeginInvoke(ExitFully);
        }
        // Cancel: keep the window open, nothing else to do.
    }

    private async Task PauseInvestmentAsync()
    {
        try
        {
            using var client = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            using var request = new System.Net.Http.HttpRequestMessage(
                System.Net.Http.HttpMethod.Post, _processManager.EngineUrl + "/api/commands/issue");
            request.Headers.Add("X-IA-Token", _processManager.AccessToken);
            request.Content = new System.Net.Http.StringContent(
                "{\"command\":\"pause\",\"payload\":{\"reason\":\"托盘暂停\"},\"requested_by\":\"desktop\"}",
                System.Text.Encoding.UTF8, "application/json");
            await client.SendAsync(request);
        }
        catch { }
    }

    private void OpenLogs()
    {
        var log = System.IO.Path.Combine(_processManager.DataRoot, "runtime", "logs", "investment-auto.log");
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
            {
                FileName = "notepad.exe",
                Arguments = "\"" + log + "\"",
                UseShellExecute = true,
            });
        }
        catch { }
    }

    public void ExitFully()
    {
        _closingViaTray = true;
        Close();
    }

    private void ToggleAutoStart(bool enable)
    {
        try
        {
            if (enable)
            {
                var exePath = System.Diagnostics.Process.GetCurrentProcess().MainModule?.FileName;
                if (string.IsNullOrEmpty(exePath))
                    exePath = System.IO.Path.Combine(AppContext.BaseDirectory, "InvestmentAuto.Desktop.exe");
                AutoStart.Enable(exePath, _processManager.AppRoot, _processManager.DataRoot);
            }
            else
            {
                AutoStart.Disable();
            }
            _tray?.ShowAutoStartBalloon(AutoStart.IsEnabled());
        }
        catch (Exception ex)
        {
            _tray?.ShowErrorBalloon("开机自启设置失败：" + ex.Message);
        }
    }
}
