using System.Text.RegularExpressions;
using System.Windows;
using System.Windows.Input;
using Launcher.Services;

namespace Launcher.Views;

public partial class RegisterWindow : Window
{
    private readonly ConfigService _config;

    public RegisterWindow()
    {
        InitializeComponent();
        _config = new ConfigService();
    }

    private async void RegisterBtn_Click(object sender, RoutedEventArgs e)
    {
        var username = UsernameBox.Text.Trim();
        var email = EmailBox.Text.Trim();
        var password = PasswordBox.Password;
        var referral = ReferralBox.Text.Trim();

        if (string.IsNullOrEmpty(username) || string.IsNullOrEmpty(email) || string.IsNullOrEmpty(password))
        {
            ShowError("请填写所有必填项");
            return;
        }
        // 用户名兼容手机号（11 位数字）与普通账号名；号段合法性由后端校验，前端不做严格号段拦截。
        if (!Regex.IsMatch(username, @"^1[3-9]\d{9}$") &&
            !Regex.IsMatch(username, @"^[A-Za-z0-9_]{3,32}$"))
        {
            ShowError("请输入 11 位手机号或 3-32 位字母/数字/下划线账号");
            return;
        }
        if (!Regex.IsMatch(email, @"^[^@\s]+@[^@\s]+\.[^@\s]+$"))
        {
            ShowError("请输入有效邮箱地址");
            return;
        }
        if (password.Length < 6)
        {
            ShowError("密码至少6位");
            return;
        }

        RegisterBtn.IsEnabled = false;
        ErrorText.Visibility = Visibility.Collapsed;

        try
        {
            var api = new AuthApiClient(_config.Config.ApiBaseUrl);
            await api.RegisterAsync(username, email, password,
                string.IsNullOrEmpty(referral) ? null : referral);

            MessageBox.Show("注册成功，请登录", "提示", MessageBoxButton.OK, MessageBoxImage.Information);
            var login = new LoginWindow();
            login.Show();
            Close();
        }
        catch (Exception ex)
        {
            ShowError(ex.Message);
        }
        finally
        {
            RegisterBtn.IsEnabled = true;
        }
    }

    private void BackToLogin_Click(object sender, MouseButtonEventArgs e)
    {
        var login = new LoginWindow();
        login.Show();
        Close();
    }

    private void ShowError(string msg)
    {
        ErrorText.Text = msg;
        ErrorText.Visibility = Visibility.Visible;
    }
}
