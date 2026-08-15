using System;
using Microsoft.Win32;

namespace InvestmentAuto.Desktop.Services;

/// <summary>Per-user "start with Windows" toggle (HKCU Run key, no admin).</summary>
internal static class AutoStart
{
    private const string RunKeyPath = @"Software\Microsoft\Windows\CurrentVersion\Run";
    private const string ValueName = "InvestmentAuto";

    public static bool IsEnabled()
    {
        // Only our own entry counts: the legacy project used the same value
        // name, so a foreign command must not read as "enabled".
        var command = ReadCommand();
        return command is not null
            && command.Contains("InvestmentAuto.Desktop.exe", StringComparison.OrdinalIgnoreCase);
    }

    public static void Enable(string exePath, string appRoot, string dataRoot)
    {
        var command = $"\"{exePath}\" --app-root \"{appRoot}\" --data-root \"{dataRoot}\" --autostart";
        using var key = Registry.CurrentUser.CreateSubKey(RunKeyPath);
        key.SetValue(ValueName, command, RegistryValueKind.String);
    }

    public static void Disable()
    {
        using var key = Registry.CurrentUser.OpenSubKey(RunKeyPath, writable: true);
        key?.DeleteValue(ValueName, throwOnMissingValue: false);
    }

    private static string? ReadCommand()
    {
        try
        {
            using var key = Registry.CurrentUser.OpenSubKey(RunKeyPath);
            return key?.GetValue(ValueName) as string;
        }
        catch { return null; }
    }
}
