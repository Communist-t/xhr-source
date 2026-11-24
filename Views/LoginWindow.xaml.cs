using System.Windows;
using System.Windows.Input;
using Launcher.Models;
using Launcher.Services;

namespace Launcher.Views;

public partial class LoginWindow : Window
{
    private readonly ConfigService _config;
    private readonly SessionStore _sessionStore;

    public LoginWindow()
    {
        InitializeComponent();
        _config = new ConfigService();
        _sessionStore = new SessionStore();
    }

    private async void LoginBtn_Click(object sender, RoutedEventArgs e)
    {
        var username = UsernameBox.Text.Trim();
        var password = PasswordBox.Password;

        if (string.IsNullOrEmpty(username) || string.IsNullOrEmpty(password))
        {
            ShowError("请输入用户名和密码");
            return;
        }

        LoginBtn.IsEnabled = false;
        ErrorText.Visibility = Visibility.Collapsed;

        try
        {
            var api = new AuthApiClient(_config.Config.ApiBaseUrl);
            var token = await api.LoginAsync(username, password);

            // 登录成功后拉全量用户信息
            var session = await api.GetCurrentUserAsync(token);

            // 再拉 api_key
            try
            {
                session.ApiKey = await api.GetApiKeyAsync(token);
            }
            catch
            {
                // api_key 拉失败不阻断登录
            }

            session.LoginAt = DateTime.UtcNow.ToString("o");
            session.ApiBaseUrl = _config.Config.ApiBaseUrl;

            // 写入 session.json 供其他 exe 读取
            _sessionStore.Save(session);

            // 登录后立即把账号 ApiKey 同步到各子程序的 API 配置文件
            new UserConfigSyncService().SyncAll(_config.Config.Apps, session);

            // 打开主窗口
            var main = new MainWindow(session);
            main.Show();
            Close();
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
        finally
        {
            LoginBtn.IsEnabled = true;
        }
    }

    private void RegisterLink_Click(object sender, MouseButtonEventArgs e)
    {
        var reg = new RegisterWindow();
        reg.Show();
        Close();
    }

    private void ShowError(string msg)
    {
        ErrorText.Text = msg;
        ErrorText.Visibility = Visibility.Visible;
    }
}
