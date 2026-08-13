using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Management;
using System.Net;
using System.Reflection;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Windows.Forms;
using Microsoft.Win32;

[assembly: AssemblyTitle("Investment Auto")]
[assembly: AssemblyDescription("Investment Auto Windows launcher")]
[assembly: AssemblyCompany("Investment Auto Contributors")]
[assembly: AssemblyProduct("Investment Auto")]
[assembly: AssemblyCopyright("MIT License")]
[assembly: AssemblyVersion("0.4.0.0")]
[assembly: AssemblyFileVersion("0.4.0.0")]

namespace InvestmentAuto.Windows
{
    internal sealed class AppStatus
    {
        public string ProjectRoot;
        public bool ProjectReady;
        public bool PythonReady;
        public bool NodeReady;
        public bool AgentRunning;
        public bool ChatRunning;
        public bool AutoStartEnabled;

        public bool AllRunning { get { return AgentRunning && ChatRunning; } }
    }

    internal static class LauncherService
    {
        internal const string ProductName = "Investment Auto";
        internal const string AutoStartValueName = "InvestmentAuto";
        internal const string DashboardUrl = "http://127.0.0.1:8080";
        private const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";

        internal static string ExecutablePath
        {
            get { return Process.GetCurrentProcess().MainModule.FileName; }
        }

        internal static string ResolveProjectRoot(string explicitPath)
        {
            var candidates = new List<string>();
            if (!String.IsNullOrWhiteSpace(explicitPath)) candidates.Add(explicitPath);
            var configured = Environment.GetEnvironmentVariable("INVESTMENT_AUTO_HOME");
            if (!String.IsNullOrWhiteSpace(configured)) candidates.Add(configured);
            candidates.Add(AppDomain.CurrentDomain.BaseDirectory);
            candidates.Add(Path.GetFullPath(Path.Combine(AppDomain.CurrentDomain.BaseDirectory, "..")));
            candidates.Add(@"D:\investment-auto");
            candidates.Add(Environment.CurrentDirectory);

            foreach (var candidate in candidates)
            {
                try
                {
                    var root = Path.GetFullPath(candidate.Trim().Trim('"'));
                    if (File.Exists(Path.Combine(root, "src", "main.py"))) return root;
                }
                catch (Exception) { }
            }
            return Path.GetFullPath(String.IsNullOrWhiteSpace(explicitPath)
                ? AppDomain.CurrentDomain.BaseDirectory
                : explicitPath);
        }

        internal static string FindPython(string projectRoot)
        {
            var candidates = new[]
            {
                Path.Combine(projectRoot, ".venv", "Scripts", "python.exe"),
                Path.Combine(projectRoot, ".venv", "Scripts", "pythonw.exe"),
                Path.Combine(projectRoot, "venv", "Scripts", "python.exe"),
                Path.Combine(projectRoot, "venv", "Scripts", "pythonw.exe")
            };
            foreach (var candidate in candidates)
                if (File.Exists(candidate)) return candidate;
            return null;
        }

        internal static bool HasNode()
        {
            return FindOnPath("node.exe") != null;
        }

        private static string FindOnPath(string fileName)
        {
            var path = Environment.GetEnvironmentVariable("PATH") ?? String.Empty;
            foreach (var item in path.Split(Path.PathSeparator))
            {
                try
                {
                    var candidate = Path.Combine(item.Trim().Trim('"'), fileName);
                    if (File.Exists(candidate)) return candidate;
                }
                catch (Exception) { }
            }
            return null;
        }

        internal static AppStatus GetStatus(string projectRoot)
        {
            return new AppStatus
            {
                ProjectRoot = projectRoot,
                ProjectReady = File.Exists(Path.Combine(projectRoot, "src", "main.py")),
                PythonReady = FindPython(projectRoot) != null,
                NodeReady = HasNode(),
                AgentRunning = IsAgentAlive(projectRoot),
                ChatRunning = IsChatAlive(),
                AutoStartEnabled = IsAutoStartEnabled()
            };
        }

        internal static bool IsAgentAlive(string projectRoot)
        {
            try
            {
                var path = Path.Combine(projectRoot, "runtime", "investment", "bus", "worker.json");
                if (!File.Exists(path)) return false;
                var text = File.ReadAllText(path, Encoding.UTF8);
                var match = Regex.Match(text, "\\\"updated_at\\\"\\s*:\\s*\\\"([^\\\"]+)\\\"");
                if (!match.Success) return false;
                DateTimeOffset updated;
                if (!DateTimeOffset.TryParse(match.Groups[1].Value, out updated)) return false;
                return Math.Abs((DateTimeOffset.Now - updated).TotalSeconds) <= 8.0;
            }
            catch (Exception) { return false; }
        }

        internal static bool IsChatAlive()
        {
            try
            {
                var request = (HttpWebRequest)WebRequest.Create(DashboardUrl + "/api/history");
                request.Method = "GET";
                request.Timeout = 900;
                request.ReadWriteTimeout = 900;
                request.Proxy = null;
                using (var response = (HttpWebResponse)request.GetResponse())
                    return (int)response.StatusCode >= 200 && (int)response.StatusCode < 500;
            }
            catch (Exception) { return false; }
        }

        internal static string ValidateForStart(string projectRoot)
        {
            if (!File.Exists(Path.Combine(projectRoot, "src", "main.py")))
                return "没有找到项目文件 src\\main.py。请把 EXE 放到 investment-auto 根目录，或设置 INVESTMENT_AUTO_HOME。";
            if (FindPython(projectRoot) == null)
                return "没有找到项目虚拟环境。请先运行 Setup-Windows.cmd 安装 Python 依赖。";
            if (!HasNode())
                return "没有找到 Node.js。请先安装 Node.js 18 或更高版本，并重新登录 Windows。";
            return null;
        }

        internal static string StartServices(string projectRoot, bool openBrowser)
        {
            var error = ValidateForStart(projectRoot);
            if (error != null) return error;
            var started = new List<string>();

            if (!IsAgentAlive(projectRoot))
            {
                var process = StartPython(projectRoot, "run");
                RememberProcess(projectRoot, process, "agent");
                started.Add("投资 Agent");
            }
            if (!IsChatAlive())
            {
                var process = StartPython(projectRoot, "chat");
                RememberProcess(projectRoot, process, "chat");
                started.Add("AI 对话与仪表盘");
            }

            var chatReady = IsChatAlive();
            for (var attempt = 0; !chatReady && attempt < 40; attempt++)
            {
                Thread.Sleep(500);
                chatReady = IsChatAlive();
            }
            if (openBrowser && chatReady) OpenDashboard();
            WriteLauncherLog(projectRoot, "start", started.Count == 0 ? "already-running" : String.Join(",", started.ToArray()));
            if (!chatReady) return "服务已启动，但对话页面在 20 秒内没有就绪。请查看 runtime\\logs\\investment-auto.log。";
            return started.Count == 0 ? "服务已经在运行，已打开管理页面。" : "已启动：" + String.Join("、", started.ToArray()) + "。";
        }

        private static Process StartPython(string projectRoot, string command)
        {
            var info = new ProcessStartInfo
            {
                FileName = FindPython(projectRoot),
                Arguments = "-m src.main " + command,
                WorkingDirectory = projectRoot,
                UseShellExecute = false,
                CreateNoWindow = true,
                WindowStyle = ProcessWindowStyle.Hidden
            };
            info.EnvironmentVariables["PYTHONUTF8"] = "1";
            info.EnvironmentVariables["PYTHONIOENCODING"] = "utf-8";
            if (command == "chat") info.EnvironmentVariables["CHAT_OPEN_BROWSER"] = "false";
            var process = Process.Start(info);
            if (process == null) throw new InvalidOperationException("无法创建 " + command + " 进程");
            return process;
        }

        private static void RememberProcess(string projectRoot, Process process, string role)
        {
            try
            {
                var directory = Path.Combine(projectRoot, "runtime", "launcher");
                Directory.CreateDirectory(directory);
                var line = role + "|" + process.Id + "|" + process.StartTime.ToUniversalTime().Ticks + Environment.NewLine;
                File.AppendAllText(Path.Combine(directory, "processes.txt"), line, Encoding.UTF8);
            }
            catch (Exception) { }
        }

        internal static string StopServices(string projectRoot)
        {
            var processIds = new HashSet<int>();
            AddRememberedProcesses(projectRoot, processIds);
            AddMatchingProjectProcesses(projectRoot, processIds);
            var stopped = 0;
            foreach (var processId in processIds)
            {
                if (processId == Process.GetCurrentProcess().Id) continue;
                try
                {
                    var info = new ProcessStartInfo
                    {
                        FileName = "taskkill.exe",
                        Arguments = "/PID " + processId + " /T /F",
                        UseShellExecute = false,
                        CreateNoWindow = true,
                        WindowStyle = ProcessWindowStyle.Hidden
                    };
                    using (var killer = Process.Start(info))
                    {
                        if (killer != null)
                        {
                            killer.WaitForExit(8000);
                            if (killer.ExitCode == 0) stopped++;
                        }
                    }
                }
                catch (Exception) { }
            }
            try { File.Delete(Path.Combine(projectRoot, "runtime", "launcher", "processes.txt")); }
            catch (Exception) { }
            WriteLauncherLog(projectRoot, "stop", "processes=" + stopped);
            return stopped == 0 ? "未发现可停止的项目进程。" : "项目服务已停止。";
        }

        private static void AddRememberedProcesses(string projectRoot, HashSet<int> target)
        {
            try
            {
                var statePath = Path.Combine(projectRoot, "runtime", "launcher", "processes.txt");
                if (!File.Exists(statePath)) return;
                foreach (var line in File.ReadAllLines(statePath, Encoding.UTF8))
                {
                    var parts = line.Split('|');
                    int pid;
                    long ticks;
                    if (parts.Length != 3 || !Int32.TryParse(parts[1], out pid) || !Int64.TryParse(parts[2], out ticks)) continue;
                    try
                    {
                        var process = Process.GetProcessById(pid);
                        if (Math.Abs(process.StartTime.ToUniversalTime().Ticks - ticks) < TimeSpan.TicksPerSecond * 2)
                            target.Add(pid);
                    }
                    catch (Exception) { }
                }
            }
            catch (Exception) { }
        }

        private static void AddMatchingProjectProcesses(string projectRoot, HashSet<int> target)
        {
            try
            {
                var normalizedRoot = Path.GetFullPath(projectRoot).TrimEnd('\\') + "\\";
                using (var searcher = new ManagementObjectSearcher(
                    "SELECT ProcessId, ExecutablePath, CommandLine FROM Win32_Process " +
                    "WHERE Name='python.exe' OR Name='pythonw.exe'"))
                using (var results = searcher.Get())
                {
                    foreach (ManagementObject item in results)
                    {
                        var executable = Convert.ToString(item["ExecutablePath"]);
                        var commandLine = Convert.ToString(item["CommandLine"]);
                        var belongsToRoot = (!String.IsNullOrEmpty(executable) && executable.StartsWith(normalizedRoot, StringComparison.OrdinalIgnoreCase))
                            || (!String.IsNullOrEmpty(commandLine) && commandLine.IndexOf(normalizedRoot, StringComparison.OrdinalIgnoreCase) >= 0);
                        var isService = !String.IsNullOrEmpty(commandLine)
                            && (commandLine.IndexOf("-m src.main run", StringComparison.OrdinalIgnoreCase) >= 0
                                || commandLine.IndexOf("-m src.main chat", StringComparison.OrdinalIgnoreCase) >= 0);
                        if (belongsToRoot && isService) target.Add(Convert.ToInt32(item["ProcessId"]));
                    }
                }
            }
            catch (Exception) { }
        }

        internal static void OpenDashboard()
        {
            Process.Start(new ProcessStartInfo { FileName = DashboardUrl, UseShellExecute = true });
        }

        internal static bool IsAutoStartEnabled()
        {
            try
            {
                using (var key = Registry.CurrentUser.OpenSubKey(RunKeyPath, false))
                {
                    var value = key == null ? null : Convert.ToString(key.GetValue(AutoStartValueName));
                    return !String.IsNullOrWhiteSpace(value)
                        && value.IndexOf(ExecutablePath, StringComparison.OrdinalIgnoreCase) >= 0;
                }
            }
            catch (Exception) { return false; }
        }

        internal static void SetAutoStart(bool enabled, string projectRoot)
        {
            using (var key = Registry.CurrentUser.CreateSubKey(RunKeyPath))
            {
                if (key == null) throw new InvalidOperationException("无法打开 Windows 当前用户启动项");
                if (enabled)
                {
                    var command = "\"" + ExecutablePath + "\" --start --minimized --project \"" + projectRoot + "\"";
                    key.SetValue(AutoStartValueName, command, RegistryValueKind.String);
                }
                else key.DeleteValue(AutoStartValueName, false);
            }
            WriteLauncherLog(projectRoot, "autostart", enabled ? "enabled" : "disabled");
        }

        internal static void WriteDiagnostics(string outputPath, string projectRoot)
        {
            var status = GetStatus(projectRoot);
            var json = "{" +
                "\"project_root\":\"" + EscapeJson(projectRoot) + "\"," +
                "\"project_ready\":" + status.ProjectReady.ToString().ToLowerInvariant() + "," +
                "\"python_ready\":" + status.PythonReady.ToString().ToLowerInvariant() + "," +
                "\"node_ready\":" + status.NodeReady.ToString().ToLowerInvariant() + "," +
                "\"agent_running\":" + status.AgentRunning.ToString().ToLowerInvariant() + "," +
                "\"chat_running\":" + status.ChatRunning.ToString().ToLowerInvariant() + "," +
                "\"autostart_enabled\":" + status.AutoStartEnabled.ToString().ToLowerInvariant() + "}";
            File.WriteAllText(outputPath, json, new UTF8Encoding(false));
        }

        private static string EscapeJson(string value)
        {
            return value.Replace("\\", "\\\\").Replace("\"", "\\\"");
        }

        private static void WriteLauncherLog(string projectRoot, string action, string detail)
        {
            try
            {
                var directory = Path.Combine(projectRoot, "runtime", "logs");
                Directory.CreateDirectory(directory);
                var line = DateTimeOffset.Now.ToString("yyyy-MM-dd HH:mm:ss zzz") + " " + action + " " + detail + Environment.NewLine;
                File.AppendAllText(Path.Combine(directory, "launcher.log"), line, Encoding.UTF8);
            }
            catch (Exception) { }
        }
    }

    internal sealed class LauncherForm : Form
    {
        private readonly string projectRoot;
        private readonly Label statusBadge;
        private readonly Label detailLabel;
        private readonly Button startButton;
        private readonly Button stopButton;
        private readonly Button openButton;
        private readonly CheckBox autoStartBox;
        private readonly System.Windows.Forms.Timer refreshTimer;
        private bool updatingAutoStart;

        internal LauncherForm(string projectRoot)
        {
            this.projectRoot = projectRoot;
            Text = "Investment Auto 启动器";
            ClientSize = new Size(590, 365);
            MinimumSize = new Size(606, 404);
            StartPosition = FormStartPosition.CenterScreen;
            Font = new Font("Microsoft YaHei UI", 10F);
            BackColor = Color.FromArgb(246, 248, 252);
            Icon = SystemIcons.Application;

            var title = new Label
            {
                Text = "Investment Auto",
                Font = new Font("Microsoft YaHei UI", 22F, FontStyle.Bold),
                ForeColor = Color.FromArgb(24, 34, 55),
                AutoSize = true,
                Location = new Point(30, 25)
            };
            var subtitle = new Label
            {
                Text = "AI 自主模拟投资 · Windows 控制中心",
                ForeColor = Color.FromArgb(89, 101, 125),
                AutoSize = true,
                Location = new Point(34, 70)
            };
            statusBadge = new Label
            {
                Text = "正在检查…",
                TextAlign = ContentAlignment.MiddleCenter,
                Font = new Font("Microsoft YaHei UI", 10F, FontStyle.Bold),
                Size = new Size(130, 34),
                Location = new Point(425, 30),
                BackColor = Color.FromArgb(228, 232, 240),
                ForeColor = Color.FromArgb(64, 76, 99)
            };
            detailLabel = new Label
            {
                AutoEllipsis = true,
                ForeColor = Color.FromArgb(69, 81, 105),
                Location = new Point(34, 112),
                Size = new Size(520, 62)
            };

            startButton = MakeButton("启动并打开", new Point(34, 190), Color.FromArgb(39, 98, 232), Color.White);
            stopButton = MakeButton("停止服务", new Point(218, 190), Color.White, Color.FromArgb(171, 45, 54));
            openButton = MakeButton("仅打开页面", new Point(402, 190), Color.White, Color.FromArgb(39, 98, 232));
            startButton.Click += delegate { RunInBackground(true); };
            stopButton.Click += delegate { StopInBackground(); };
            openButton.Click += delegate { LauncherService.OpenDashboard(); };

            var divider = new Panel { BackColor = Color.FromArgb(220, 225, 234), Location = new Point(34, 250), Size = new Size(520, 1) };
            autoStartBox = new CheckBox
            {
                Text = "登录 Windows 后自动启动投资 Agent 与对话服务",
                AutoSize = true,
                Location = new Point(38, 272),
                ForeColor = Color.FromArgb(42, 54, 77)
            };
            autoStartBox.CheckedChanged += AutoStartChanged;
            var hint = new Label
            {
                Text = "配置和账户数据继续保存在项目目录；EXE 不保存 API Key。",
                AutoSize = true,
                Location = new Point(38, 306),
                ForeColor = Color.FromArgb(112, 122, 142),
                Font = new Font("Microsoft YaHei UI", 8.8F)
            };

            Controls.Add(title);
            Controls.Add(subtitle);
            Controls.Add(statusBadge);
            Controls.Add(detailLabel);
            Controls.Add(startButton);
            Controls.Add(stopButton);
            Controls.Add(openButton);
            Controls.Add(divider);
            Controls.Add(autoStartBox);
            Controls.Add(hint);

            refreshTimer = new System.Windows.Forms.Timer { Interval = 3000 };
            refreshTimer.Tick += delegate { RefreshStatus(); };
            Shown += delegate { RefreshStatus(); refreshTimer.Start(); };
        }

        private static Button MakeButton(string text, Point location, Color backColor, Color foreColor)
        {
            return new Button
            {
                Text = text,
                Location = location,
                Size = new Size(152, 42),
                FlatStyle = FlatStyle.Flat,
                BackColor = backColor,
                ForeColor = foreColor,
                Cursor = Cursors.Hand
            };
        }

        private void RefreshStatus()
        {
            var status = LauncherService.GetStatus(projectRoot);
            if (!status.ProjectReady || !status.PythonReady || !status.NodeReady)
            {
                statusBadge.Text = "需要初始化";
                statusBadge.BackColor = Color.FromArgb(255, 235, 203);
                statusBadge.ForeColor = Color.FromArgb(142, 84, 8);
            }
            else if (status.AllRunning)
            {
                statusBadge.Text = "● 运行中";
                statusBadge.BackColor = Color.FromArgb(218, 244, 228);
                statusBadge.ForeColor = Color.FromArgb(21, 116, 64);
            }
            else if (status.AgentRunning || status.ChatRunning)
            {
                statusBadge.Text = "部分运行";
                statusBadge.BackColor = Color.FromArgb(255, 235, 203);
                statusBadge.ForeColor = Color.FromArgb(142, 84, 8);
            }
            else
            {
                statusBadge.Text = "已停止";
                statusBadge.BackColor = Color.FromArgb(228, 232, 240);
                statusBadge.ForeColor = Color.FromArgb(64, 76, 99);
            }
            detailLabel.Text = "投资 Agent：" + (status.AgentRunning ? "运行中" : "未运行")
                + "    对话服务：" + (status.ChatRunning ? "运行中" : "未运行") + Environment.NewLine
                + "项目位置：" + projectRoot;
            startButton.Enabled = status.ProjectReady && status.PythonReady && status.NodeReady;
            stopButton.Enabled = status.AgentRunning || status.ChatRunning;
            openButton.Enabled = status.ChatRunning;
            updatingAutoStart = true;
            autoStartBox.Checked = status.AutoStartEnabled;
            updatingAutoStart = false;
        }

        private void SetBusy(bool busy)
        {
            startButton.Enabled = !busy;
            stopButton.Enabled = !busy;
            openButton.Enabled = !busy;
            statusBadge.Text = busy ? "处理中…" : statusBadge.Text;
        }

        private void RunInBackground(bool openBrowser)
        {
            SetBusy(true);
            ThreadPool.QueueUserWorkItem(delegate
            {
                string message;
                try { message = LauncherService.StartServices(projectRoot, openBrowser); }
                catch (Exception ex) { message = "启动失败：" + ex.Message; }
                BeginInvoke((MethodInvoker)delegate
                {
                    RefreshStatus();
                    SetBusy(false);
                    MessageBox.Show(this, message, LauncherService.ProductName, MessageBoxButtons.OK, MessageBoxIcon.Information);
                });
            });
        }

        private void StopInBackground()
        {
            SetBusy(true);
            ThreadPool.QueueUserWorkItem(delegate
            {
                string message;
                try { message = LauncherService.StopServices(projectRoot); }
                catch (Exception ex) { message = "停止失败：" + ex.Message; }
                Thread.Sleep(1000);
                BeginInvoke((MethodInvoker)delegate
                {
                    RefreshStatus();
                    SetBusy(false);
                    MessageBox.Show(this, message, LauncherService.ProductName, MessageBoxButtons.OK, MessageBoxIcon.Information);
                });
            });
        }

        private void AutoStartChanged(object sender, EventArgs args)
        {
            if (updatingAutoStart) return;
            try
            {
                LauncherService.SetAutoStart(autoStartBox.Checked, projectRoot);
                RefreshStatus();
            }
            catch (Exception ex)
            {
                MessageBox.Show(this, "开机自启动设置失败：" + ex.Message, LauncherService.ProductName,
                    MessageBoxButtons.OK, MessageBoxIcon.Error);
                RefreshStatus();
            }
        }
    }

    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            string explicitProject = null;
            string diagnosticsPath = null;
            var start = false;
            var stop = false;
            var minimized = false;
            var enableAutoStart = false;
            var disableAutoStart = false;
            for (var index = 0; index < args.Length; index++)
            {
                var value = args[index];
                if (value == "--project" && index + 1 < args.Length) explicitProject = args[++index];
                else if (value.StartsWith("--project=")) explicitProject = value.Substring(10);
                else if (value == "--diagnostics" && index + 1 < args.Length) diagnosticsPath = args[++index];
                else if (value.StartsWith("--diagnostics=")) diagnosticsPath = value.Substring(14);
                else if (value == "--start") start = true;
                else if (value == "--stop") stop = true;
                else if (value == "--minimized") minimized = true;
                else if (value == "--enable-autostart") enableAutoStart = true;
                else if (value == "--disable-autostart") disableAutoStart = true;
            }

            var projectRoot = LauncherService.ResolveProjectRoot(explicitProject);
            try
            {
                if (diagnosticsPath != null)
                {
                    LauncherService.WriteDiagnostics(diagnosticsPath, projectRoot);
                    return;
                }
                if (enableAutoStart) LauncherService.SetAutoStart(true, projectRoot);
                if (disableAutoStart) LauncherService.SetAutoStart(false, projectRoot);
                if ((enableAutoStart || disableAutoStart) && !start && !stop) return;
                if (stop)
                {
                    LauncherService.StopServices(projectRoot);
                    return;
                }
                if (start)
                {
                    var result = LauncherService.StartServices(projectRoot, !minimized);
                    if (minimized || result.IndexOf("失败", StringComparison.OrdinalIgnoreCase) < 0) return;
                    MessageBox.Show(result, LauncherService.ProductName, MessageBoxButtons.OK, MessageBoxIcon.Warning);
                    return;
                }
            }
            catch (Exception ex)
            {
                if (minimized) return;
                MessageBox.Show(ex.Message, LauncherService.ProductName, MessageBoxButtons.OK, MessageBoxIcon.Error);
            }

            Application.EnableVisualStyles();
            Application.SetCompatibleTextRenderingDefault(false);
            Application.Run(new LauncherForm(projectRoot));
        }
    }
}
