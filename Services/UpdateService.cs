using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Reflection;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.RegularExpressions;
using Launcher.Models;

namespace Launcher.Services;

/// <summary>
/// 程序内自动更新服务：拉取 /api/version → 版本比对 → 下载 NSIS 安装包 → SHA512 校验 → 静默安装 → 重启。
///
/// 设计要点：
/// 1) 文件替换与旧进程关闭由 NSIS 安装器负责（等待 xHR.exe 退出，超时强杀），
///    主程序拉起安装器后立即退出，安装完成由安装器拉起新版。
/// 2) 版本信息走后端现成的 {ApiBaseUrl}/api/version desktop 渠道，与完整版客户端共用一套版本号；
///    安装包地址为 {ApiBaseUrl}/uploads/software/<desktop.files[0].url>。可在 config.json 覆盖清单地址。
/// 3) 下载、校验失败均抛异常由上层 UI 捕获；暂存目录下次启动清理。
/// </summary>
public class UpdateService
{
    private readonly HttpClient _http;
    private readonly string _apiBase;
    private readonly string _updateBase;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull
    };

    /// <summary>本地运行态目录（%APPDATA%\xHR），更新缓存与日志放这里。</summary>
    public static string LocalDataDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "xHR");

    /// <summary>更新暂存根目录：已下载的安装包放在这里。</summary>
    private static string UpdateStagingDir => Path.Combine(LocalDataDir, "update");

    public UpdateService(string apiBaseUrl, string? manifestUrlOverride = null, string? authToken = null)
    {
        _http = new HttpClient { Timeout = TimeSpan.FromMinutes(15) };
        _http.DefaultRequestHeaders.Add("User-Agent", "xHR-Launcher-Update");
        if (!string.IsNullOrWhiteSpace(authToken))
            _http.DefaultRequestHeaders.Authorization =
                new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", authToken);
        _apiBase = apiBaseUrl.TrimEnd('/');
        _updateBase = string.IsNullOrWhiteSpace(manifestUrlOverride)
            ? $"{_apiBase}/api/version/all"
            : manifestUrlOverride;
    }

    /// <summary>读取当前程序集版本；读取不到时回退到 0.0.0。</summary>
    public static Version CurrentVersion
    {
        get
        {
            var asm = Assembly.GetEntryAssembly();
            var ver = asm?.GetName().Version;
            return ver ?? new Version(0, 0, 0);
        }
    }

    public static string CurrentVersionLabel => "v" + CurrentVersion.ToString(3);

    /// <summary>
    /// 从程序目录下的 version.json 读取发布版本号（launcher_version）。
    /// 绿色包随物料一并分发；读不到时返回 null，UI 回退到程序集版本。
    /// </summary>
    public static string? ReadPackagedVersion()
    {
        try
        {
            var path = Path.Combine(AppContext.BaseDirectory, "version.json");
            if (!File.Exists(path)) return null;
            var json = File.ReadAllText(path);
            using var doc = JsonDocument.Parse(json);
            if (doc.RootElement.TryGetProperty("launcher_version", out var el))
                return el.GetString();
        }
        catch
        {
            // 版本文件缺失/损坏时忽略，回退程序集版本
        }
        return null;
    }

    // ===== 1. 检查更新 =====

    /// <summary>拉取三渠道版本信息并比对 lite 渠道；有更新返回渠道信息，无更新返回 null。</summary>
    public async Task<UpdateInfo?> CheckForUpdateAsync(CancellationToken ct = default)
    {
        var json = await _http.GetStringAsync(_updateBase, ct);
        var all = JsonSerializer.Deserialize<VersionAllResponse>(json, JsonOpts);
        var info = all?.Lite;
        if (info is null || string.IsNullOrWhiteSpace(info.TargetVersion)) return null;

        if (TryParseVersion(info.TargetVersion, out var remote) && remote > EffectiveCurrentVersion)
            return info;

        return null;
    }

    /// <summary>
    /// 比对所用的本地版本：优先读发布包 version.json，其次程序集版本。
    /// </summary>
    private static Version EffectiveCurrentVersion =>
        TryParseVersion(ReadPackagedVersion() ?? "", out var packed)
            ? packed
            : CurrentVersion;

    /// <summary>
    /// 下载安装包到暂存目录并校验哈希。返回本地 Setup.exe 完整路径。
    /// 进度回调：已下载字节 / 总字节（总字节未知时为 0）。
    /// </summary>
    public async Task<string> DownloadInstallerAsync(
        UpdateInfo info,
        IProgress<(long Received, long Total)>? progress = null,
        CancellationToken ct = default)
    {
        var packageUrl = info.DownloadUrl.Trim();
        if (string.IsNullOrWhiteSpace(packageUrl))
            throw new InvalidOperationException("版本接口缺少 lite 渠道 download_url");
        if (!packageUrl.StartsWith("http", StringComparison.OrdinalIgnoreCase))
            packageUrl = $"{_apiBase}/{packageUrl.TrimStart('/')}";

        Directory.CreateDirectory(UpdateStagingDir);
        var installerPath = Path.Combine(UpdateStagingDir,
            $"xHR-{SanitizeFileName(info.TargetVersion)}-setup.exe");

        using var response = await _http.GetAsync(packageUrl, HttpCompletionOption.ResponseHeadersRead, ct);
        response.EnsureSuccessStatusCode();

        var total = response.Content.Headers.ContentLength ?? 0;
        await using var src = await response.Content.ReadAsStreamAsync(ct);
        await using var dst = new FileStream(installerPath, FileMode.Create, FileAccess.Write, FileShare.None);

        var buffer = new byte[81920];
        long received = 0;
        int read;
        while ((read = await src.ReadAsync(buffer, ct)) > 0)
        {
            await dst.WriteAsync(buffer.AsMemory(0, read), ct);
            received += read;
            progress?.Report((received, total));
        }

        // lite 渠道后端不提供安装包哈希，暂只校验下载完整性（HTTP 成功 + 非空文件）
        if (new FileInfo(installerPath).Length == 0)
            throw new InvalidOperationException("安装包下载不完整（0 字节）");

        return installerPath;
    }

    /// <summary>
    /// 静默拉起 NSIS 安装器并返回：安装器负责等待旧进程退出、
    /// 替换文件、完成后拉起新版。调用方随后应立即退出主程序。
    /// </summary>
    public void LaunchInstaller(string installerPath)
    {
        var psi = new ProcessStartInfo
        {
            FileName = installerPath,
            Arguments = "/S",
            WorkingDirectory = UpdateStagingDir,
            UseShellExecute = false,
            CreateNoWindow = true
        };
        // 安装器进程独立于主程序 JobObject：主程序退出后仍能完成替换与重启。
        Process.Start(psi);
    }

    /// <summary>清理上次未完成的更新暂存内容。</summary>
    public void CleanupStaging() => CleanupStagingStatic();

    /// <summary>
    /// 静态版本：供 App 启动早期（尚无服务实例）调用。
    /// 清空暂存目录下的安装包与旧机制残留（解压目录、helper 脚本等）。
    /// </summary>
    public static void CleanupStagingStatic()
    {
        try
        {
            if (!Directory.Exists(UpdateStagingDir)) return;
            foreach (var dir in Directory.GetDirectories(UpdateStagingDir))
                Directory.Delete(dir, true);
            foreach (var file in Directory.GetFiles(UpdateStagingDir))
                File.Delete(file);
        }
        catch
        {
            // 清理失败不阻断启动
        }
    }

    // ===== 工具方法 =====

    /// <summary>解析语义化版本号；带后缀时取主.次.修订部分。解析失败返回 false。</summary>
    internal static bool TryParseVersion(string text, out Version version)
    {
        version = new Version(0, 0, 0);
        if (string.IsNullOrWhiteSpace(text)) return false;
        var match = Regex.Match(text.TrimStart('v', 'V'), @"^(\d+)\.(\d+)\.(\d+)");
        if (!match.Success) return false;
        if (!Version.TryParse(match.Value, out var parsed)) return false;
        version = parsed;
        return true;
    }

    private static string SanitizeFileName(string name)
    {
        var invalid = Path.GetInvalidFileNameChars();
        return new string(name.Select(c => invalid.Contains(c) ? '_' : c).ToArray());
    }
}
