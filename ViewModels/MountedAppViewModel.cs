using System.IO;
using System.Windows.Media.Imaging;
using CommunityToolkit.Mvvm.ComponentModel;
using CommunityToolkit.Mvvm.Input;
using Launcher.Models;
using Launcher.Services;

namespace Launcher.ViewModels;

/// <summary>
/// 单个程序卡片的 ViewModel。支持三类应用：
/// 内置型（壳包自带）、物料型（绿色 zip 按需下载解压）、安装型（完整客户端，安装包静默安装后从安装目录拉起）。
/// </summary>
public partial class MountedAppViewModel : ObservableObject
{
    private readonly ProcessManager _pm;
    private readonly UserConfigSyncService _sync;
    private readonly UserSession _session;
    private readonly MaterialService? _materials;
    private readonly FullClientService? _fullClient;

    public MountedApp Model { get; }

    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(LaunchCommand))]
    [NotifyCanExecuteChangedFor(nameof(StopCommand))]
    private bool _isRunning;

    [ObservableProperty]
    private string _statusText = "待命中";

    /// <summary>产物是否已下载/安装就位（内置型恒为 true）。</summary>
    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(LaunchCommand))]
    private bool _isInstalled;

    /// <summary>下载 / 安装中（含下载、解压、静默安装）。</summary>
    [ObservableProperty]
    [NotifyCanExecuteChangedFor(nameof(LaunchCommand))]
    [NotifyCanExecuteChangedFor(nameof(DownloadCommand))]
    private bool _isMaterialBusy;

    /// <summary>下载/安装进度 0–100。</summary>
    [ObservableProperty]
    [NotifyPropertyChangedFor(nameof(ActionText))]
    private double _materialProgress;

    public string Name => Model.Name;
    public string Description => Model.Description;

    /// <summary>应用类型：material 物料型；installer 安装型；其余为内置型。</summary>
    public string AppKind => Model.Kind;

    /// <summary>是否为物料型（绿色 zip）。</summary>
    public bool IsMaterial => AppKind == "material" || !string.IsNullOrWhiteSpace(Model.MaterialId);

    /// <summary>是否为安装型（完整客户端）。</summary>
    public bool IsInstaller => AppKind == "installer";

    /// <summary>产物是否已就位：内置型恒可启动；物料/安装型需本地已下载/安装。</summary>
    public bool IsReady => !IsMaterial && !IsInstaller || IsInstalled;

    /// <summary>底部主按钮文案。</summary>
    public string ActionText
    {
        get
        {
            if (IsMaterialBusy) return $"{MaterialProgress:0.#}%";
            if (IsReady) return "开始工作";
            return IsInstaller ? "下载安装" : "下载";
        }
    }

    /// <summary>解析后的图标绝对路径（相对路径已基于程序根目录解析）。</summary>
    public string IconPath => PathResolver.Resolve(Model.IconPath);

    /// <summary>图标文件是否存在；不存在时界面回退首字母占位。</summary>
    public bool HasIcon => File.Exists(IconPath);

    /// <summary>解析后的封面图绝对路径（相对路径已基于程序根目录解析）。</summary>
    public string CoverPath => PathResolver.Resolve(Model.CoverPath);

    /// <summary>封面位图；文件不存在或解码失败时为 null，界面回退首字母占位。</summary>
    public BitmapImage? CoverImage { get; }

    /// <summary>封面图是否存在。</summary>
    public bool HasCover => CoverImage != null;

    /// <summary>标签列表（逗号分隔解析，去空白）。</summary>
    public IReadOnlyList<string> Tags =>
        (Model.Tags ?? "")
        .Split(',', StringSplitOptions.RemoveEmptyEntries | StringSplitOptions.TrimEntries)
        .ToList();

    public MountedAppViewModel(MountedApp model, ProcessManager pm, UserConfigSyncService sync,
        UserSession session, MaterialService? materials = null, FullClientService? fullClient = null)
    {
        Model = model;
        _pm = pm;
        _sync = sync;
        _session = session;
        _materials = materials;
        _fullClient = fullClient;

        IsInstalled = DetectInstalled();
        StatusText = IsReady ? "待命中" : (IsInstaller ? "待安装" : "待下载");

        var cover = PathResolver.Resolve(Model.CoverPath);
        if (File.Exists(cover))
        {
            try
            {
                var bmp = new BitmapImage();
                bmp.BeginInit();
                bmp.CacheOption = BitmapCacheOption.OnLoad;
                bmp.UriSource = new Uri(cover, UriKind.Absolute);
                bmp.EndInit();
                CoverImage = bmp;
            }
            catch
            {
                CoverImage = null;
            }
        }
    }

    private bool DetectInstalled()
    {
        if (IsInstaller) return _fullClient?.IsInstalled(Model) ?? false;
        if (IsMaterial) return _materials?.IsInstalled(Model.MaterialId) ?? false;
        return true;
    }

    private bool CanLaunch() => !IsRunning && IsReady && !IsMaterialBusy;

    private bool CanStop() => IsRunning;

    private bool CanDownload() => !IsInstalled && !IsMaterialBusy && (IsMaterial || IsInstaller);

    partial void OnIsMaterialBusyChanged(bool value)
    {
        OnPropertyChanged(nameof(ActionText));
        DownloadCommand.NotifyCanExecuteChanged();
        LaunchCommand.NotifyCanExecuteChanged();
    }

    partial void OnIsInstalledChanged(bool value)
    {
        OnPropertyChanged(nameof(IsReady));
        OnPropertyChanged(nameof(ActionText));
        LaunchCommand.NotifyCanExecuteChanged();
        DownloadCommand.NotifyCanExecuteChanged();
    }

    /// <summary>下载物料包（物料型）或下载并静默安装完整客户端（安装型）。</summary>
    [RelayCommand(CanExecute = nameof(CanDownload))]
    private async Task DownloadAsync()
    {
        IsMaterialBusy = true;
        MaterialProgress = 0;
        StatusText = IsInstaller ? "正在下载安装包…" : "正在下载物料…";
        try
        {
            var progress = new Progress<(long Received, long Total)>(p =>
            {
                MaterialProgress = p.Total > 0 ? Math.Round(100.0 * p.Received / p.Total, 1) : 0;
                StatusText = $"{StatusText.Split('…')[0]}… {MaterialProgress:0.#}%";
            });

            if (IsInstaller)
            {
                if (_fullClient is null) return;
                var remote = await _fullClient.LoadRemoteAsync();
                if (remote is null) throw new InvalidOperationException("后端未提供完整客户端安装包");

                // 已装版本与远端一致时不重复装修；仅缺失或远端更新时才安装/覆盖。
                var localVer = _fullClient.LocalVersion(Model);
                if (UpdateService.TryParseVersion(localVer, out var local) &&
                    UpdateService.TryParseVersion(remote.Version, out var remoteVer) &&
                    remoteVer <= local)
                {
                    IsInstalled = true;
                    StatusText = "待命中";
                    return;
                }

                _sync.Sync(Model, _session);
                StatusText = "正在安装完整客户端…（请在弹出的安装向导中完成）";
                var stage = new Progress<string>(s => StatusText = $"正在安装完整客户端…{s}");
                await _fullClient.InstallAsync(Model, remote, progress, stage);
            }
            else
            {
                if (_materials is null || !IsMaterial) return;
                await _materials.InstallMaterialAsync(Model.MaterialId, progress);
            }

            IsInstalled = true;
            MaterialProgress = 100;
            StatusText = "待命中";
        }
        catch (Exception ex)
        {
            StatusText = $"{(IsInstaller ? "安装" : "下载")}失败：{ex.Message}";
        }
        finally
        {
            IsMaterialBusy = false;
            OnPropertyChanged(nameof(ActionText));
        }
    }

    [RelayCommand(CanExecute = nameof(CanLaunch))]
    private void Launch()
    {
        // 未就位不启动（按钮已隐藏；兜底拦截）。
        if (!IsReady) return;

        // 拉起子程序前，先把当前登录账号的 ApiKey 同步进它的配置文件。
        _sync.Sync(Model, _session);

        string? exePath = null, workDir = null, args = null;
        if (IsInstaller)
        {
            exePath = _fullClient?.ResolveEntryExe(Model);
        }
        else if (IsMaterial && _materials is not null)
        {
            exePath = _materials.ResolveEntryExe(Model);
            workDir = _materials.ResolveWorkDir(Model);
            args = _materials.ResolveArguments(Model);
        }

        var ok = _pm.Launch(Model, exePath, workDir, args);
        if (!ok)
        {
            StatusText = "启用失败";
        }
    }

    /// <summary>在客户端内关闭正在运行的程序（连同其子进程）。</summary>
    [RelayCommand(CanExecute = nameof(CanStop))]
    private void Stop()
    {
        _pm.Kill(Model.Id);
    }

    public void UpdateState(bool running)
    {
        IsRunning = running;
        StatusText = running ? "工作中" : (IsReady ? "待命中" : (IsInstaller ? "待安装" : "待下载"));
    }

    /// <summary>清单拉取后由主窗刷新就位状态。</summary>
    public void RefreshInstalledState(bool installed)
    {
        IsInstalled = installed;
        if (!IsRunning) StatusText = IsReady ? "待命中" : (IsInstaller ? "待安装" : "待下载");
    }
}
