using System;
using System.IO;
using Microsoft.Win32;

namespace InvestmentAuto.Desktop.Services;

/// <summary>
/// Resolves the installed WebView2 Evergreen runtime folder explicitly.
///
/// The loader normally discovers the runtime through the registry view
/// matching the app's bitness, but some machines register the runtime only
/// in the 32-bit view (WOW6432Node) while our app is x64 - default discovery
/// then finds nothing and environment creation can hang forever. We check
/// both registry views plus the per-user install location and verify that
/// msedgewebview2.exe actually exists.
/// </summary>
internal static class WebView2Locator
{
    private const string ClientKeyPath =
        @"Software\Microsoft\EdgeUpdate\Clients\{F3017226-FE2A-4295-8BDF-00C3A9A7E4C5}";

    /// <summary>Returns the folder containing msedgewebview2.exe, or null when
    /// no usable runtime can be located.</summary>
    public static string? FindRuntimeFolder()
    {
        var folders = new System.Collections.Generic.List<string>();

        // Machine-wide installs: both registry views.
        try
        {
            foreach (var view in new[] { RegistryView.Registry64, RegistryView.Registry32 })
            {
                using var baseKey = RegistryKey.OpenBaseKey(RegistryHive.LocalMachine, view);
                using var client = baseKey.OpenSubKey(ClientKeyPath);
                folders.Add(BuildCandidate(client));
            }
        }
        catch { }

        // Per-user install.
        try
        {
            using var client = Registry.CurrentUser.OpenSubKey(ClientKeyPath);
            folders.Add(BuildCandidate(client));
        }
        catch { }

        foreach (var folder in folders)
        {
            if (string.IsNullOrEmpty(folder)) continue;
            try
            {
                if (File.Exists(Path.Combine(folder, "msedgewebview2.exe"))) return folder;
            }
            catch { }
        }
        return null;
    }

    private static string BuildCandidate(RegistryKey? client)
    {
        var pv = client?.GetValue("pv") as string;
        if (string.IsNullOrEmpty(pv)) return "";
        var location = client?.GetValue("location") as string;
        if (string.IsNullOrEmpty(location))
        {
            location = Path.Combine(
                Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86),
                "Microsoft", "EdgeWebView", "Application");
        }
        return Path.Combine(location, pv);
    }
}
