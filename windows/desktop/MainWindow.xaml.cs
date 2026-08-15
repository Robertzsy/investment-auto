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
        var ready = await _processManager.EnsureRunningAsync(new System.Threading.CancellationToken());
        _ready = ready;
        await WebView.EnsureCoreWebView2Async(null);

        // Inject the per-launch token on every request, so the front-end
        // never has to know about authentication.
        WebView.CoreWebView2.WebResourceRequested += (_, args) =>
        {
            args.Request.Headers.SetHeader("X-IA-Token", ready.Token);
        };
        WebView.CoreWebView2.AddWebResourceRequestedFilter("*", CoreWebView2WebResourceContext.All);

        var startPage = _processManager.IsFirstRun ? "/setup" : "/";
        WebView.CoreWebView2.Navigate(ready.Url + startPage + "?token=" + Uri.EscapeDataString(ready.Token));

        StartStatusTimer();
    }

    private void StartStatusTimer()
    {
        _statusTimer = new DispatcherTimer { Interval = TimeSpan.FromSeconds(5) };
        _statusTimer.Tick += async (_, _) => await RefreshStatusAsync();
        _statusTimer.Start();
        _ = RefreshStatusAsync();
    }

    private async Task RefreshStatusAsync()
    {
        try
        {
            if (_ready == null) return;
            var status = await _processManager.FetchStatusAsync(_ready);
            AgentStatusText.Text = status.AgentRunning ? "投资 Agent: ● 运行中" : "投资 Agent: 未运行";
            ChatStatusText.Text = status.ChatRunning ? "对话服务: ● 运行中" : "对话服务: 未运行";
            ModeText.Text = "模式: " + status.OperationMode;
            MandateText.Text = "策略: " + status.Mandate;
            RoundText.Text = "轮次: " + status.LastRound;
        }
        catch
        {
            ChatStatusText.Text = "对话服务: 未连接";
        }
    }

    public void RestoreFromTray()
    {
        Show();
        WindowState = WindowState.Normal;
        Activate();
    }

    public void DisposeTray() => _tray?.Dispose();

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
        if (_ready == null) return;
        try
        {
            using var client = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(5) };
            using var request = new System.Net.Http.HttpRequestMessage(
                System.Net.Http.HttpMethod.Post, _ready.Url + "/api/autonomy/pause");
            request.Headers.Add("X-IA-Token", _ready.Token);
            request.Content = new System.Net.Http.StringContent(
                "{}", System.Text.Encoding.UTF8, "application/json");
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
