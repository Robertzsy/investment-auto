using System;
using System.Diagnostics;
using System.IO;
using System.Runtime.InteropServices;
using System.Text.Json;
using System.Threading;
using System.Threading.Tasks;

namespace InvestmentAuto.Desktop.Services;

/// <summary>
/// Owns the background Python services (investment agent + chat) and their
/// lifecycle.  A Windows Job Object with KILL_ON_JOB_CLOSE guarantees no
/// orphan python/node process survives the desktop shell exit.
/// </summary>
internal sealed class ProcessManager : IDisposable
{
    private readonly string _appRoot;
    private readonly string _dataRoot;
    private readonly string _pythonW;
    private readonly string _token;
    private IntPtr _jobHandle;
    private bool _disposed;

    public ProcessManager(string? appRoot = null, string? dataRoot = null)
    {
        _appRoot = appRoot ?? AppDomain.CurrentDomain.BaseDirectory;
        _dataRoot = dataRoot
            ?? Environment.GetEnvironmentVariable("INVESTMENT_AUTO_DATA_DIR")
            ?? _appRoot;
        _pythonW = LocatePythonW(_appRoot);
        _token = Environment.GetEnvironmentVariable("IA_ACCESS_TOKEN")
            ?? Convert.ToBase64String(Guid.NewGuid().ToByteArray()).TrimEnd('=').Replace('+', '-').Replace('/', '_');
        Environment.SetEnvironmentVariable("IA_ACCESS_TOKEN", _token);
        Log($"init: appRoot={_appRoot} dataRoot={_dataRoot} python={_pythonW}");
    }

    public string AppRoot => _appRoot;
    public string DataRoot => _dataRoot;
    public string AccessToken => _token;

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
        // The agent side is idempotent (scheduler lock + worker heartbeat);
        // the chat side binds a fresh dynamic port and rewrites the ready file.
        //
        // First run: only the chat service starts. The autonomous agent must
        // NOT begin trading before the user finished the wizard (no models,
        // strategy or mode selected yet). StartAgent() is called after the
        // setup marker appears.
        CreateJobObject();
        if (!IsFirstRun) StartPython("run");
        StartPython("chat");
    }

    /// <summary>Starts the autonomous agent process (post-setup).</summary>
    public void StartAgent()
    {
        if (IsFirstRun) return; // wizard not finished; never start trading
        CreateJobObject();
        StartPython("run");
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

    private void StartPython(string command)
    {
        var info = new ProcessStartInfo
        {
            FileName = _pythonW,
            // -s: never load the user's Python user-site packages; the
            // bundled runtime must be able to run entirely on its own.
            Arguments = "-s -m src.main " + command,
            WorkingDirectory = _appRoot,
            UseShellExecute = false,
            CreateNoWindow = true,
            WindowStyle = ProcessWindowStyle.Hidden,
        };
        info.Environment["PYTHONUTF8"] = "1";
        info.Environment["PYTHONNOUSERSITE"] = "1";
        info.Environment["INVESTMENT_AUTO_DATA_DIR"] = _dataRoot;
        info.Environment["IA_ACCESS_TOKEN"] = _token;
        info.Environment["CHAT_OPEN_BROWSER"] = "false";
        info.Environment["CHAT_PORT"] = "0";
        // The bundled Node.js must be on PATH for stock-fetcher/macro jobs.
        var nodeDir = Path.Combine(_appRoot, "node");
        if (Directory.Exists(nodeDir))
            info.Environment["PATH"] = nodeDir + ";" + (info.Environment["PATH"] ?? "");
        try
        {
            var process = Process.Start(info);
            Log($"started {command}: {_pythonW} (pid={process?.Id})");
            if (process != null) AssignToJob(process.Handle);
        }
        catch (Exception ex)
        {
            Log($"FAILED to start {command}: {ex.Message}");
            throw;
        }
    }

    private ChatReady? ReadReadyFile()
    {
        var path = Path.Combine(_dataRoot, "runtime", "chat.ready.json");
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
            using var client = new System.Net.Http.HttpClient();
            client.Timeout = TimeSpan.FromSeconds(2);
            using var request = new System.Net.Http.HttpRequestMessage(
                System.Net.Http.HttpMethod.Get, ready.Url + "/api/history");
            request.Headers.Add("X-IA-Token", ready.Token);
            var response = await client.SendAsync(request, cancellationToken);
            return (int)response.StatusCode is >= 200 and < 500;
        }
        catch { return false; }
    }

    public async Task<RuntimeStatus> FetchStatusAsync(ChatReady ready)
    {
        var status = new RuntimeStatus();
        try
        {
            using var client = new System.Net.Http.HttpClient();
            client.Timeout = TimeSpan.FromSeconds(3);
            using var request = new System.Net.Http.HttpRequestMessage(
                System.Net.Http.HttpMethod.Get, ready.Url + "/api/autonomy");
            request.Headers.Add("X-IA-Token", ready.Token);
            var response = await client.SendAsync(request);
            status.ChatRunning = (int)response.StatusCode is >= 200 and < 500;
            var body = await response.Content.ReadAsStringAsync();
            using var doc = JsonDocument.Parse(body);
            var root = doc.RootElement;
            if (root.TryGetProperty("operation_mode", out var mode))
                status.OperationMode = mode.GetString() ?? "-";
            if (root.TryGetProperty("mandate", out var mandate) && mandate.TryGetProperty("profile", out var profile))
                status.Mandate = profile.GetString() ?? "-";
        }
        catch { status.ChatRunning = false; }

        try
        {
            using var client = new System.Net.Http.HttpClient { Timeout = TimeSpan.FromSeconds(3) };
            using var request = new System.Net.Http.HttpRequestMessage(
                System.Net.Http.HttpMethod.Get, ready.Url + "/api/harness?limit=1");
            request.Headers.Add("X-IA-Token", ready.Token);
            var response = await client.SendAsync(request);
            if (response.IsSuccessStatusCode)
            {
                using var doc = JsonDocument.Parse(await response.Content.ReadAsStringAsync());
                var root = doc.RootElement;
                if (root.TryGetProperty("summary", out var summary)
                    && summary.TryGetProperty("skills", out var skills))
                    status.HarnessSkills = skills.GetInt32();
                if (root.TryGetProperty("executions", out var executions)
                    && executions.ValueKind == JsonValueKind.Array && executions.GetArrayLength() > 0
                    && executions[0].TryGetProperty("status", out var latestStatus))
                    status.HarnessLastStatus = latestStatus.GetString() ?? "就绪";
            }
        }
        catch { /* Harness status is additive; chat health remains authoritative. */ }

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

    /// <summary>Parses a chat.ready.json payload; null when missing/invalid/portless.
    /// The ready file is written by Python with lowercase keys (host/port/token/url/
    /// pid/started_at), so binding must be case-insensitive.</summary>
    internal static ChatReady? TryParse(string json)
    {
        try
        {
            var payload = JsonSerializer.Deserialize<ChatReady>(json,
                new JsonSerializerOptions { PropertyNameCaseInsensitive = true });
            if (payload == null || payload.Port <= 0) return null;
            // Older ready files (and any future writer) may emit "localhost";
            // WebView2 can resolve that to IPv6 ::1 and hang in SYN_SENT when
            // the Python server only bound IPv4 loopback. Always use IPv4.
            payload.Url = payload.Url.Replace("://localhost:", "://127.0.0.1:");
            return payload;
        }
        catch { return null; }
    }
}

internal sealed class RuntimeStatus
{
    public bool AgentRunning { get; set; }
    public bool ChatRunning { get; set; }
    public string OperationMode { get; set; } = "-";
    public string Mandate { get; set; } = "-";
    public string LastRound { get; set; } = "-";
    public int HarnessSkills { get; set; }
    public string HarnessLastStatus { get; set; } = "就绪";
}
