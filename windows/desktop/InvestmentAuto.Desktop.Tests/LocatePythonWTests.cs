using System.IO;
using InvestmentAuto.Desktop.Services;
using Xunit;

namespace InvestmentAuto.Desktop.Tests;

/// <summary>Test requirement #6: bundled-python resolution order.</summary>
public class LocatePythonWTests
{
    [Fact]
    public void PrefersBundledPythonOverVenv()
    {
        using var root = new TempDir();
        var bundled = root.File("python", "pythonw.exe");
        var venv = root.File(".venv", "Scripts", "pythonw.exe");

        Assert.Equal(bundled, ProcessManager.LocatePythonW(root.Path));
    }

    [Fact]
    public void FallsBackToVenv()
    {
        using var root = new TempDir();
        var venv = root.File(".venv", "Scripts", "pythonw.exe");

        Assert.Equal(venv, ProcessManager.LocatePythonW(root.Path));
    }

    [Fact]
    public void FallsBackToPath()
    {
        using var root = new TempDir();
        var pathDir = Path.Combine(root.Path, "pathbin");
        Directory.CreateDirectory(pathDir);
        var onPath = Path.Combine(pathDir, "pythonw.exe");
        File.WriteAllText(onPath, "");

        var old = Environment.GetEnvironmentVariable("PATH");
        try
        {
            Environment.SetEnvironmentVariable("PATH", pathDir);
            Assert.Equal(onPath, ProcessManager.LocatePythonW(root.Path));
        }
        finally
        {
            Environment.SetEnvironmentVariable("PATH", old);
        }
    }

    [Fact]
    public void NoPythonAnywhere_Throws()
    {
        using var root = new TempDir();
        var old = Environment.GetEnvironmentVariable("PATH");
        try
        {
            Environment.SetEnvironmentVariable("PATH", "");
            Assert.Throws<FileNotFoundException>(() => ProcessManager.LocatePythonW(root.Path));
        }
        finally
        {
            Environment.SetEnvironmentVariable("PATH", old);
        }
    }

    private sealed class TempDir : IDisposable
    {
        public string Path { get; } =
            System.IO.Path.Combine(System.IO.Path.GetTempPath(), "ia-tests-" + Guid.NewGuid().ToString("N"));

        public TempDir() => Directory.CreateDirectory(Path);

        public string File(params string[] relativeParts)
        {
            var full = System.IO.Path.Combine(new[] { Path }.Concat(relativeParts).ToArray());
            Directory.CreateDirectory(System.IO.Path.GetDirectoryName(full)!);
            System.IO.File.WriteAllText(full, "");
            return full;
        }

        public void Dispose()
        {
            try { Directory.Delete(Path, recursive: true); } catch { }
        }
    }
}
