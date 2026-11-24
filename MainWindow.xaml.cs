using System.IO;
using System.Windows;
using System.Windows.Threading;
using Launcher.Models;
using Launcher.ViewModels;
using Launcher.Views;

namespace Launcher;

public partial class MainWindow : Window
{
    private readonly MainViewModel _vm;

    /// <summary>新手引导步骤：Target 为空表示居中展示，否则高亮对应命名区域。</summary>
    private static readonly (string Title, string Text, string Target)[] GuideSteps =
    {
        ("欢迎使用 xHR 数字员工工作台",
         "这里集中管理你的数字员工：本地启动、云端技能、账号钱包与程序更新。接下来用几步带你熟悉每个功能区。",
         ""),
        ("页面导航",
         "「员工」页管理本地数字员工；「技能广场」用于发现更多员工技能（即将开放）。",
         "NavSection"),
        ("我创建的云端员工",
         "这里是你在后台创建的云端员工，列表每 3 秒自动刷新。点击某个员工可一键启动其对应的本地员工。",
         "SkillScroll"),
        ("我的员工",
         "每张卡片对应一位本地数字员工：点击「开始工作」启动、「停止」结束运行；矩阵视频等子应用物料会显示下载进度；「x.HR 完整版」卡片可下载并安装完整客户端。",
         "AppsScroll"),
        ("设置与更新",
         "进入设置页可查看账号信息、钱包余额与充值记录，检查程序新版本、查看关于信息。发现新版本时点「立即更新」即可自动下载安装。",
         "NavSettingsBtn"),
        ("当前账号",
         "这里显示你的登录账号与角色。需要换号时可在设置页底部「退出登录」。引导到此结束，祝使用愉快！",
         "UserCard"),
    };

    private int _guideStep = -1;

    private static string GuideMarkerPath => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "xHR", "guide-seen.flag");

    public MainWindow(UserSession session)
    {
        InitializeComponent();
        _vm = new MainViewModel(session);
        DataContext = _vm;

        // 首屏滚动到员工列表顶部，避免初始化时被可聚焦元素带到下方卡片。
        Loaded += (_, _) =>
        {
            AppsScroll.ScrollToTop();
            SkillScroll.ScrollToTop();

            // 首次启动展示新手引导；完成或跳过后写标记文件，不再重复。
            if (!File.Exists(GuideMarkerPath))
            {
                GuideOverlay.Visibility = Visibility.Visible;
                Dispatcher.BeginInvoke(DispatcherPriority.Loaded, () => ShowGuideStep(0));
            }
        };

        SizeChanged += (_, _) =>
        {
            if (_guideStep >= 0) ShowGuideStep(_guideStep);
        };
    }

    private void ShowGuideStep(int index)
    {
        _guideStep = index;
        var step = GuideSteps[index];

        GuideStepNo.Text = $"{index + 1} / {GuideSteps.Length}";
        GuideTitle.Text = step.Title;
        GuideText.Text = step.Text;
        GuidePrevBtn.Visibility = index == 0 ? Visibility.Collapsed : Visibility.Visible;
        GuideNextBtn.Content = index == GuideSteps.Length - 1 ? "开始使用" : "下一步";

        var overlayW = GuideOverlay.ActualWidth;
        var overlayH = GuideOverlay.ActualHeight;
        GuideFullGeo.Rect = new Rect(0, 0, overlayW, overlayH);

        var target = string.IsNullOrEmpty(step.Target)
            ? null
            : FindName(step.Target) as FrameworkElement;

        if (target == null || target.ActualWidth <= 0)
        {
            // 无目标（欢迎/兜底）：全屏遮罩 + 居中卡片
            GuideHoleGeo.Rect = Rect.Empty;
            GuideHalo.Visibility = Visibility.Collapsed;
            GuideCard.HorizontalAlignment = HorizontalAlignment.Center;
            GuideCard.VerticalAlignment = VerticalAlignment.Center;
            GuideCard.Margin = new Thickness(0);
            return;
        }

        var b = target.TransformToVisual(GuideOverlay).TransformBounds(
            new Rect(0, 0, target.ActualWidth, target.ActualHeight));
        var hole = new Rect(b.X - 6, b.Y - 6, b.Width + 12, b.Height + 12);
        GuideHoleGeo.Rect = hole;

        GuideHalo.Visibility = Visibility.Visible;
        GuideHalo.HorizontalAlignment = HorizontalAlignment.Left;
        GuideHalo.VerticalAlignment = VerticalAlignment.Top;
        GuideHalo.Margin = new Thickness(hole.X, hole.Y, 0, 0);
        GuideHalo.Width = hole.Width;
        GuideHalo.Height = hole.Height;

        GuideCard.UpdateLayout();
        var cardW = GuideCard.ActualWidth;
        var cardH = GuideCard.ActualHeight;

        GuideCard.HorizontalAlignment = HorizontalAlignment.Left;
        GuideCard.VerticalAlignment = VerticalAlignment.Top;

        double left;
        if (b.Right + 16 + cardW <= overlayW)
        {
            left = b.Right + 16;
        }
        else if (b.X - 16 - cardW >= 0)
        {
            left = b.X - 16 - cardW;
        }
        else
        {
            left = Math.Clamp(b.X, 8, Math.Max(8, overlayW - cardW - 8));
        }

        double top = b.Top + b.Height / 2 - cardH / 2;
        top = Math.Clamp(top, 8, Math.Max(8, overlayH - cardH - 8));
        GuideCard.Margin = new Thickness(left, top, 0, 0);
    }

    private void FinishGuide()
    {
        _guideStep = -1;
        GuideOverlay.Visibility = Visibility.Collapsed;
        try
        {
            Directory.CreateDirectory(Path.GetDirectoryName(GuideMarkerPath)!);
            File.WriteAllText(GuideMarkerPath, DateTime.Now.ToString("s"));
        }
        catch { /* 标记写入失败不影响本次使用 */ }
    }

    private void GuideNext_Click(object sender, RoutedEventArgs e)
    {
        if (_guideStep >= GuideSteps.Length - 1) FinishGuide();
        else ShowGuideStep(_guideStep + 1);
    }

    private void GuidePrev_Click(object sender, RoutedEventArgs e)
    {
        if (_guideStep > 0) ShowGuideStep(_guideStep - 1);
    }

    private void GuideSkip_Click(object sender, RoutedEventArgs e) => FinishGuide();

    private void PayMethod_Checked(object sender, RoutedEventArgs e)
    {
        if (_vm is null) return;
        if (sender is System.Windows.Controls.RadioButton rb && rb.Tag is string method && !string.IsNullOrWhiteSpace(method))
        {
            _vm.PaymentMethod = method;
        }
    }

    protected override void OnClosing(System.ComponentModel.CancelEventArgs e)
    {
        base.OnClosing(e);
        _vm.Shutdown();
    }

    private void Logout_Click(object sender, RoutedEventArgs e)
    {
        _vm.Logout();
        var login = new LoginWindow();
        login.Show();
        Close();
    }
}
