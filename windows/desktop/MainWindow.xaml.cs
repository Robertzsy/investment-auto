using System;
using System.ComponentModel;
using System.Threading.Tasks;
using System.Windows;
using System.Windows.Threading;
using InvestmentAuto.Desktop.Services;
using Microsoft.Web.WebView2.Core;
using Application = System.Windows.Application;

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

        WebView.CoreWebView2.Navigate(ready.Url);

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

        e.Cancel = true;
        Hide();
        _tray?.ShowMinimizedBalloon();
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
}
