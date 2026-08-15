using System;
using System.IO;
using System.IO.Pipes;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Windows;
using InvestmentAuto.Desktop.Services;
using Application = System.Windows.Application;
using MessageBox = System.Windows.MessageBox;

namespace InvestmentAuto.Desktop;

public partial class App : Application
{
    private Mutex? _mutex;
    private ProcessManager? _processManager;
    private MainWindow? _window;
    private CancellationTokenSource? _pipeCts;

    protected override async void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);
        DispatcherUnhandledException += OnDispatcherUnhandledException;
        try
        {
            System.IO.File.AppendAllText(
                System.IO.Path.Combine(Path.GetTempPath(), "ia-desktop-boot.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " OnStartup args=" + string.Join("|", e.Args) + Environment.NewLine);
        }
        catch { }

        if (!SingleInstance.TryAcquire(out _mutex))
        {
            SingleInstance.SignalExisting();
            Shutdown();
            return;
        }

        ShutdownMode = ShutdownMode.OnExplicitShutdown;

        var appRoot = ReadArg(e.Args, "--app-root");
        var dataRoot = ReadArg(e.Args, "--data-root");
        var autoStart = e.Args.Contains("--autostart");
        _processManager = new ProcessManager(appRoot, dataRoot);
        _window = new MainWindow(_processManager);

        ListenForShowSignal();

        // The window must be realized BEFORE StartAsync: WebView2's host
        // HWND is created from the window handle, and initializing it while
        // the window is still hidden can leave EnsureCoreWebView2Async
        // hanging forever. Autostart stays tray-only, so it skips showing.
        if (!autoStart) _window.Show();

        try
        {
            await _window.StartAsync();
        }
        catch (Exception ex)
        {
            try
            {
                File.AppendAllText(
                    Path.Combine(Path.GetTempPath(), "ia-desktop-boot.log"),
                    DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " STARTUP-FAILED: " + ex + Environment.NewLine);
            }
            catch { }
            MessageBox.Show("启动失败：" + ex.Message + "\n\n诊断日志：%TEMP%\\ia-desktop-boot.log", "Investment Auto",
                MessageBoxButton.OK, MessageBoxImage.Error);
            Shutdown();
            return;
        }
    }

    private static string? ReadArg(string[] args, string name)
    {
        foreach (var arg in args)
        {
            if (arg.StartsWith(name + "=", StringComparison.Ordinal))
                return arg.Substring(name.Length + 1).Trim('"');
        }
        for (int i = 0; i + 1 < args.Length; i++)
            if (args[i] == name) return args[i + 1];
        return null;
    }

    private void ListenForShowSignal()
    {
        _pipeCts = new CancellationTokenSource();
        var token = _pipeCts.Token;
        Task.Run(() =>
        {
            while (!token.IsCancellationRequested)
            {
                try
                {
                    using var server = new NamedPipeServerStream(
                        SingleInstance.PipeName, PipeDirection.In,
                        1, PipeTransmissionMode.Byte, PipeOptions.Asynchronous);
                    server.WaitForConnectionAsync(token).GetAwaiter().GetResult();
                    var buffer = new byte[16];
                    var read = server.Read(buffer, 0, buffer.Length);
                    var message = Encoding.UTF8.GetString(buffer, 0, read);
                    if (message == "show")
                    {
                        Dispatcher.Invoke(() =>
                        {
                            _window?.RestoreFromTray();
                        });
                    }
                }
                catch (OperationCanceledException) { break; }
                catch { /* pipe torn down; retry */ }
            }
        }, token);
    }

    protected override void OnExit(ExitEventArgs e)
    {
        try
        {
            File.AppendAllText(
                Path.Combine(Path.GetTempPath(), "ia-desktop-boot.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " OnExit code=" + e.ApplicationExitCode + Environment.NewLine);
        }
        catch { }
        _pipeCts?.Cancel();
        _window?.ShutdownWebView();
        _window?.DisposeTray();
        _processManager?.Dispose();
        _mutex?.ReleaseMutex();
        base.OnExit(e);
    }

    private void OnDispatcherUnhandledException(object sender, System.Windows.Threading.DispatcherUnhandledExceptionEventArgs e)
    {
        try
        {
            File.AppendAllText(
                Path.Combine(Path.GetTempPath(), "ia-desktop-boot.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " DISPATCHER-EX: " + e.Exception + Environment.NewLine);
        }
        catch { }
    }
}
