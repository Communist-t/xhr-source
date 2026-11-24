using System.Diagnostics;
using System.IO;
using System.Net.Http;
using System.Security.Cryptography;
using System.Text.Json;
using System.Text.Json.Serialization;
using Launcher.Models;
using Microsoft.Win32;

namespace Launcher.Services;

/// <summary>
/// 完整客户端（安装型应用）管理服务。
///
/// 与物料型（绿色 zip 解压即用）不同，完整客户端是 NSIS/MSI 安装包：
/// 1) 复用后端 <c>/api/version/all</c> 桌面渠道（desktop.files）登记安装包文件名、哈希、大小与版本；
/// 2) 首次点用时下载安装包，拉起官方安装向导（NSIS assisted，/D 预置独立默认目录）；
/// 3) 安装后通过注册表 appId 探测 InstallLocation，定位入口 exe；
/// 4) 日常启动直接从安装目录拉起 exe，不重复运行安装包；仅远端版本高于本地登记版本时才重装修复。
/// 5) 安装登记（目录、版本）写入本地 install.json，避免每次启动都扫注册表。
/// </summary>
public class FullClientService
{
    private readonly HttpClient _http;
    private readonly string _apiBase;

    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = System.Text.Json.Serialization.JsonIgnoreCondition.WhenWritingNull
    };

    /// <summary>本地运行态目录（%APPDATA%\xHR）。</summary>
    public static string LocalDataDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "xHR");

    /// <summary>安装包下载暂存目录。</summary>
    private static string InstallerStagingDir => Path.Combine(LocalDataDir, "full-client-download");

    /// <summary>安装登记文件：appId → 安装目录 / 已装版本。</summary>
    private static string InstallRegistryFile => Path.Combine(LocalDataDir, "full-client-installs.json");

    /// <summary>
    /// 完整版默认安装目录。必须与壳子目录（%LOCALAPPDATA%\Programs\xHR）错开：
    /// 完整版 productName 也是 x.HR，NSIS 默认落同一目录，会覆盖壳子文件，
    /// 且其安装脚本会强杀 INSTDIR 下全部进程，壳子与员工子进程会被连坐。
    /// NSIS /D 参数不接受带空格路径。
    /// </summary>
    private static string DefaultInstallDir => Path.Combine(
        Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
        "Programs", "x.HR-Client");

    public FullClientService(string apiBaseUrl, string? authToken = null)
    {
        _apiBase = apiBaseUrl.TrimEnd('/');
        _http = new HttpClient { Timeout = TimeSpan.FromMinutes(10) };
        _http.DefaultRequestHeaders.Add("User-Agent", "xHR-Launcher-FullClient");
        if (!string.IsNullOrWhiteSpace(authToken))
            _http.DefaultRequestHeaders.Authorization =
                new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", authToken);
    }

    // ===== 远端清单（/api/version/all 桌面渠道）=====

    private sealed class VersionResponse
    {
        [JsonPropertyName("desktop")] public DesktopChannel? Desktop { get; set; }
    }

    private sealed class DesktopChannel
    {
        [JsonPropertyName("version")] public string Version { get; set; } = "";
        [JsonPropertyName("files")] public List<DesktopFile> Files { get; set; } = new();
    }

    private sealed class DesktopFile
    {
        [JsonPropertyName("url")] public string Url { get; set; } = "";
        [JsonPropertyName("sha512")] public string Sha512 { get; set; } = "";
        [JsonPropertyName("sha256")] public string Sha256 { get; set; } = "";
        [JsonPropertyName("size")] public long Size { get; set; }
        [JsonPropertyName("path")] public string Path { get; set; } = "";
    }

    /// <summary>远端完整客户端安装包描述。</summary>
    public sealed record RemotePackage(
        string Version,
        string FileName,
        string DownloadUrl,
        string ExpectedHash,
        bool HashIsSha512,
        bool HashIsBase64,
        long Size);

    /// <summary>拉取 /api/version/all，解析 desktop 渠道首个安装包；渠道未发布返回 null。</summary>
    public async Task<RemotePackage?> LoadRemoteAsync(CancellationToken ct = default)
    {
        var url = $"{_apiBase}/api/version/all";
        var json = await _http.GetStringAsync(url, ct);
        var resp = JsonSerializer.Deserialize<VersionResponse>(json, JsonOpts);

        var file = resp?.Desktop?.Files.FirstOrDefault(f => !string.IsNullOrWhiteSpace(f.Url));
        if (file is null) return null;

        var downloadUrl = ResolveUrl(file.Url);
        var version = resp!.Desktop?.Version ?? "";

        bool useSha512 = !string.IsNullOrWhiteSpace(file.Sha512);
        var expected = useSha512 ? file.Sha512 : file.Sha256;
        // 文档中的 sha512 为 Base64 编码（结尾 ==）；Hex 形式按十六进制处理。
        var isBase64 = expected.EndsWith("==", StringComparison.Ordinal) ||
                       (useSha512 && expected.Length == 88);

        return new RemotePackage(version, Path.GetFileName(file.Url), downloadUrl, expected, useSha512, isBase64, file.Size);
    }

    private string ResolveUrl(string raw)
    {
        if (string.IsNullOrWhiteSpace(raw)) return "";
        if (raw.StartsWith("http://", StringComparison.OrdinalIgnoreCase) ||
            raw.StartsWith("https://", StringComparison.OrdinalIgnoreCase))
            return raw;
        // 后端 files[].url 可能是裸文件名（app-Setup-x.y.z.exe），按上传目录约定补前缀。
        var rel = raw.StartsWith('/') ? raw : "/uploads/software/" + raw;
        return _apiBase + rel;
    }

    // ===== 本地安装登记 =====

    private sealed class InstallRecord
    {
        [JsonPropertyName("install_dir")] public string InstallDir { get; set; } = "";
        [JsonPropertyName("installed_version")] public string InstalledVersion { get; set; } = "";
        [JsonPropertyName("entry_exe")] public string EntryExe { get; set; } = "";
    }

    private static Dictionary<string, InstallRecord> LoadRecords()
    {
        try
        {
            if (!File.Exists(InstallRegistryFile)) return new();
            var json = File.ReadAllText(InstallRegistryFile);
            return JsonSerializer.Deserialize<Dictionary<string, InstallRecord>>(json, JsonOpts) ?? new();
        }
        catch { return new(); }
    }

    private static void SaveRecords(Dictionary<string, InstallRecord> records)
    {
        try
        {
            Directory.CreateDirectory(LocalDataDir);
            var json = JsonSerializer.Serialize(records, JsonOpts);
            File.WriteAllText(InstallRegistryFile, json);
        }
        catch { /* 登记写入失败不阻断启动 */ }
    }

    private static string RecordKey(MountedApp app) =>
        !string.IsNullOrWhiteSpace(app.RegistryAppId) ? app.RegistryAppId : app.Id;

    /// <summary>本地是否已就位：登记目录存在且入口 exe 存在；登记缺失时回退注册表探测。</summary>
    public bool IsInstalled(MountedApp app)
    {
        var record = LoadRecords().TryGetValue(RecordKey(app), out var r) ? r : null;
        var dir = record?.InstallDir;
        var entry = record?.EntryExe ?? app.EntryExe;

        if (string.IsNullOrWhiteSpace(dir))
        {
            dir = DetectInstallDir(app.RegistryAppId, app.EntryExe);
        }
        if (string.IsNullOrWhiteSpace(dir) || string.IsNullOrWhiteSpace(entry)) return false;
        return File.Exists(Path.Combine(dir, entry));
    }

    /// <summary>解析入口 exe 绝对路径；未安装返回空串。</summary>
    public string ResolveEntryExe(MountedApp app)
    {
        var record = LoadRecords().TryGetValue(RecordKey(app), out var r) ? r : null;
        var dir = record?.InstallDir;
        var entry = record?.EntryExe ?? app.EntryExe;
        if (string.IsNullOrWhiteSpace(dir)) dir = DetectInstallDir(app.RegistryAppId, app.EntryExe);
        if (string.IsNullOrWhiteSpace(dir) || string.IsNullOrWhiteSpace(entry)) return "";
        var exe = Path.Combine(dir, entry);
        return File.Exists(exe) ? exe : "";
    }

    /// <summary>已登记的本地版本号；未登记返回空串。</summary>
    public string LocalVersion(MountedApp app) =>
        LoadRecords().TryGetValue(RecordKey(app), out var r) ? r.InstalledVersion : "";

    /// <summary>
    /// 注册表探测安装目录。优先 HKCU，回退 HKLM；
    /// 读 <c>Software\&lt;appId&gt;</c> 的 InstallLocation，再回退卸载项 DisplayIcon 反推目录。
    /// </summary>
    public string? DetectInstallDir(string appId, string? entryExe = null)
    {
        if (string.IsNullOrWhiteSpace(appId)) return null;

        foreach (var root in new[] { RegistryHive.CurrentUser, RegistryHive.LocalMachine })
        {
            using var appKey = RegistryKey.OpenBaseKey(root, RegistryView.Registry32)
                .OpenSubKey($@"Software\{appId}");
            var loc = appKey?.GetValue("InstallLocation") as string;
            if (!string.IsNullOrWhiteSpace(loc) && Directory.Exists(loc)) return loc;
        }

        // 卸载项：DisplayIcon 形如 "D:\...\x.HR.exe,0"，反推目录。
        // 必须按入口 exe 文件名过滤：不过滤会按遍历顺序命中任意其他软件的卸载项
        // （实测曾误取 DeepSeek 的目录并把 DeepSeek Harness.exe 登记为入口）。
        foreach (var root in new[] { RegistryHive.CurrentUser, RegistryHive.LocalMachine })
        {
            foreach (var sub in new[] { @"Software\Microsoft\Windows\CurrentVersion\Uninstall",
                                        @"Software\WOW6432Node\Microsoft\Windows\CurrentVersion\Uninstall" })
            {
                using var uninst = RegistryKey.OpenBaseKey(root, RegistryView.Registry32).OpenSubKey(sub);
                if (uninst is null) continue;
                foreach (var name in uninst.GetSubKeyNames())
                {
                    using var item = uninst.OpenSubKey(name);
                    var icon = item?.GetValue("DisplayIcon") as string;
                    if (string.IsNullOrWhiteSpace(icon)) continue;
                    var iconPath = icon.Split(',')[0].Trim('"', ' ');
                    if (!string.IsNullOrWhiteSpace(entryExe) &&
                        !string.Equals(Path.GetFileName(iconPath), entryExe, StringComparison.OrdinalIgnoreCase))
                        continue;
                    var dir = Path.GetDirectoryName(iconPath);
                    if (!string.IsNullOrWhiteSpace(dir) && Directory.Exists(dir)) return dir;
                }
            }
        }
        return null;
    }

    /// <summary>
    /// 下载并安装完整客户端（官方向导 UI）。返回安装目录。
    /// progress：已下载字节 / 总字节；stage：安装阶段中文名（轮询安装器计时日志）。
    /// </summary>
    public async Task<string> InstallAsync(
        MountedApp app,
        RemotePackage package,
        IProgress<(long Received, long Total)>? progress = null,
        IProgress<string>? stage = null,
        CancellationToken ct = default)
    {
        Directory.CreateDirectory(InstallerStagingDir);
        var installerPath = Path.Combine(InstallerStagingDir, SanitizeFileName(package.FileName));

        // 1) 下载安装包
        using (var response = await _http.GetAsync(package.DownloadUrl, HttpCompletionOption.ResponseHeadersRead, ct))
        {
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
        }

        // 2) 哈希校验（清单提供才校验；后端桌面渠道给 sha512 且为 Base64 编码）
        if (!string.IsNullOrWhiteSpace(package.ExpectedHash))
        {
            var actual = await ComputeHashAsync(installerPath, package.HashIsSha512, ct);
            var expected = NormalizeExpectedHash(package.ExpectedHash, package.HashIsBase64);
            if (!string.Equals(actual, expected, StringComparison.OrdinalIgnoreCase))
                throw new InvalidOperationException("完整客户端安装包哈希校验失败，文件可能已损坏或被篡改");
        }

        // 3) 拉起官方安装向导：NSIS assisted 走 /D 预置独立默认目录；msi 走 msiexec /qb 显示进度框。
        //    不再用 /S 静默：安装器 tar 解包阶段耗时长且无界面，用户无法区分慢与卡死。
        //    安装包内嵌 requireAdministrator 清单，UseShellExecute=false 时 CreateProcess 报 740，
        //    必须走 ShellExecute 由系统弹 UAC 提权。
        var ext = Path.GetExtension(installerPath).ToLowerInvariant();
        var isMsi = ext == ".msi";
        var psi = new ProcessStartInfo
        {
            UseShellExecute = true
        };

        if (isMsi)
        {
            psi.FileName = "msiexec.exe";
            psi.Arguments = $"/i \"{installerPath}\" /qb /norestart";
        }
        else
        {
            psi.FileName = installerPath;
            psi.Arguments = $"/D={DefaultInstallDir}";
        }

        using (var installer = Process.Start(psi))
        {
            if (installer is null) throw new InvalidOperationException("安装程序启动失败");
            var watchTask = WatchInstallerPhasesAsync(installer, stage, ct);
            await installer.WaitForExitAsync(ct);
            await watchTask;
            if (installer.ExitCode != 0)
                throw new InvalidOperationException($"安装程序退出，代码 {installer.ExitCode}");
        }

        // 4) 探测安装目录并登记
        var installDir = DetectInstallDir(app.RegistryAppId, app.EntryExe);
        if (string.IsNullOrWhiteSpace(installDir))
        {
            // 兜底：扫常见用户目录下含入口 exe 的目录（仅当入口文件名已知时）。
            installDir = FallbackScan(app.EntryExe);
        }
        if (string.IsNullOrWhiteSpace(installDir))
            throw new InvalidOperationException("安装完成，但未能定位安装目录");

        var entry = app.EntryExe;
        if (!string.IsNullOrWhiteSpace(entry) && !File.Exists(Path.Combine(installDir, entry)))
        {
            // 入口文件名未登记对时，在安装目录里找首个 exe 兜底。
            var found = Directory.EnumerateFiles(installDir, "*.exe", SearchOption.TopDirectoryOnly)
                .FirstOrDefault();
            if (found is not null) entry = Path.GetFileName(found);
        }

        var records = LoadRecords();
        records[RecordKey(app)] = new InstallRecord
        {
            InstallDir = installDir,
            InstalledVersion = package.Version,
            EntryExe = entry
        };
        SaveRecords(records);

        // 清理安装包；占用中则保留，不阻断。
        try { File.Delete(installerPath); } catch { }

        return installDir;
    }

    /// <summary>完整版安装器计时日志的阶段 → 卡片展示文案。</summary>
    private static readonly Dictionary<string, string> PhaseLabels = new()
    {
        ["custom-init-start"] = "初始化",
        ["install-preflight-complete"] = "环境检查",
        ["defender-exclusion-complete"] = "配置安全排除",
        ["payload-materialize-start"] = "释放安装包",
        ["payload-7z-extract-start"] = "解压程序主体",
        ["payload-copy-start"] = "复制文件",
        ["tar-extract-start"] = "解压资源包（耗时较长）",
        ["app-files-install-complete"] = "写入注册信息",
    };

    /// <summary>
    /// 向导运行期间轮询 %APPDATA%\x.HR\install-timing.log（完整版安装器自带逐阶段追加，其 productName 仍是 x.HR），
    /// 把本次安装开始后的最新阶段回显到卡片；日志缺失或占用时静默跳过，不影响等待。
    /// </summary>
    private static async Task WatchInstallerPhasesAsync(
        Process installer, IProgress<string>? stage, CancellationToken ct)
    {
        if (stage is null) return;
        // 完整版 productName 仍是 x.HR，其安装器日志固定写在 %APPDATA%\x.HR，不随壳子目录改名
        var logPath = Path.Combine(
            Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData), "x.HR", "install-timing.log");
        DateTime since;
        try { since = installer.StartTime; } catch { return; }
        var lastLabel = "";

        while (!installer.HasExited)
        {
            try
            {
                if (File.Exists(logPath))
                {
                    using var fs = new FileStream(logPath, FileMode.Open, FileAccess.Read, FileShare.ReadWrite);
                    fs.Seek(0, SeekOrigin.End);
                    var len = (int)Math.Min(fs.Length, 8192);
                    fs.Seek(-len, SeekOrigin.End);
                    var buf = new byte[len];
                    _ = await fs.ReadAsync(buf, 0, len, ct);
                    var lines = System.Text.Encoding.UTF8.GetString(buf).Split('\n');
                    for (var i = lines.Length - 1; i >= 0; i--)
                    {
                        var m = System.Text.RegularExpressions.Regex.Match(
                            lines[i], @"^(\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}) phase=([a-z0-9-]+)");
                        if (!m.Success) continue;
                        if (DateTime.TryParse(m.Groups[1].Value, out var ts) && ts < since) break;
                        if (PhaseLabels.TryGetValue(m.Groups[2].Value, out var label) && label != lastLabel)
                        {
                            lastLabel = label;
                            stage.Report(label);
                        }
                        break;
                    }
                }
            }
            catch { /* 共享冲突/解析失败：下个周期再试 */ }

            try { await Task.Delay(800, ct); }
            catch (OperationCanceledException) { return; }
        }
    }

    private string? FallbackScan(string entryExe)
    {
        if (string.IsNullOrWhiteSpace(entryExe)) return null;
        var roots = new[]
        {
            Environment.GetFolderPath(Environment.SpecialFolder.LocalApplicationData),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFiles),
            Environment.GetFolderPath(Environment.SpecialFolder.ProgramFilesX86)
        };
        foreach (var root in roots.Where(Directory.Exists))
        {
            try
            {
                var hit = Directory.EnumerateFiles(root, entryExe, SearchOption.AllDirectories)
                    .FirstOrDefault();
                if (hit is not null) return Path.GetDirectoryName(hit);
            }
            catch { /* 无权限/长路径跳过 */ }
        }
        return null;
    }

    private static async Task<string> ComputeHashAsync(string path, bool sha512, CancellationToken ct)
    {
        await using var fs = File.OpenRead(path);
        using HashAlgorithm hash = sha512 ? SHA512.Create() : SHA256.Create();
        var bytes = await hash.ComputeHashAsync(fs, ct);
        return Convert.ToHexString(bytes);
    }

    /// <summary>把清单里的哈希归一化为小写十六进制；Base64 先解码再转 Hex。</summary>
    private static string NormalizeExpectedHash(string expected, bool isBase64)
    {
        expected = expected.Trim();
        if (!isBase64) return expected.ToLowerInvariant();
        try
        {
            var bytes = Convert.FromBase64String(expected);
            return Convert.ToHexString(bytes).ToLowerInvariant();
        }
        catch
        {
            return expected.ToLowerInvariant();
        }
    }

    private static string SanitizeFileName(string name)
    {
        var invalid = Path.GetInvalidFileNameChars();
        return new string(name.Select(c => invalid.Contains(c) ? '_' : c).ToArray());
    }
}
