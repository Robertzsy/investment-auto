using System;
using System.Drawing;
using System.Windows.Forms;

namespace InvestmentAuto.Desktop.Services;

/// <summary>System tray icon with the full management menu.</summary>
internal sealed class TrayIcon : IDisposable
{
    private readonly NotifyIcon _icon;
    private readonly Action _onOpen;
    private readonly Action _onStart;
    private readonly Action _onPause;
    private readonly Action _onViewLogs;
    private readonly Action _onExit;

    public TrayIcon(Action onOpen, Action onStart, Action onPause, Action onViewLogs, Action onExit)
    {
        _onOpen = onOpen;
        _onStart = onStart;
        _onPause = onPause;
        _onViewLogs = onViewLogs;
        _onExit = onExit;

        var menu = new ContextMenuStrip();
        menu.Items.Add("打开主窗口", null, (_, _) => _onOpen());
        menu.Items.Add("启动服务", null, (_, _) => _onStart());
        menu.Items.Add("暂停投资", null, (_, _) => _onPause());
        menu.Items.Add("查看日志", null, (_, _) => _onViewLogs());
        menu.Items.Add(new ToolStripSeparator());
        menu.Items.Add("退出", null, (_, _) => _onExit());

        _icon = new NotifyIcon
        {
            Text = "Investment Auto - AI 自主模拟投资",
            Icon = SystemIcons.Application,
            ContextMenuStrip = menu,
            Visible = true,
        };
        _icon.DoubleClick += (_, _) => _onOpen();
    }

    public void ShowMinimizedBalloon()
    {
        _icon.BalloonTipTitle = "Investment Auto";
        _icon.BalloonTipText = "已最小化到托盘，后台自动投资继续运行。";
        _icon.ShowBalloonTip(2500);
    }

    public void Dispose() => _icon.Dispose();
}
