using System;
using System.IO.Pipes;
using System.Threading;

namespace InvestmentAuto.Desktop.Services;

/// <summary>
/// Single-instance guard.  A second launch signals the running instance
/// through a named pipe (to restore the window) and then exits.
/// </summary>
internal static class SingleInstance
{
    internal const string MutexName = "InvestmentAuto.Desktop.Singleton";
    internal const string PipeName = "InvestmentAuto.Desktop.Pipe";

    public static bool TryAcquire(out Mutex mutex)
        => TryAcquire(MutexName, out mutex);

    internal static bool TryAcquire(string mutexName, out Mutex mutex)
    {
        mutex = new Mutex(true, mutexName, out bool createdNew);
        return createdNew;
    }

    public static void SignalExisting()
    {
        try
        {
            using var client = new NamedPipeClientStream(".", PipeName, PipeDirection.Out);
            client.Connect(300);
            var bytes = System.Text.Encoding.UTF8.GetBytes("show");
            client.Write(bytes, 0, bytes.Length);
        }
        catch
        {
            // The running instance may not be listening yet; ignore.
        }
    }
}
