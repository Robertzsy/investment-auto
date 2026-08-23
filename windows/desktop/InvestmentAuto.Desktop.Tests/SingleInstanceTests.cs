using System.Threading;
using InvestmentAuto.Desktop.Services;
using Xunit;

namespace InvestmentAuto.Desktop.Tests;

/// <summary>Test requirement #2: single-instance guard.</summary>
public class SingleInstanceTests
{
    [Fact]
    public void TryAcquire_WhenFree_AcquiresAndCanRelease()
    {
        var name = SingleInstance.MutexName + ".Tests." + Guid.NewGuid().ToString("N");
        Assert.True(SingleInstance.TryAcquire(name, out var mutex));
        try
        {
            mutex.ReleaseMutex();
            mutex.Dispose();

            // Re-acquiring after release must succeed again.
            Assert.True(SingleInstance.TryAcquire(name, out var again));
            again.ReleaseMutex();
            again.Dispose();
        }
        catch
        {
            mutex.Dispose();
            throw;
        }
    }

    [Fact]
    public void TryAcquire_WhileHeld_ReturnsFalse()
    {
        var name = SingleInstance.MutexName + ".Tests." + Guid.NewGuid().ToString("N");
        Assert.True(SingleInstance.TryAcquire(name, out var first));
        try
        {
            Assert.False(SingleInstance.TryAcquire(name, out var second));
            // second refers to the existing mutex without ownership; just dispose it.
            second?.Dispose();
        }
        finally
        {
            first.ReleaseMutex();
            first.Dispose();
        }
    }

    [Fact]
    public void SignalExisting_WithoutListener_DoesNotThrow()
    {
        // No named-pipe listener is running; the call must be swallowed internally
        // (a second launch must never crash).
        SingleInstance.SignalExisting();
    }
}
