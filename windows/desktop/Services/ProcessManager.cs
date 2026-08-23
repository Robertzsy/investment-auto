using System;
using System.Diagnostics;
using System.IO;
using System.Net;
using System.Net.Http;
using System.Net.Sockets;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

namespace InvestmentAuto.Desktop.Services;

/// <summary>
/// Owns the 2.0 background processes and their lifecycle:
///   - the investment ENGINE (pythonw -s -m engine.main serve | run), and
///   - the DSH web app (node dsh --profile investment-web), the conversation
///     and decision plane.
/// A Windows Job Object with KILL_ON_JOB_CLOSE guarantees no orphan
/// python/node process survives the desktop shell exit.
/// </summary>
internal sealed class ProcessManager : IDisposable
{
    private static readonly Regex WebUrlLine = new(
        @"dsh web:\s*https?://(127\.0\.0\.1|localhost):(\d+)", RegexOptions.Compiled);

    private readonly string _appRoot;
    private readonly string _dataRoot;
    private readonly string _pythonW;
    private readonly string _node;
    private readonly string _token;
    private int _enginePort;
    private int _webPort;
    private Process? _webProcess;
    private IntPtr _jobHandle;
    private bool _disposed;

    public ProcessManager(string? appRoot = null, string? dataRoot = null)
    {
        _appRoot = appRoot ?? AppDomain.CurrentDomain.BaseDirectory;
        _dataRoot = dataRoot
            ?? Environment.GetEnvironmentVariable("INVESTMENT_AUTO_DATA_DIR")
            ?? _appRoot;
        _pythonW = LocatePythonW(_appRoot);
        _node = LocateNode(_appRoot);
        _token = Environment.GetEnvironmentVariable("IA_ACCESS_TOKEN")
            ?? Convert.ToBase64String(Guid.NewGuid().ToByteArray()).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        Environment.SetEnvironmentVariable("IA_ACCESS_TOKEN", _token);
        Log($"init: appRoot={_appRoot} dataRoot={_dataRoot} python={_pythonW} node={_node}");
    }

    public string AppRoot => _appRoot;
    public string DataRoot => _dataRoot;
    public string AccessToken => _token;

    /// <summary>Loopback URL of the DSH web app (conversation plane).</summary>
    public string WebUrl => "http://127.0.0.1:" + _webPort;

    /// <summary>Loopback URL of the engine command API.</summary>
    public string EngineUrl => "http://127.0.0.1:" + _enginePort;

    /// <summary>True when the desktop wizard has never completed.</summary>
    public bool IsFirstRun =>
        !File.Exists(Path.Combine(_dataRoot, "runtime", "setup.complete"));

    internal static string LocatePythonW(string appRoot)
    {
        var candidates = new[]
        {
            Path.Combine(appRoot, "python", "pythonw.exe"),
            Path.Combine(appRoot, ".venv", "Scripts", "pythonw.exe"),
            Path.Combine(appRoot, "venv", "Scripts", "pythonw.exe"),
        };
        foreach (var candidate in candidates)
            if (File.Exists(candidate)) return candidate;

        // Development fallback: find pythonw on PATH.
        var path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var dir in path.Split(Path.PathSeparator))
        {
            var candidate = Path.Combine(dir, "pythonw.exe");
            if (File.Exists(candidate)) return candidate;
        }
        throw new FileNotFoundException("未找到 pythonw.exe。请先完成安装或初始化。");
    }

    internal static string LocateNode(string appRoot)
    {
        var candidates = new[]
        {
            Path.Combine(appRoot, "node", "node.exe"),
            Path.Combine(appRoot, "nodejs", "node.exe"),
        };
        foreach (var candidate in candidates)
            if (File.Exists(candidate)) return candidate;

        var path = Environment.GetEnvironmentVariable("PATH") ?? "";
        foreach (var dir in path.Split(Path.PathSeparator))
        {
            var candidate = Path.Combine(dir, "node.exe");
            if (File.Exists(candidate)) return candidate;
        }
        throw new FileNotFoundException("未找到 node.exe（需要 Node.js 22+）。请先完成安装或初始化。");
    }

    internal static int PickFreePort()
    {
        var listener = new TcpListener(IPAddress.Loopback, 0);
        listener.Start();
        var port = ((IPEndPoint)listener.LocalEndpoint).Port;
        listener.Stop();
        return port;
    }

    public async Task<ChatReady> EnsureRunningAsync(CancellationToken cancellationToken)
    {
        var ready = ReadReadyFile();
        if (ready != null && await IsHealthyAsync(ready, cancellationToken)) return ready;

        StartPublicServices();
        for (int attempt = 0; attempt < 120 && !cancellationToken.IsCancellationRequested; attempt++)
        {
            await Task.Delay(500, cancellationToken);
            ready = ReadReadyFile();
            if (ready != null && await IsHealthyAsync(ready, cancellationToken)) return ready;
        }
        throw new TimeoutException("对话服务在 60 秒内未就绪。请查看日志。");
    }

    public void StartPublicServices()
    {
        // Idempotent per instance: ports are picked once, the engine is
        // guarded by its own locks, and the web app binds its fixed port.
        CreateJobObject();
        if (_enginePort == 0) _enginePort = PickFreePort();
        if (_webPort == 0) _webPort = PickFreePort();

        // The API-only engine always serves the command API (bridge tools +
        // setup page); the autonomous scheduler must NOT start trading
        // before the wizard finished. Post-setup, StartAgent() brings up
        // `run` on top.
        StartEngine("serve");
        if (!IsFirstRun) StartEngine("run");

        if (_webProcess == null || _webProcess.HasExited) StartWeb();
    }

    /// <summary>Starts the autonomous engine process (post-setup, idempotent).</summary>
    public void StartAgent()
    {
        if (IsFirstRun) return; // wizard not finished; never start trading
        if (IsAgentAlive()) return;
        CreateJobObject();
        StartEngine("run");
    }

    private void Log(string message)
    {
        try
        {
            var dir = Path.Combine(_dataRoot, "runtime", "logs");
            Directory.CreateDirectory(dir);
            File.AppendAllText(
                Path.Combine(dir, "desktop.log"),
                DateTime.Now.ToString("yyyy-MM-dd HH:mm:ss") + " " + message + Environment.NewLine);
        }
        catch { }
    }

    private Dictionary<string, string> BaseEngineEnv()
    {
        var env = new Dictionary<string, string>
        {
            ["PYTHONUTF8"] = "1",
            ["PYTHONNOUSERSITE"] = "1",
            ["INVESTMENT_AUTO_DATA_DIR"] = _dataRoot,
            ["INVESTMENT_AUTO_ROOT"] = _appRoot,
            ["INVESTMENT_AUTO_APP_DIR"] = Path.Combine(_appRoot, "app"),
            ["INVESTMENT_API_PORT"] = _enginePort.ToString(),
            ["IA_ACCESS_TOKEN"] = _token,
            ["DSH_HOME"] = _dataRoot,
            ["DSH_PERMISSION_MODE"] = "danger-full-access",
        };
        var nodeDir = Path.GetDirectoryName(_node);
        if (!string.IsNullOrEmpty(nodeDir))
            env["PATH"] = nodeDir + ";" + (Environment.GetEnvironmentVariable("PATH") ?? "");
        return env;
    }

    private void StartEngine(string command)
    {
        if (_enginePort == 0) _enginePort = PickFreePort();
        var info = new ProcessStartInfo
        {
            FileName = _pythonW,
            // -s: never load the user's Python user-site packages; the
            // bundled runtime must be able to run entirely on its own.
            Arguments = "-s -m engine.main " + command,
            WorkingDirectory = _appRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        foreach (var pair in BaseEngineEnv()) info.Environment[pair.Key] = pair.Value;
        try
        {
            var process = Process.Start(info);
            Log($"started engine {command}: pid={process?.Id} (api={EngineUrl})");
            if (process != null) AssignToJob(process.Handle);
        }
        catch (Exception ex)
        {
            Log($"FAILED to start engine {command}: {ex.Message}");
            throw;
        }
    }

    private string WebReadyPath => Path.Combine(_dataRoot, "runtime", "web.ready.json");
    private string DshPatchPath => Path.Combine(_appRoot, "app", "profiles", "patches", "dpapi-credentials.yml");
    private string DshBinPath => Path.Combine(_appRoot, "app", "node_modules", "@deepseek-ai", "dsh", "lib", "bin.js");

    private void StartWeb()
    {
        if (_webPort == 0) _webPort = PickFreePort();
        if (!File.Exists(DshBinPath))
            throw new FileNotFoundException("未找到 DSH 应用（app/node_modules/@deepseek-ai/dsh）。请先完成安装。");
        var info = new ProcessStartInfo
        {
            FileName = _node,
            // Launcher flags (--profile/--patch) must precede the first app
            // flag (--port): everything after it is passed through verbatim.
            Arguments = "\"" + DshBinPath + "\" --profile investment-web"
                + (File.Exists(DshPatchPath) ? " --patch \"" + DshPatchPath + "\"" : "")
                + " --port " + _webPort,
            WorkingDirectory = _appRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            RedirectStandardOutput = true,
            RedirectStandardError = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        info.Environment["DSH_HOME"] = _dataRoot;
        info.Environment["DSH_TELEMETRY_DISABLED"] = "1";
        info.Environment["DSH_PERMISSION_MODE"] = "danger-full-access";
        info.Environment["IA_ACCESS_TOKEN"] = _token;
        info.Environment["INVESTMENT_AUTO_ROOT"] = _appRoot;
        info.Environment["INVESTMENT_AUTO_APP_DIR"] = Path.Combine(_appRoot, "app");
        info.Environment["INVESTMENT_ENGINE_URL"] = EngineUrl;
        info.Environment["INVESTMENT_ENGINE_APP_DIR"] = Path.Combine(_appRoot, "app");
        var nodeDir = Path.GetDirectoryName(_node);
        if (!string.IsNullOrEmpty(nodeDir))
            info.Environment["PATH"] = nodeDir + ";" + (Environment.GetEnvironmentVariable("PATH") ?? "");
        try
        {
            var process = Process.Start(info);
            _webProcess = process;
            Log($"started web: pid={process?.Id} (port={_webPort})");
            if (process != null)
            {
                AssignToJob(process.Handle);
                // Drain output streams (avoids pipe buffer deadlock) and
                // publish the ready file the moment the URL line appears.
                _ = Task.Run(() => ObserveWebOutputAsync(process));
            }
        }
        catch (Exception ex)
        {
            _webProcess = null;
            Log($"FAILED to start web: {ex.Message}");
            throw;
        }
    }

    private async Task ObserveWebOutputAsync(Process process)
    {
        var errorTask = Task.Run(() => process.StandardError.ReadToEndAsync());
        try
        {
            while (true)
            {
                var line = await process.StandardOutput.ReadLineAsync();
                if (line == null) break;
                Log("web: " + line);
                var match = WebUrlLine.Match(line);
                if (match.Success && int.TryParse(match.Groups[2].Value, out var port))
                {
                    PublishReady(port, process.Id);
                }
            }
        }
        catch (IOException) { /* stream closed on exit */ }
        catch (Exception ex)
        {
            Log("web: output observer failed: " + ex.Message);
        }
        try { await errorTask.WaitAsync(TimeSpan.FromSeconds(5)); } catch { }
        Log("web: process exited rc=" + (process.HasExited ? process.ExitCode : -1));
    }

    private void PublishReady(int port, int pid)
    {
        try
        {
            var payload = new ChatReady
            {
                Host = "127.0.0.1",
                Port = port,
                Token = _token,
                Url = "http://127.0.0.1:" + port,
                Pid = pid,
                StartedAt = DateTime.Now.ToString("o"),
            };
            var path = WebReadyPath;
            Directory.CreateDirectory(Path.GetDirectoryName(path)!);
            var temp = path + ".tmp";
            File.WriteAllText(temp, JsonSerializer.Serialize(payload));
            File.Move(temp, path, overwrite: true);
            Log("web: ready file published at " + payload.Url);
        }
        catch (Exception ex)
        {
            Log("web: FAILED to publish ready file: " + ex.Message);
        }
    }

    private ChatReady? ReadReadyFile()
    {
        var path = WebReadyPath;
        if (!File.Exists(path)) return null;
        try
        {
            return ChatReady.TryParse(File.ReadAllText(path));
        }
        catch { return null; }
    }

    private static async Task<bool> IsHealthyAsync(ChatReady ready, CancellationToken cancellationToken)
    {
        try
        {
            using var client = new HttpClient();
            client.Timeout = TimeSpan.FromSeconds(2);
            using var request = new HttpRequestMessage(HttpMethod.Get, ready.Url + "/");
            var response = await client.SendAsync(request, cancellationToken);
            return (int)response.StatusCode is >= 200 and < 500;
        }
        catch { return false; }
    }

    public async Task<RuntimeStatus> FetchStatusAsync(ChatReady ready)
    {
        var status = new RuntimeStatus();
        status.WebRunning = await IsHealthyAsync(ready, CancellationToken.None);
        try
        {
            using var client = new HttpClient { Timeout = TimeSpan.FromSeconds(3) };
            using var request = new HttpRequestMessage(HttpMethod.Get, EngineUrl + "/api/status");
            request.Headers.Add("X-IA-Token", _token);
            var response = await client.SendAsync(request);
            if (response.IsSuccessStatusCode)
            {
                using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
                var root = doc.RootElement;
                if (root.TryGetProperty("operation_mode", out var mode))
                    status.OperationMode = mode.GetString() ?? "-";
                if (root.TryGetProperty("mandate", out var mandate) && mandate.TryGetProperty("profile", out var profile))
                    status.Mandate = profile.GetString() ?? "-";
                if (root.TryGetProperty("control", out var control))
                {
                    if (control.TryGetProperty("paused", out var paused) && paused.GetBoolean())
                        status.ControlNote = "已暂停";
                    if (control.TryGetProperty("kill_switch", out var killed) && killed.GetBoolean())
                        status.ControlNote = "紧急停止";
                }
            }
        }
        catch { /* engine down; web health remains authoritative */ }

        status.AgentRunning = IsAgentAlive();
        status.LastRound = ReadLastRoundTime();
        return status;
    }

    private bool IsAgentAlive()
    {
        var path = Path.Combine(_dataRoot, "runtime", "investment", "bus", "worker.json");
        if (!File.Exists(path)) return false;
        try
        {
            var payload = JsonSerializer.Deserialize<JsonElement>(File.ReadAllText(path));
            if (payload.TryGetProperty("updated_at", out var updated))
            {
                if (DateTimeOffset.TryParse(updated.GetString(), out var time))
                    return (DateTimeOffset.Now - time).TotalSeconds <= 8;
            }
        }
        catch { }
        return false;
    }

    private string ReadLastRoundTime()
    {
        var dir = Path.Combine(_dataRoot, "runtime", "reports");
        try
        {
            if (!Directory.Exists(dir)) return "-";
            var latest = Directory.GetFiles(dir).OrderByDescending(File.GetLastWriteTime).FirstOrDefault();
            return latest == null ? "-" : File.GetLastWriteTime(latest).ToString("MM-dd HH:mm");
        }
        catch { return "-"; }
    }

    public void StopAll()
    {
        if (_jobHandle != IntPtr.Zero)
        {
            TerminateJobObject(_jobHandle, 1);
            CloseHandle(_jobHandle);
            _jobHandle = IntPtr.Zero;
        }
    }

    private void CreateJobObject()
    {
        // Idempotent: repeated "启动服务" clicks must reuse the same job
        // handle instead of leaking the previous one (orphaning its children).
        if (_jobHandle != IntPtr.Zero) return;
        _jobHandle = CreateJobObject(IntPtr.Zero, null);
        var info = new JOBOBJECT_EXTENDED_LIMIT_INFORMATION
        {
            BasicLimitInformation = new JOBOBJECT_BASIC_LIMIT_INFORMATION
            {
                LimitFlags = 0x2000, // JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
            },
        };
        int length = Marshal.SizeOf(typeof(JOBOBJECT_EXTENDED_LIMIT_INFORMATION));
        IntPtr extendedInfoPtr = Marshal.AllocHGlobal(length);
        try
        {
            Marshal.StructureToPtr(info, extendedInfoPtr, false);
            SetInformationJobObject(_jobHandle, JobObjectInfoType.ExtendedLimitInformation, extendedInfoPtr, (uint)length);
        }
        finally { Marshal.FreeHGlobal(extendedInfoPtr); }
    }

    private void AssignToJob(IntPtr processHandle)
    {
        if (_jobHandle != IntPtr.Zero) AssignProcessToJobObject(_jobHandle, processHandle);
    }

    public void Dispose()
    {
        if (_disposed) return;
        _disposed = true;
        StopAll();
    }

    // P/Invoke
    [DllImport("kernel32.dll", CharSet = CharSet.Unicode)]
    private static extern IntPtr CreateJobObject(IntPtr lpJobAttributes, string? lpName);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool AssignProcessToJobObject(IntPtr hJob, IntPtr hProcess);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool SetInformationJobObject(IntPtr hJob, JobObjectInfoType infoType, IntPtr lpJobObjectInfo, uint cbJobObjectInfoLength);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool TerminateJobObject(IntPtr hJob, uint uExitCode);
    [DllImport("kernel32.dll", SetLastError = true)]
    private static extern bool CloseHandle(IntPtr hObject);

    private enum JobObjectInfoType { ExtendedLimitInformation = 9 }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_BASIC_LIMIT_INFORMATION
    {
        public long PerProcessUserTimeLimit;
        public long PerJobUserTimeLimit;
        public uint LimitFlags;
        public UIntPtr MinimumWorkingSetSize;
        public UIntPtr MaximumWorkingSetSize;
        public uint ActiveProcessLimit;
        public UIntPtr Affinity;
        public uint PriorityClass;
        public uint SchedulingClass;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct IO_COUNTERS
    {
        public ulong ReadOperationCount, WriteOperationCount, OtherOperationCount;
        public ulong ReadTransferCount, WriteTransferCount, OtherTransferCount;
    }

    [StructLayout(LayoutKind.Sequential)]
    private struct JOBOBJECT_EXTENDED_LIMIT_INFORMATION
    {
        public JOBOBJECT_BASIC_LIMIT_INFORMATION BasicLimitInformation;
        public IO_COUNTERS IoInfo;
        public UIntPtr ProcessMemoryLimit;
        public UIntPtr JobMemoryLimit;
        public UIntPtr PeakProcessMemoryUsed;
        public UIntPtr PeakJobMemoryUsed;
    }
}

internal sealed class ChatReady
{
    public string Host { get; set; } = "127.0.0.1";
    public int Port { get; set; }
    public string Token { get; set; } = "";
    public string Url { get; set; } = "";
    public int Pid { get; set; }
    [System.Text.Json.Serialization.JsonPropertyName("started_at")]
    public string StartedAt { get; set; } = "";

    /// <summary>Parses a web.ready.json payload; null when missing/invalid/portless.
    /// Binding is case-insensitive, and "localhost" is normalized to the IPv4
    /// loopback WebView2 can actually reach.</summary>
    internal static ChatReady? TryParse(string json)
    {
        try
        {
            var payload = JsonSerializer.Deserialize<ChatReady>(json,
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
            if (payload == null || payload.Port <= 0) return null;
            payload.Url = payload.Url.Replace("://localhost:", "://127.0.0.1:");
            return payload;
        }
        catch { return null; }
    }
}

internal sealed class RuntimeStatus
{
    public bool AgentRunning { get; set; }
    public bool WebRunning { get; set; }
    public string OperationMode { get; set; } = "-";
    public string Mandate { get; set; } = "-";
    public string ControlNote { get; set; } = "";
    public string LastRound { get; set; } = "-";
}
