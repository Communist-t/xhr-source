using System.Collections.ObjectModel;
using System.IO;
using System.Windows;
using System.Windows.Media.Imaging;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Launcher.Models;
using Launcher.Services;
using QRCoder;

namespace Launcher.ViewModels;

/// <summary>充值支付方式可选项。</summary>
public sealed record PaymentMethodOption(string Key, string Label);

public partial class MainViewModel : ObservableObject
{
    private readonly ConfigService _config;
    private readonly ProcessManager _pm;
    private readonly SessionStore _sessionStore;
    private readonly UserConfigSyncService _sync;
    private readonly UserSession _session;
    private readonly MaterialService? _materials;
    private readonly FullClientService? _fullClient;

    public ObservableCollection<MountedAppViewModel> Apps { get; } = new();

    [ObservableProperty]
    private string _currentUsername = "";

    [ObservableProperty]
    private string _currentRole = "";

    /// <summary>当前选中的页面</summary>
    [ObservableProperty]
    private AppPage _currentPage = AppPage.Programs;

    // ===== 设置面板：账号与版本展示字段 =====
    public string Email { get; }
    public string UserId { get; }
    public string ReferralCode { get; }
    public string ApiKeyDisplay { get; }
    public string ApiBaseUrl { get; }
    public string Version { get; } =
        string.IsNullOrWhiteSpace(UpdateService.ReadPackagedVersion())
            ? UpdateService.CurrentVersionLabel
            : "v" + UpdateService.ReadPackagedVersion()!.TrimStart('v', 'V');
    public int AppCount => Apps.Count;

    // ===== 自动更新状态字段 =====

    /// <summary>更新流程是否正在进行（检查/下载/替换），禁用重复操作。</summary>
    [ObservableProperty]
    private bool _isUpdateBusy;

    /// <summary>更新状态提示文案（版本说明、下载进度、错误信息）。</summary>
    [ObservableProperty]
    private string _updateStatusText = "";

    /// <summary>是否存在可安装的新版本。</summary>
    [ObservableProperty]
    private bool _hasUpdateAvailable;

    /// <summary>远端新版本号（有更新时展示）。</summary>
    [ObservableProperty]
    private string _updateTargetVersion = "";

    /// <summary>更新进度 0–100。</summary>
    [ObservableProperty]
    private double _updateProgress;

    /// <summary>本次更新是否由后端标记为强制（当前仅展示，不拦截使用）。</summary>
    [ObservableProperty]
    private bool _updateMandatory;

    /// <summary>更新推荐弹窗是否可见：每次启动检测到新版本即弹出，用户可稍后再说。</summary>
    [ObservableProperty]
    private bool _isUpdatePromptOpen;

    private UpdateInfo? _pendingUpdate;
    private UpdateService? _updateService;

    public bool IsProgramsActive => CurrentPage == AppPage.Programs;
    public bool IsMarketplaceActive => CurrentPage == AppPage.Marketplace;
    public bool IsSettingsActive => CurrentPage == AppPage.Settings;

    /// <summary>我发布的云端员工（技能），侧边栏列表展示，与本地启动器分离。</summary>
    public ObservableCollection<SkillApiClient.SkillItem> MySkills { get; } = new();

    /// <summary>按分类分组后的云端员工，供侧边栏分组展示。</summary>
    public ObservableCollection<SkillGroup> SkillGroups { get; } = new();

    [ObservableProperty]
    private bool _hasCloudSkills;

    /// <summary>点击云端员工后，给用户的右侧引导提示。</summary>
    [ObservableProperty]
    private string _skillHintText = "";

    /// <summary>启动确认弹窗是否可见。</summary>
    [ObservableProperty]
    private bool _isLaunchConfirmOpen;

    /// <summary>待启动的云端员工名（弹窗文案用）。</summary>
    [ObservableProperty]
    private string _confirmSkillName = "";

    /// <summary>对应本地启动员工名（弹窗文案用）。</summary>
    [ObservableProperty]
    private string _confirmTargetName = "";

    /// <summary>待启动的目标本地员工 VM（确认后执行启动）。</summary>
    private MountedAppViewModel? _confirmTarget;

    /// <summary>云端员工分类分组。</summary>
    public sealed class SkillGroup
    {
        public string Category { get; set; } = "";
        /// <summary>归属本地程序 ID（wechat-rpa / matrix-video）。</summary>
        public string OwnerAppId { get; set; } = "";
        /// <summary>归属本地程序展示名（微信可用 / 矩阵可用）。</summary>
        public string OwnerAppLabel { get; set; } = "";
        public ObservableCollection<SkillApiClient.SkillItem> Items { get; } = new();
    }

    // ===== 钱包 / 充值 =====

    /// <summary>当前余额（展示用，两位小数）</summary>
    [ObservableProperty]
    private string _balance = "0.00";

    /// <summary>累计充值</summary>
    [ObservableProperty]
    private string _totalRecharge = "0.00";

    /// <summary>累计提现</summary>
    [ObservableProperty]
    private string _totalWithdraw = "0.00";

    /// <summary>充值弹窗是否可见</summary>
    [ObservableProperty]
    private bool _isRechargeOpen;

    /// <summary>充值金额输入（元）</summary>
    [ObservableProperty]
    private string _rechargeAmount = "";

    /// <summary>支付方式：wechat_pay / alipay（以后端开通为准）</summary>
    [ObservableProperty]
    private string _paymentMethod = "wechat_pay";

    /// <summary>后端实际开通的充值方式（key=方式标识，value=展示名）；未取到时只保留微信。</summary>
    public ObservableCollection<PaymentMethodOption> PaymentMethods { get; } = new()
    {
        new("wechat_pay", "微信支付"),
    };

    /// <summary>是否存在多种可选支付方式（仅一种时不展示选项行）。</summary>
    public bool HasMultiplePaymentMethods => PaymentMethods.Count > 1;

    /// <summary>充值流程状态提示</summary>
    [ObservableProperty]
    private string _rechargeStatusText = "";

    /// <summary>支付链接（微信 code_url / 支付宝链接），供扫码或浏览器打开</summary>
    [ObservableProperty]
    private string _paymentUrl = "";

    /// <summary>支付链接对应的二维码位图</summary>
    [ObservableProperty]
    private BitmapImage? _qrImage;

    /// <summary>是否已有可展示的支付链接</summary>
    public bool HasPaymentUrl => !string.IsNullOrWhiteSpace(PaymentUrl);

    /// <summary>是否处于下单 / 轮询中（禁用按钮）</summary>
    [ObservableProperty]
    private bool _isRechargeBusy;

    /// <summary>快捷面额</summary>
    public ObservableCollection<decimal> QuickAmounts { get; } = new() { 10m, 50m, 100m, 200m, 500m };

    private string? _currentOrderId;
    private CancellationTokenSource? _pollCts;

    /// <summary>余额自动刷新定时器（每 3 秒一次）</summary>
    private System.Windows.Threading.DispatcherTimer? _balanceTimer;

    /// <summary>余额刷新重入保护：上一次请求未返回前不并发发起下一次</summary>
    private int _balanceRefreshing;

    /// <summary>云端员工列表刷新重入保护</summary>
    private int _skillsRefreshing;

    /// <summary>员工列表内容签名：无变化时跳过重建，避免列表每 3 秒闪断/丢失选中</summary>
    private string _skillsSignature = "";

    /// <summary>余额自动刷新间隔（秒）</summary>
    private static readonly TimeSpan BalanceRefreshInterval = TimeSpan.FromSeconds(3);

    public MainViewModel(UserSession session)
    {
        _config = new ConfigService();
        _pm = new ProcessManager();
        _sessionStore = new SessionStore();
        _sync = new UserConfigSyncService();
        _session = session;

        CurrentUsername = session.Username;
        CurrentRole = session.Role;
        Email = string.IsNullOrWhiteSpace(session.Email) ? "未绑定邮箱" : session.Email;
        UserId = session.UserId.ToString();
        ReferralCode = string.IsNullOrWhiteSpace(session.ReferralCode) ? "无" : session.ReferralCode;
        ApiKeyDisplay = string.IsNullOrWhiteSpace(session.ApiKey)
            ? "未配置"
            : session.ApiKey.Length > 8 ? session.ApiKey[..8] + "…" : session.ApiKey;
        ApiBaseUrl = string.IsNullOrWhiteSpace(session.ApiBaseUrl) ? "默认地址" : session.ApiBaseUrl;

        Balance = session.Balance;
        TotalRecharge = session.TotalRecharge;
        TotalWithdraw = session.TotalWithdraw;

        // 进入主窗前，先把当前账号 ApiKey 同步到各子程序配置文件
        _sync.SyncAll(_config.Config.Apps, session);

        // 物料服务：清单拉取失败不阻断主窗展示，卡片按未下载处理。
        _materials = CreateMaterialService();
        _ = LoadMaterialManifestAsync();

        // 完整客户端（安装型）服务：探测注册表/安装登记，失败不阻断主窗。
        _fullClient = CreateFullClientService();

        foreach (var app in _config.Config.Apps)
        {
            var vm = new MountedAppViewModel(app, _pm, _sync, session, _materials, _fullClient);
            Apps.Add(vm);
        }

        _pm.OnProcessStateChanged += (appId, running) =>
        {
            System.Windows.Application.Current.Dispatcher.Invoke(() =>
            {
                var vm = Apps.FirstOrDefault(a => a.Model.Id == appId);
                vm?.UpdateState(running);
            });
        };

        // 进入主窗后先拉一次最新余额，随后每 3 秒自动刷新
        _ = RefreshBalanceAsync();
        _ = RefreshMySkillsAsync();
        StartBalanceTimer();

        // 启动时静默检查一次更新；失败不阻断主流程。
        _ = CheckUpdateOnStartupAsync();
    }

    /// <summary>启动余额自动刷新定时器（3 秒一次）。</summary>
    private void StartBalanceTimer()
    {
        _balanceTimer?.Stop();
        _balanceTimer = new System.Windows.Threading.DispatcherTimer
        {
            Interval = BalanceRefreshInterval
        };
        _balanceTimer.Tick += (_, _) =>
        {
            _ = RefreshBalanceAsync();
            _ = RefreshMySkillsAsync();
        };
        _balanceTimer.Start();
    }

    partial void OnCurrentPageChanged(AppPage value)
    {
        OnPropertyChanged(nameof(IsProgramsActive));
        OnPropertyChanged(nameof(IsMarketplaceActive));
        OnPropertyChanged(nameof(IsSettingsActive));
    }

    partial void OnPaymentUrlChanged(string value)
    {
        OnPropertyChanged(nameof(HasPaymentUrl));
        QrImage = BuildQr(value);
    }

    /// <summary>把支付链接渲染为二维码位图；链接为空或生成失败返回 null。</summary>
    private static BitmapImage? BuildQr(string? payload)
    {
        if (string.IsNullOrWhiteSpace(payload)) return null;
        try
        {
            using var generator = new QRCodeGenerator();
            using var data = generator.CreateQrCode(payload, QRCodeGenerator.ECCLevel.M);
            using var renderer = new PngByteQRCode(data);
            var bytes = renderer.GetGraphic(16);
            using var ms = new MemoryStream(bytes);
            var bmp = new BitmapImage();
            bmp.BeginInit();
            bmp.CacheOption = BitmapCacheOption.OnLoad;
            bmp.StreamSource = ms;
            bmp.EndInit();
            bmp.Freeze();
            return bmp;
        }
        catch
        {
            return null;
        }
    }

    /// <summary>拉取我发布的云端员工；失败不阻断本地启动器展示。</summary>
    private async Task RefreshMySkillsAsync()
    {
        if (System.Threading.Interlocked.Exchange(ref _skillsRefreshing, 1) == 1) return;
        try
        {
            var client = new SkillApiClient(_config.Config.ApiBaseUrl, _session.AccessToken);
            var skills = await client.GetMySkillsAsync();

            // 只排除已删除（status=deleted）；公开/私密都展示，私密单独标注。
            var active = skills.Where(s => s.IsActive).ToList();

            // 内容签名一致则跳过重建：3 秒轮询下避免列表闪断、选中丢失。
            var signature = string.Join("|", active.Select(s => $"{s.Id}:{s.Name}:{s.Version}"));
            if (signature == _skillsSignature) return;
            _skillsSignature = signature;

            MySkills.Clear();
            SkillGroups.Clear();

            foreach (var skill in active)
            {
                MySkills.Add(skill);
            }

            // 固定分组顺序：微信客服 → x.HR 完整版（矩阵是独立完整员工，不挂子员工）。
            var order = new[] { "微信员工", "x.HR 员工" };
            var groups = active.GroupBy(s => s.CategoryLabel)
                               .OrderBy(g => order.Contains(g.Key) ? Array.IndexOf(order, g.Key) : int.MaxValue);
            foreach (var g in groups)
            {
                var group = new SkillGroup
                {
                    Category = g.Key,
                    OwnerAppId = g.First().OwnerAppId,
                    OwnerAppLabel = g.First().OwnerAppLabel
                };
                foreach (var item in g) group.Items.Add(item);
                SkillGroups.Add(group);
            }

            HasCloudSkills = MySkills.Count > 0;
        }
        catch (Exception ex)
        {
            // 云端技能拉取失败：分组区留空，不影响本地员工；错误写入日志便于排查。
            try
            {
                var dir = System.IO.Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "xHR");
                Directory.CreateDirectory(dir);
                File.AppendAllText(System.IO.Path.Combine(dir, "launcher.log"),
                    $"[{DateTime.Now:yyyy-MM-dd HH:mm:ss}] RefreshMySkillsAsync 失败: {ex}\r\n");
            }
            catch { /* 忽略日志写入失败 */ }
        }
        finally
        {
            System.Threading.Interlocked.Exchange(ref _skillsRefreshing, 0);
        }
    }

    // ===== 物料清单 =====

    private MaterialService CreateMaterialService() =>
        new(_config.Config.ApiBaseUrl, string.IsNullOrWhiteSpace(_config.Config.MaterialManifestUrl)
            ? null
            : _config.Config.MaterialManifestUrl);

    private FullClientService CreateFullClientService() =>
        new(_config.Config.ApiBaseUrl, _session.AccessToken);

    /// <summary>启动时拉取物料清单；成功后刷新各物料卡片的就位状态。失败静默忽略。</summary>
    private async Task LoadMaterialManifestAsync()
    {
        try
        {
            if (_materials is not null)
            {
                await _materials.LoadManifestAsync();
                foreach (var vm in Apps)
                {
                    if (!vm.IsMaterial) continue;
                    vm.RefreshInstalledState(_materials.IsInstalled(vm.Model.MaterialId));
                }
            }

            // 安装型卡片：按注册表/本地登记刷新就位状态（不依赖远端清单拉取成功）。
            if (_fullClient is not null)
            {
                foreach (var vm in Apps)
                {
                    if (!vm.IsInstaller) continue;
                    vm.RefreshInstalledState(_fullClient.IsInstalled(vm.Model));
                }
            }
        }
        catch
        {
            // 清单不可用时静默忽略；卡片保持待下载/待安装态，点用时再提示。
        }
    }

    // ===== 自动更新 =====

    private UpdateService CreateUpdateService() =>
        new(_config.Config.ApiBaseUrl, string.IsNullOrWhiteSpace(_config.Config.UpdateManifestUrl)
            ? null
            : _config.Config.UpdateManifestUrl,
            _session.AccessToken);

    /// <summary>启动时静默检查更新：有更新则回填提示，不自动下载。</summary>
    private async Task CheckUpdateOnStartupAsync()
    {
        try
        {
            _updateService = CreateUpdateService();
            var info = await _updateService.CheckForUpdateAsync();
            if (info is null) return;

            _pendingUpdate = info;
            HasUpdateAvailable = true;
            UpdateTargetVersion = "v" + info.TargetVersion;
            UpdateMandatory = false; // /api/version 无 mandatory 字段，一律按推荐更新
            UpdateStatusText = string.IsNullOrWhiteSpace(info.ReleaseNotes)
                ? $"发现新版本 {UpdateTargetVersion}"
                : $"发现新版本 {UpdateTargetVersion}：{info.ReleaseNotes}";

            // 推荐更新：每次启动检测到新版本即弹窗提示，可稍后再说，不强制拦截。
            IsUpdatePromptOpen = true;
        }
        catch
        {
            // 更新检查不可用时静默忽略，保留本地版本展示
        }
    }

    /// <summary>手动点击"检查更新"：重新拉取远端清单比对。</summary>
    [RelayCommand]
    private async Task CheckForUpdateAsync()
    {
        if (IsUpdateBusy) return;
        IsUpdateBusy = true;
        UpdateStatusText = "正在检查更新…";
        try
        {
            _updateService ??= CreateUpdateService();
            var info = await _updateService.CheckForUpdateAsync();
            if (info is null)
            {
                HasUpdateAvailable = false;
                UpdateStatusText = "当前已是最新版本";
                return;
            }

            _pendingUpdate = info;
            HasUpdateAvailable = true;
            UpdateTargetVersion = "v" + info.TargetVersion;
            UpdateMandatory = false; // /api/version 无 mandatory 字段，一律按推荐更新
            UpdateStatusText = string.IsNullOrWhiteSpace(info.ReleaseNotes)
                ? $"发现新版本 {UpdateTargetVersion}"
                : $"发现新版本 {UpdateTargetVersion}：{info.ReleaseNotes}";
            IsUpdatePromptOpen = true;
        }
        catch (Exception ex)
        {
            UpdateStatusText = $"检查更新失败：{ex.Message}";
        }
        finally
        {
            IsUpdateBusy = false;
        }
    }

    /// <summary>关闭推荐更新弹窗（稍后再说），不影响继续使用；下次重启仍会提示。</summary>
    [RelayCommand]
    private void DismissUpdatePrompt() => IsUpdatePromptOpen = false;

    /// <summary>从推荐弹窗直接进入下载安装流程。</summary>
    [RelayCommand]
    private async Task InstallUpdateFromPromptAsync()
    {
        IsUpdatePromptOpen = false;
        await InstallUpdateAsync();
    }

    /// <summary>下载并安装更新：下载安装包 → 校验 → 静默拉起安装器 → 退出，替换与重启由安装器完成。</summary>
    [RelayCommand]
    private async Task InstallUpdateAsync()
    {
        if (IsUpdateBusy || _pendingUpdate is null) return;
        IsUpdateBusy = true;
        UpdateProgress = 0;
        try
        {
            _updateService ??= CreateUpdateService();
            UpdateStatusText = "正在下载安装包…";

            var progress = new Progress<(long Received, long Total)>(p =>
            {
                UpdateProgress = p.Total > 0 ? Math.Round(100.0 * p.Received / p.Total, 1) : 0;
                UpdateStatusText = $"正在下载安装包… {UpdateProgress:0.#}%";
            });

            var installerPath = await _updateService.DownloadInstallerAsync(_pendingUpdate, progress);

            UpdateStatusText = "正在安装更新，即将自动重启…";
            // 静默拉起安装器：由它关闭旧进程、替换文件并拉起新版。
            _updateService.LaunchInstaller(installerPath);

            // 触发主窗口关闭；子程序由 JobObject 兜底回收。
            Shutdown();
            System.Windows.Application.Current.Shutdown(0);
        }
        catch (Exception ex)
        {
            UpdateStatusText = $"更新失败：{ex.Message}";
            IsUpdateBusy = false;
        }
    }

    [RelayCommand]
    private void Navigate(AppPage page) => CurrentPage = page;

    /// <summary>
    /// 点击左侧云端员工：按分类映射到对应本地启动器，弹确认框；确认后直接启动对应员工。
    /// 云端员工本体不直接启动，需通过右侧对应员工进入使用。
    /// </summary>
    [RelayCommand]
    private void SelectCloudSkill(SkillApiClient.SkillItem skill)
    {
        if (skill is null) return;

        // 分类 → 本地启动器 ID：直接取员工自身归属，避免再多维护一份映射。
        var appId = skill.OwnerAppId;

        var target = Apps.FirstOrDefault(a => a.Model.Id == appId);
        CurrentPage = AppPage.Programs;

        if (target is null)
        {
            SkillHintText = $"请在右侧选择「{skill.CategoryLabel}」对应的员工后使用。";
            return;
        }

        // 目标程序尚未就位：安装型（完整客户端）提示先下载安装，物料型提示先下载。
        if (!target.IsReady)
        {
            SkillHintText = target.IsInstaller
                ? $"「{skill.Name}」需要在「{target.Name}」中使用，请先在右侧卡片点「下载安装」完成安装。"
                : $"「{skill.Name}」需要在「{target.Name}」中使用，请先在右侧卡片点「下载」完成下载。";
            return;
        }

        // 目标员工已在运行：不重复拉起。
        if (target.IsRunning)
        {
            SkillHintText = $"「{target.Name}」已在运行，可直接使用。";
            return;
        }

        // 弹出启动确认框。
        _confirmTarget = target;
        ConfirmSkillName = skill.Name;
        ConfirmTargetName = target.Name;
        SkillHintText = "";
        IsLaunchConfirmOpen = true;
    }

    /// <summary>确认启动：同步账号 ApiKey 后拉起对应本地员工。</summary>
    [RelayCommand]
    private void ConfirmLaunch()
    {
        if (_confirmTarget is null)
        {
            IsLaunchConfirmOpen = false;
            return;
        }

        var target = _confirmTarget;
        IsLaunchConfirmOpen = false;

        // 复用右侧卡片同一套启动链路：先同步 ApiKey，再启动。
        target.LaunchCommand.Execute(null);

        SkillHintText = target.IsRunning
            ? $"「{ConfirmTargetName}」已启动，请在右侧查看工作状态。"
            : $"「{ConfirmTargetName}」启动失败，请稍后重试。";

        _confirmTarget = null;
    }

    /// <summary>取消启动确认，不拉起员工。</summary>
    [RelayCommand]
    private void CloseLaunchConfirm()
    {
        IsLaunchConfirmOpen = false;
        _confirmTarget = null;
        SkillHintText = "";
    }

    public void Shutdown()
    {
        _balanceTimer?.Stop();
        _balanceTimer = null;
        _pollCts?.Cancel();
        _pm.Dispose();
    }

    /// <summary>退出登录：清掉 session 文件，回到登录窗</summary>
    public void Logout()
    {
        _balanceTimer?.Stop();
        _balanceTimer = null;
        _pollCts?.Cancel();
        _sessionStore.Clear();
    }

    // ===== 钱包 =====

    private WalletApiClient CreateWalletClient() =>
        new(_config.Config.ApiBaseUrl, _session.AccessToken);

    /// <summary>拉取最新余额并回写会话展示字段。</summary>
    public async Task RefreshBalanceAsync()
    {
        // 重入保护：上一次请求未返回时跳过本次，避免定时器与充值轮询叠加并发请求
        if (System.Threading.Interlocked.Exchange(ref _balanceRefreshing, 1) == 1)
            return;
        try
        {
            var wallet = await CreateWalletClient().GetBalanceAsync();
            Balance = wallet.Balance;
            TotalRecharge = wallet.TotalRecharge;
            TotalWithdraw = wallet.TotalWithdraw;

            // 同步回本地会话文件，供子程序读取
            _session.Balance = wallet.Balance;
            _session.TotalRecharge = wallet.TotalRecharge;
            _session.TotalWithdraw = wallet.TotalWithdraw;
            _sessionStore.Save(_session);

            await RefreshPaymentMethodsAsync();
        }
        catch
        {
            // 余额拉取失败不阻断主流程，保留本地缓存值
        }
        finally
        {
            System.Threading.Interlocked.Exchange(ref _balanceRefreshing, 0);
        }
    }

    /// <summary>按后端 /wallet/config 返回的 support_payment_methods 刷新可选支付方式；
    /// 接口不可用或列表为空时，兜底只保留微信。</summary>
    private async Task RefreshPaymentMethodsAsync()
    {
        try
        {
            var cfg = await CreateWalletClient().GetWalletConfigAsync();
            var supported = cfg.SupportPaymentMethods
                .Where(m => !string.IsNullOrWhiteSpace(m))
                .Select(m => m.Trim())
                .ToList();

            PaymentMethods.Clear();
            if (supported.Count == 0)
            {
                // 兜底口径：后端未返回渠道清单时，只展示当前已开通的微信
                PaymentMethods.Add(new PaymentMethodOption("wechat_pay", "微信支付"));
            }
            else
            {
                var known = new Dictionary<string, string>
                {
                    ["wechat_pay"] = "微信支付",
                    ["alipay"] = "支付宝",
                };
                foreach (var method in supported)
                {
                    var label = known.TryGetValue(method, out var n) ? n : method;
                    PaymentMethods.Add(new PaymentMethodOption(method, label));
                }
            }

            // 当前选中方式若已不在清单内，回退到第一个可用方式
            if (!PaymentMethods.Any(p => p.Key == PaymentMethod))
            {
                PaymentMethod = PaymentMethods.FirstOrDefault()?.Key ?? "wechat_pay";
            }

            OnPropertyChanged(nameof(HasMultiplePaymentMethods));
        }
        catch
        {
            // 渠道配置拉取失败：维持现有选项（默认微信），不阻断充值
        }
    }

    [RelayCommand]
    private void OpenRecharge()
    {
        _currentOrderId = null;
        PaymentUrl = "";
        RechargeStatusText = "";
        IsRechargeBusy = false;
        IsRechargeOpen = true;
    }

    [RelayCommand]
    private void CloseRecharge()
    {
        _pollCts?.Cancel();
        IsRechargeOpen = false;
    }

    [RelayCommand]
    private void PickQuickAmount(decimal amount) => RechargeAmount = amount.ToString("0.##");

    [RelayCommand]
    private async Task SubmitRechargeAsync()
    {
        if (IsRechargeBusy) return;

        if (!decimal.TryParse(RechargeAmount?.Trim(), out var amount) || amount <= 0)
        {
            RechargeStatusText = "请输入大于 0 的充值金额";
            return;
        }
        if (amount < 0.01m)
        {
            RechargeStatusText = "充值金额最小 0.01 元";
            return;
        }

        IsRechargeBusy = true;
        RechargeStatusText = "正在创建充值订单…";
        PaymentUrl = "";

        try
        {
            var order = await CreateWalletClient().CreateRechargeAsync(amount, PaymentMethod);
            _currentOrderId = order.OrderId;

            var url = order.PaymentUrl
                      ?? order.PaymentParams?.CodeUrl
                      ?? "";
            PaymentUrl = url;
            RechargeStatusText = order.PaymentParams?.Mock == true
                ? "模拟支付模式：订单已创建，等待确认到账"
                : "请使用对应 App 扫码或点击链接完成支付";

            // 开始轮询订单状态
            await StartPollingAsync(order.OrderId);
        }
        catch (Exception ex)
        {
            RechargeStatusText = ex.Message;
        }
        finally
        {
            IsRechargeBusy = false;
        }
    }

    /// <summary>轮询订单状态，默认每 2 秒一次，最长约 5 分钟。</summary>
    private async Task StartPollingAsync(string orderId)
    {
        _pollCts?.Cancel();
        _pollCts = new CancellationTokenSource();
        var ct = _pollCts.Token;

        var deadline = DateTime.UtcNow.AddMinutes(5);
        try
        {
            while (DateTime.UtcNow < deadline && !ct.IsCancellationRequested)
            {
                await Task.Delay(TimeSpan.FromSeconds(2), ct);

                var status = await CreateWalletClient().GetRechargeStatusAsync(orderId);
                switch (status.Status)
                {
                    case "completed":
                        RechargeStatusText = "支付成功，余额已到账";
                        PaymentUrl = "";
                        await RefreshBalanceAsync();
                        return;
                    case "failed":
                        RechargeStatusText = "支付失败，请重新发起充值";
                        return;
                    default:
                        RechargeStatusText = "等待支付确认…";
                        break;
                }
            }

            if (!ct.IsCancellationRequested)
                RechargeStatusText = "支付确认超时，请稍后在余额中核对";
        }
        catch (OperationCanceledException)
        {
            // 弹窗关闭或手动取消，不提示
        }
        catch (Exception ex)
        {
            RechargeStatusText = $"状态查询中断：{ex.Message}";
        }
    }

    [RelayCommand]
    private void OpenPaymentLink()
    {
        if (string.IsNullOrWhiteSpace(PaymentUrl)) return;
        try
        {
            System.Diagnostics.Process.Start(new System.Diagnostics.ProcessStartInfo
            {
                FileName = PaymentUrl,
                UseShellExecute = true
            });
        }
        catch
        {
            RechargeStatusText = "无法自动打开支付链接，请复制后在浏览器中打开";
        }
    }
}
