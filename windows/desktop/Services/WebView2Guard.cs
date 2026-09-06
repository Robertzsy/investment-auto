using System;
using System.Diagnostics;
using System.Management;

namespace InvestmentAuto.Desktop.Services;

/// <summary>
/// Recovers from orphaned WebView2 browser processes. When the desktop shell
/// is hard-killed (Task Manager, crash, test teardown), its msedgewebview2.exe
/// browser group can survive and keep holding the user-data-folder lock, which
/// makes the next EnsureCoreWebView2Async wait indefinitely.
///
/// The guard only runs after the single-instance mutex is acquired, so no live
/// copy of this app can exist at that point; any msedgewebview2.exe whose
/// command line references our app (its user-data folder lives under the
/// InvestmentAuto data root) is therefore an orphan and safe to kill.
/// Unrelated WebView2 hosts (Windows Search, other apps) never match.
/// </summary>
internal static class WebView2Guard
{
    /// <summary>Kills orphaned browser groups belonging to this app.
    /// Returns the number of root processes killed.</summary>
    public static int KillOrphanedBrowsers()
    {
        var killed = 0;
        try
        {
            using var searcher = new ManagementObjectSearcher(
                "SELECT ProcessId, CommandLine FROM Win32_Process WHERE Name = 'msedgewebview2.exe'");
            foreach (ManagementObject mo in searcher.Get())
            {
                try
                {
                    var commandLine = mo["CommandLine"] as string;
                    if (commandLine == null
                        || !commandLine.Contains("InvestmentAuto", StringComparison.OrdinalIgnoreCase))
                        continue;

                    var pid = Convert.ToInt32(mo["ProcessId"]);
                    using var process = Process.GetProcessById(pid);
                    process.Kill(entireProcessTree: true);
                    killed++;
                }
                catch
                {
                    // process already gone or inaccessible; keep scanning
                }
            }
        }
        catch
        {
            // WMI unavailable: proceed without cleanup (best effort)
        }
        return killed;
    }
}
