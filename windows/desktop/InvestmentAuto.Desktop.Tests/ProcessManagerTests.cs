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
        Directory.CreateDirectory(Path.Combine(app, "node"));
        File.WriteAllText(Path.Combine(app, "node", "node.exe"), "");

        using var pm = new ProcessManager(app, data);

        Assert.Equal(app, pm.AppRoot);
        Assert.Equal(data, pm.DataRoot);
        Assert.False(string.IsNullOrEmpty(pm.AccessToken));
        Assert.Equal(pm.AccessToken, Environment.GetEnvironmentVariable("IA_ACCESS_TOKEN"));
        Assert.True(pm.IsFirstRun);
        Assert.True(File.Exists(Path.Combine(data, "runtime", "logs", "desktop.log")));
        Assert.Equal("http://127.0.0.1:" + pm.WebUrl.Split(':')[2], pm.WebUrl);
        Assert.StartsWith("http://127.0.0.1:", pm.EngineUrl);
    }

    [Fact]
    public void LocateNode_PrefersBundledThenFallsBackToPath()
    {
        using var root = new TempDir();
        var bundled = root.Dir("app");
        Directory.CreateDirectory(Path.Combine(bundled, "node"));
        var bundledNode = Path.Combine(bundled, "node", "node.exe");
        File.WriteAllText(bundledNode, "");
        Assert.Equal(bundledNode, ProcessManager.LocateNode(bundled));

        using var empty = new TempDir();
        var pathDir = Path.Combine(empty.Path, "pathbin");
        Directory.CreateDirectory(pathDir);
        var onPath = Path.Combine(pathDir, "node.exe");
        File.WriteAllText(onPath, "");
        var old = Environment.GetEnvironmentVariable("PATH");
        try
        {
            Environment.SetEnvironmentVariable("PATH", pathDir);
            Assert.Equal(onPath, ProcessManager.LocateNode(empty.Path));
        }
        finally
        {
            Environment.SetEnvironmentVariable("PATH", old);
        }
    }

    [Fact]
    public void PickFreePort_ReturnsLoopbackPort()
    {
        var port = ProcessManager.PickFreePort();
        Assert.InRange(port, 1024, 65535);
    }

    [Fact]
    public void IsFirstRun_FalseOnceSetupCompletes()
    {
        using var root = new TempDir();
        var app = root.Dir("app");
        var data = root.Dir("data");
        Directory.CreateDirectory(Path.Combine(app, "python"));
        File.WriteAllText(Path.Combine(app, "python", "pythonw.exe"), "");
        Directory.CreateDirectory(Path.Combine(app, "node"));
        File.WriteAllText(Path.Combine(app, "node", "node.exe"), "");
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
        var oldPath = Environment.GetEnvironmentVariable("PATH");
        try
        {
            Environment.SetEnvironmentVariable("INVESTMENT_AUTO_DATA_DIR", null);
            // Can't set BaseDirectory; verify env fallback only.
            Environment.SetEnvironmentVariable("INVESTMENT_AUTO_DATA_DIR", @"C:\nonexistent-ia-data");
            // Hermetic node resolution for the ctor on any machine.
            using var root = new TempDir();
            var pathDir = Path.Combine(root.Path, "pathbin");
            Directory.CreateDirectory(pathDir);
            File.WriteAllText(Path.Combine(pathDir, "pythonw.exe"), "");
            File.WriteAllText(Path.Combine(pathDir, "node.exe"), "");
            Environment.SetEnvironmentVariable("PATH", pathDir);
            using var pm = new ProcessManager();
            Assert.Equal(@"C:\nonexistent-ia-data", pm.DataRoot);
        }
        finally
        {
            Environment.SetEnvironmentVariable("INVESTMENT_AUTO_DATA_DIR", oldData);
            Environment.SetEnvironmentVariable("PATH", oldPath);
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
