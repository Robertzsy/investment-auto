using InvestmentAuto.Desktop.Services;
using Microsoft.Win32;
using Xunit;

namespace InvestmentAuto.Desktop.Tests;

/// <summary>Autostart toggle: HKCU Run key round-trip, non-destructive to any
/// pre-existing value (e.g. the legacy project's own entry).</summary>
public class AutoStartTests : IDisposable
{
    private const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string ValueName = "InvestmentAuto";
    private readonly string? _original;

    public AutoStartTests()
    {
        using var key = Registry.CurrentUser.OpenSubKey(RunKeyPath);
        _original = key?.GetValue(ValueName) as string;
    }

    public void Dispose()
    {
        using var key = Registry.CurrentUser.OpenSubKey(RunKeyPath, writable: true);
        if (key == null) return;
        if (_original == null) key.DeleteValue(ValueName, throwOnMissingValue: false);
        else key.SetValue(ValueName, _original);
    }

    [Fact]
    public void Enable_Disable_RoundTrip()
    {
        AutoStart.Disable();
        Assert.False(AutoStart.IsEnabled());

        AutoStart.Enable(@"C:\Temp\App\InvestmentAuto.Desktop.exe", @"C:\Temp\App", @"C:\Temp\Data");
        Assert.True(AutoStart.IsEnabled());

        using var key = Registry.CurrentUser.OpenSubKey(RunKeyPath);
        var command = Assert.IsType<string>(key?.GetValue(ValueName));
        Assert.Contains("InvestmentAuto.Desktop.exe", command);
        Assert.Contains("--app-root", command);
        Assert.Contains("--data-root", command);
        Assert.Contains("--autostart", command);

        AutoStart.Disable();
        Assert.False(AutoStart.IsEnabled());
    }

    [Fact]
    public void Disable_WhenAbsent_IsNoOp()
    {
        AutoStart.Disable();
        Assert.False(AutoStart.IsEnabled());
        AutoStart.Disable(); // still no throw
    }

    [Fact]
    public void IsEnabled_IgnoresForeignEntries()
    {
        // The legacy project reused the same Run-key value name with a
        // different command line; that must not read as our autostart.
        using var key = Registry.CurrentUser.CreateSubKey(RunKeyPath);
        key.SetValue(ValueName, "\"D:\\old\\InvestmentAuto.exe\" --start --minimized");

        Assert.False(AutoStart.IsEnabled());
    }
}
