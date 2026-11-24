using System.Windows;
using Launcher.Models;
using Launcher.Services;
using Launcher.Views;

namespace Launcher;

public partial class App : Application
{
    protected override void OnStartup(StartupEventArgs e)
    {
        base.OnStartup(e);

        // 清理上一轮未完成的更新暂存目录，避免残留旧包占用空间。
        try { Services.UpdateService.CleanupStagingStatic(); } catch { }

        var sessionStore = new SessionStore();
        var session = sessionStore.Load();

        if (session != null && !string.IsNullOrEmpty(session.AccessToken))
        {
            // 已有登录态，直接进主窗口
            var main = new MainWindow(session);
            main.Show();
        }
        else
        {
            var login = new LoginWindow();
            login.Show();
        }
    }
}
