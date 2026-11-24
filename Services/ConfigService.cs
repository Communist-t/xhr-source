using System.IO;
using System.Text.Json;
using Launcher.Models;

namespace Launcher.Services;

/// <summary>
/// config.json 根结构
/// </summary>
public class LauncherConfig
{
    /// <summary>后端 API 地址，如 http://127.0.0.1:8080</summary>
    public string ApiBaseUrl { get; set; } = "http://127.0.0.1:8080";

    /// <summary>更新清单地址；留空则用 {ApiBaseUrl}/api/version（后端现成接口，desktop 渠道）。
    /// 仅当需要指向独立测试源时才填写覆盖值。</summary>
    public string UpdateManifestUrl { get; set; } = "";

    /// <summary>物料清单地址（materials.json 直链）；留空则用 {ApiBaseUrl}/api/launcher/materials。
    /// 描述各子程序员工物料包的版本、下载直链与入口；物料按需下载到本地物料目录。</summary>
    public string MaterialManifestUrl { get; set; } = "";

    public List<MountedApp> Apps { get; set; } = new();
}

/// <summary>
/// 负责 config.json 的读写。配置文件放在 exe 同目录下。
/// </summary>
public class ConfigService
{
    private readonly string _configPath;
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower
    };

    public LauncherConfig Config { get; private set; } = new();

    public ConfigService()
    {
        var baseDir = AppContext.BaseDirectory;
        _configPath = Path.Combine(baseDir, "config.json");
        Load();
    }

    private void Load()
    {
        if (!File.Exists(_configPath))
        {
            Config = DefaultConfig();
            Save();
            return;
        }

        var json = File.ReadAllText(_configPath);
        Config = JsonSerializer.Deserialize<LauncherConfig>(json, JsonOpts) ?? DefaultConfig();
    }

    /// <summary>
    /// 默认配置：挂载各子程序员工。微信客服专员、矩阵运营专员随壳包内置，
    /// 启动入口/参数/工作目录直接登记在本地；x.HR 完整版为安装型，首次点用下载静默安装后从安装目录拉起。
    /// </summary>
    private static LauncherConfig DefaultConfig() => new()
    {
        ApiBaseUrl = "http://saas-ai.cn:8080",
        Apps = new List<MountedApp>
        {
            new()
            {
                Id = "wechat-rpa",
                Name = "微信客服专员",
                Description = "值守微信聊天、及时回复客户，并按队列发布朋友圈",
                Tags = "智能销售,售后服务,链接推销,数据统计",
                ExePath = "apps/WeChatRPA/runtime/python/pythonw.exe",
                Arguments = "gui.py",
                WorkingDirectory = "apps/WeChatRPA/runtime",
                CoverPath = "Assets/covers/wechat-rpa.jpg"
            },
            new()
            {
                Id = "matrix-video",
                Name = "矩阵运营专员",
                Description = "负责多账号内容的分发运营与账号维护",
                Tags = "内容创作,多平台分发,数据分析,账号管理",
                ExePath = "appr/matrixmedia/matrixmedia.exe",
                WorkingDirectory = "appr/matrixmedia",
                CoverPath = "Assets/covers/matrix-video.jpg"
            },
            new()
            {
                Id = "full-client",
                Kind = "installer",
                Name = "x.HR 完整版",
                Description = "完整功能桌面端，下载安装后可直接启动",
                Tags = "完整客户端,桌面端",
                EntryExe = "x.HR.exe",
                RegistryAppId = "com.lobsterai.app",
                CoverPath = "Assets/covers/full-client.png"
            }
        }
    };

    public void Save()
    {
        var json = JsonSerializer.Serialize(Config, JsonOpts);
        File.WriteAllText(_configPath, json);
    }
}
