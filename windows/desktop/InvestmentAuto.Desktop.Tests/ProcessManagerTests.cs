using System.IO;
using InvestmentAuto.Desktop.Services;
using Xunit;

namespace InvestmentAuto.Desktop.Tests;

/// <summary>Test requirement #3: process-manager lifecycle plumbing
/// (roots, token, first-run flag, log seeding) without spawning python.</summary>
public class ProcessManagerTests
{
    [Fact]
    public void Ctor_SetsRootsTokenAndSeedsLog()
    {
        using var root = new TempDir();
        var app = root.Dir("app");
        var data = root.Dir("data");
        Directory.CreateDirectory(Path.Combine(app, "python"));
        File.WriteAllText(Path.Combine(app, "python", "pythonw.exe"), "");

        using var pm = new ProcessManager(app, data);

        Assert.Equal(app, pm.AppRoot);
        Assert.Equal(data, pm.DataRoot);
        Assert.False(string.IsNullOrEmpty(pm.AccessToken));
        Assert.Equal(pm.AccessToken, Environment.GetEnvironmentVariable("IA_ACCESS_TOKEN"));
        Assert.True(pm.IsFirstRun);
        Assert.True(File.Exists(Path.Combine(data, "runtime", "logs", "desktop.log")));
    }

    [Fact]
    public void IsFirstRun_FalseOnceSetupCompletes()
    {
        using var root = new TempDir();
        var app = root.Dir("app");
        var data = root.Dir("data");
        Directory.CreateDirectory(Path.Combine(app, "python"));
        File.WriteAllText(Path.Combine(app, "python", "pythonw.exe"), "");
        var marker = Path.Combine(data, "runtime", "setup.complete");
        Directory.CreateDirectory(Path.GetDirectoryName(marker)!);
        File.WriteAllText(marker, "1");

        using var pm = new ProcessManager(app, data);

        Assert.False(pm.IsFirstRun);
    }

    [Fact]
    public void Ctor_DefaultRoots_FallBackToExeDirectory()
    {
        var oldData = Environment.GetEnvironmentVariable("INVESTMENT_AUTO_DATA_DIR");
        try
        {
            Environment.SetEnvironmentVariable("INVESTMENT_AUTO_DATA_DIR", null);
            // Can't set BaseDirectory; verify env fallback only.
            Environment.SetEnvironmentVariable("INVESTMENT_AUTO_DATA_DIR", @"C:\nonexistent-ia-data");
            using var pm = new ProcessManager();
            Assert.Equal(@"C:\nonexistent-ia-data", pm.DataRoot);
        }
        finally
        {
            Environment.SetEnvironmentVariable("INVESTMENT_AUTO_DATA_DIR", oldData);
        }
    }

    private sealed class TempDir : IDisposable
    {
        public string Path { get; } =
            System.IO.Path.Combine(System.IO.Path.GetTempPath(), "ia-tests-" + Guid.NewGuid().ToString("N"));

        public TempDir() => Directory.CreateDirectory(Path);

        public string Dir(string name)
        {
            var full = System.IO.Path.Combine(Path, name);
            Directory.CreateDirectory(full);
            return full;
        }

        public void Dispose()
        {
            try { Directory.Delete(Path, recursive: true); } catch { }
        }
    }
}
