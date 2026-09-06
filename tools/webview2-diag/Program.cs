// Minimal WebView2 diagnostic: isolates environment/loader issues from the
// WPF shell. Run it with a plain WinForms host window and log every step.
using System;
using System.IO;
using System.Threading.Tasks;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;

internal static class Program
{
    private static readonly string LogPath = Path.Combine(Path.GetTempPath(), "wv2-diag.log");

    private static void Log(string msg)
    {
        var line = DateTime.Now.ToString("HH:mm:ss.fff") + " " + msg;
        File.AppendAllText(LogPath, line + Environment.NewLine);
        Console.WriteLine(line);
    }

    [STAThread]
    private static async Task<int> Main(string[] args)
    {
        Log("start pid=" + Environment.ProcessId);

        var folder = args.Length > 0 && Directory.Exists(args[0]) ? args[0] : null;
        Log("browserExecutableFolder=" + (folder ?? "(default)"));

        var form = new Form { Width = 800, Height = 600 };
        form.Show();
        Application.DoEvents();
        Log("form shown, handle=" + form.Handle);

        // Experiment A: CreateAsync on the STA main thread (form already shown).
        try
        {
            var env = await CoreWebView2Environment.CreateAsync(folder, null, null);
            Log("A: CreateAsync OK. BrowserVersionString=" + env.BrowserVersionString);
            var ctrl = await env.CreateCoreWebView2ControllerAsync(form.Handle);
            Log("A: CreateControllerAsync OK");
        }
        catch (Exception ex)
        {
            Log("A: EXCEPTION: " + ex.GetType().Name + " (0x" + ((ex as System.Runtime.InteropServices.COMException)?.HResult ?? ex.HResult).ToString("X8") + "): " + ex.Message);
        }

        // Experiment B: CreateAsync on a threadpool (MTA) thread.
        try
        {
            var envB = await Task.Run(() => CoreWebView2Environment.CreateAsync(folder, null, null));
            Log("B: CreateAsync OK. BrowserVersionString=" + envB.BrowserVersionString);
            var ctrlB = await envB.CreateCoreWebView2ControllerAsync(form.Handle);
            Log("B: CreateControllerAsync OK");
        }
        catch (Exception ex)
        {
            Log("B: EXCEPTION: " + ex.GetType().Name + " (0x" + ((ex as System.Runtime.InteropServices.COMException)?.HResult ?? ex.HResult).ToString("X8") + "): " + ex.Message);
        }

        // Experiment C: default discovery (no explicit folder).
        try
        {
            var envC = await CoreWebView2Environment.CreateAsync(null, null, null);
            Log("C: CreateAsync OK. BrowserVersionString=" + envC.BrowserVersionString);
            var ctrlC = await envC.CreateCoreWebView2ControllerAsync(form.Handle);
            Log("C: CreateControllerAsync OK");
        }
        catch (Exception ex)
        {
            Log("C: EXCEPTION: " + ex.GetType().Name + " (0x" + ((ex as System.Runtime.InteropServices.COMException)?.HResult ?? ex.HResult).ToString("X8") + "): " + ex.Message);
        }

        Log("end");
        return 0;
    }
}
