using System.IO;
using System.IO.Compression;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text.Json;
using Launcher.Models;

namespace Launcher.Services;

/// <summary>
/// 子程序物料（可下载员工本体）管理服务。
///
/// 设计要点：
/// 1) 壳包不内置子程序本体；各员工对应一个物料包（zip），清单描述物料版本、下载直链、入口。
/// 2) 物料下载后解压到本地物料目录：%APPDATA%\xHR\materials\<materialId>\，
///    与壳程序目录分离，覆盖更新壳子时不丢物料。
/// 3) 入口路径基于物料解压目录解析；清单提供的 entry_exe / entry_args / work_dir 覆盖卡片默认配置。
/// 4) 下载、校验、解压失败抛异常由上层 UI 捕获；已下载的 zip 暂存目录可复用。
/// </summary>
public class MaterialService
{
    private readonly HttpClient _http;
    private readonly string? _manifestUrlOverride;
    private readonly string _apiBaseUrl;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull
    };

    /// <summary>本地运行态目录（%APPDATA%\xHR）。</summary>
    public static string LocalDataDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "xHR");

    /// <summary>物料解压根目录：%APPDATA%\xHR\materials。</summary>
    public static string MaterialsRootDir => Path.Combine(LocalDataDir, "materials");

    /// <summary>物料下载暂存目录（已下载 zip 放这里，解压后可复用）。</summary>
    private static string MaterialStagingDir => Path.Combine(LocalDataDir, "material-download");

    public MaterialService(string apiBaseUrl, string? manifestUrlOverride = null)
    {
        _http = new HttpClient { Timeout = TimeSpan.FromMinutes(10) };
        _http.DefaultRequestHeaders.Add("User-Agent", "xHR-Launcher-Material");
        _apiBaseUrl = apiBaseUrl.TrimEnd('/');
        _manifestUrlOverride = manifestUrlOverride;
    }

    private string ManifestUrl => string.IsNullOrWhiteSpace(_manifestUrlOverride)
        ? $"{_apiBaseUrl}/api/launcher/materials"
        : _manifestUrlOverride;

    /// <summary>物料清单缓存：materialId → 物料描述。</summary>
    private Dictionary<string, MaterialInfo> _manifest = new();

    /// <summary>本地物料目录：%APPDATA%\xHR\materials\<materialId>。</summary>
    public static string MaterialDir(string materialId) =>
        Path.Combine(MaterialsRootDir, SanitizeFileName(materialId));

    /// <summary>
    /// 拉取物料清单。失败抛异常；成功后缓存并返回各物料描述。
    /// </summary>
    public async Task<IReadOnlyList<MaterialInfo>> LoadManifestAsync(CancellationToken ct = default)
    {
        var json = await _http.GetStringAsync(ManifestUrl, ct);
        var manifest = JsonSerializer.Deserialize<MaterialManifest>(json, JsonOpts);
        _manifest = (manifest?.Materials ?? new List<MaterialInfo>())
            .Where(m => !string.IsNullOrWhiteSpace(m.MaterialId))
            .ToDictionary(m => m.MaterialId, StringComparer.OrdinalIgnoreCase);
        return _manifest.Values.ToList();
    }

    /// <summary>按物料 id 取清单描述；未登记返回 null。</summary>
    public MaterialInfo? GetMaterial(string materialId) =>
        _manifest.TryGetValue(materialId, out var info) ? info : null;

    /// <summary>
    /// 判断本地物料是否已就位（物料目录存在且入口文件存在）。
    /// 未登记物料、目录缺失或入口缺失均视为未就位。
    /// </summary>
    public bool IsInstalled(string materialId)
    {
        if (string.IsNullOrWhiteSpace(materialId)) return false;
        var dir = MaterialDir(materialId);
        if (!Directory.Exists(dir)) return false;

        var info = GetMaterial(materialId);
        if (info is not null && !string.IsNullOrWhiteSpace(info.EntryExe))
        {
            return File.Exists(Path.GetFullPath(Path.Combine(dir, info.EntryExe)));
        }
        // 清单未给入口时，只要目录非空即视为已就位（启动阶段再解析卡片配置）。
        return Directory.EnumerateFiles(dir, "*", SearchOption.AllDirectories).Any();
    }

    /// <summary>
    /// 下载并安装指定物料：下载 zip → SHA256 校验 → 解压到物料目录（覆盖旧版本）。
    /// 进度回调：已下载字节 / 总字节（未知为 0）。返回物料解压目录。
    /// </summary>
    public async Task<string> InstallMaterialAsync(
        string materialId,
        IProgress<(long Received, long Total)>? progress = null,
        CancellationToken ct = default)
    {
        var info = GetMaterial(materialId)
            ?? throw new InvalidOperationException($"物料清单中未登记「{materialId}」");

        if (string.IsNullOrWhiteSpace(info.PackageUrl))
            throw new InvalidOperationException($"物料「{materialId}」缺少下载地址");

        Directory.CreateDirectory(MaterialStagingDir);
        Directory.CreateDirectory(MaterialsRootDir);
        var zipPath = Path.Combine(MaterialStagingDir, $"{SanitizeFileName(materialId)}.zip");

        using var response = await _http.GetAsync(info.PackageUrl, HttpCompletionOption.ResponseHeadersRead, ct);
        response.EnsureSuccessStatusCode();

        var total = response.Content.Headers.ContentLength ?? 0;
        await using var src = await response.Content.ReadAsStreamAsync(ct);
        await using var dst = new FileStream(zipPath, FileMode.Create, FileAccess.Write, FileShare.None);

        var buffer = new byte[81920];
        long received = 0;
        int read;
        while ((read = await src.ReadAsync(buffer, ct)) > 0)
        {
            await dst.WriteAsync(buffer.AsMemory(0, read), ct);
            received += read;
            progress?.Report((received, total));
        }

        // 哈希校验（清单提供 sha256 才校验）
        if (!string.IsNullOrWhiteSpace(info.Sha256))
        {
            var actual = await ComputeSha256Async(zipPath, ct);
            if (!string.Equals(actual, info.Sha256.Trim().ToLowerInvariant(), StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("物料包哈希校验失败，文件可能已损坏或被篡改");
        }

        var targetDir = MaterialDir(materialId);
        if (Directory.Exists(targetDir)) Directory.Delete(targetDir, true);
        Directory.CreateDirectory(targetDir);
        ZipFile.ExtractToDirectory(zipPath, targetDir, overwriteFiles: true);

        // 解压完成后清理下载的 zip，保留解压目录；下次可重新下载覆盖。
        try { File.Delete(zipPath); } catch { /* 占用中则保留，不阻断 */ }

        return targetDir;
    }

    /// <summary>
    /// 解析物料入口的绝对路径。优先用清单登记的入口；清单缺失时回退卡片配置。
    /// 物料目录不存在返回空串。
    /// </summary>
    public string ResolveEntryExe(MountedApp app)
    {
        var info = GetMaterial(app.MaterialId);
        if (info is not null && !string.IsNullOrWhiteSpace(info.EntryExe))
            return Path.GetFullPath(Path.Combine(MaterialDir(app.MaterialId), info.EntryExe));

        // 回退：卡片配置里记录的相对路径（相对物料目录）。
        if (!string.IsNullOrWhiteSpace(app.ExePath))
            return Path.GetFullPath(Path.Combine(MaterialDir(app.MaterialId), app.ExePath));

        return string.Empty;
    }

    /// <summary>解析物料工作目录（绝对路径）；清单优先，其次卡片配置，默认入口所在目录。</summary>
    public string ResolveWorkDir(MountedApp app)
    {
        var info = GetMaterial(app.MaterialId);
        if (info is not null && !string.IsNullOrWhiteSpace(info.WorkDir))
            return Path.GetFullPath(Path.Combine(MaterialDir(app.MaterialId), info.WorkDir));

        if (!string.IsNullOrWhiteSpace(app.WorkingDirectory))
            return Path.GetFullPath(Path.Combine(MaterialDir(app.MaterialId), app.WorkingDirectory));

        var exe = ResolveEntryExe(app);
        return string.IsNullOrWhiteSpace(exe) ? MaterialDir(app.MaterialId) : Path.GetDirectoryName(exe) ?? MaterialDir(app.MaterialId);
    }

    /// <summary>解析启动参数（清单优先，其次卡片配置）。</summary>
    public string ResolveArguments(MountedApp app)
    {
        var info = GetMaterial(app.MaterialId);
        if (info is not null && info.EntryArgs is not null)
            return info.EntryArgs;
        return app.Arguments ?? "";
    }

    private static async Task<string> ComputeSha256Async(string path, CancellationToken ct)
    {
        await using var fs = File.OpenRead(path);
        using var sha = SHA256.Create();
        var hash = await sha.ComputeHashAsync(fs, ct);
        return Convert.ToHexString(hash).ToLowerInvariant();
    }

    private static string SanitizeFileName(string name)
    {
        var invalid = Path.GetInvalidFileNameChars();
        return new string(name.Select(c => invalid.Contains(c) ? '_' : c).ToArray());
    }
}
