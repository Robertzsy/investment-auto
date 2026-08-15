using InvestmentAuto.Desktop.Services;
using Xunit;

namespace InvestmentAuto.Desktop.Tests;

/// <summary>Test requirement #5: chat.ready.json parsing.</summary>
public class ChatReadyParseTests
{
    [Fact]
    public void TryParse_ValidJson_ReturnsFields()
    {
        const string json =
            "{\"host\":\"127.0.0.1\",\"port\":2234,\"token\":\"tok123\"," +
            "\"url\":\"http://localhost:2234\",\"pid\":12345," +
            "\"started_at\":\"2026-08-15T21:19:43+08:00\"}";

        var ready = ChatReady.TryParse(json);

        Assert.NotNull(ready);
        Assert.Equal("127.0.0.1", ready!.Host);
        Assert.Equal(2234, ready.Port);
        Assert.Equal("tok123", ready.Token);
        Assert.Equal("http://localhost:2234", ready.Url);
        Assert.Equal(12345, ready.Pid);
        Assert.Equal("2026-08-15T21:19:43+08:00", ready.StartedAt);
    }

    [Fact]
    public void TryParse_MalformedJson_ReturnsNull()
    {
        Assert.Null(ChatReady.TryParse("{definitely not json"));
        Assert.Null(ChatReady.TryParse(""));
    }

    [Fact]
    public void TryParse_ZeroOrMissingPort_ReturnsNull()
    {
        Assert.Null(ChatReady.TryParse("{\"token\":\"tok\"}"));
        Assert.Null(ChatReady.TryParse("{\"port\":0}"));
    }

    [Fact]
    public void TryParse_UnknownExtraFields_AreIgnored()
    {
        const string json =
            "{\"host\":\"127.0.0.1\",\"port\":8080,\"token\":\"t\"," +
            "\"url\":\"http://127.0.0.1:8080\",\"pid\":1,\"started_at\":\"s\"," +
            "\"extra\":{\"nested\":true},\"more\":42}";

        var ready = ChatReady.TryParse(json);

        Assert.NotNull(ready);
        Assert.Equal(8080, ready!.Port);
    }
}
