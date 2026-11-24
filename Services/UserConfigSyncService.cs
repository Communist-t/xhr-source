using System.IO;
using System.Text;
using System.Text.Json;
using System.Text.Json.Nodes;
using System.Text.Encodings.Web;
using Launcher.Models;

namespace Launcher.Services;

/// <summary>
/// 把当前登录会话里的账号 ApiKey 同步写入挂载子程序的 API 配置文件（默认 user_config.json）。
///
/// 字段映射原则（对齐旧 Electron wechatRpaController 的做法，但只覆盖密钥，不冲掉已有模型配置）：
///   lobster.api_key          <- session.ApiKey（账号密钥）
///   lobster.base_url         保留现有值；缺失时回退默认对话端点
///   lobster.model            保留现有值（会话里不带模型，不覆盖）
///   lobster.model_fallbacks / moduleId / enable_thinking 等其余字段原样保留
/// 注意：session.ApiBaseUrl 是后端业务接口地址（如 http://saas-ai.cn:8080），
/// 不是对话端点，不能拿来覆盖 base_url。
/// </summary>
public class UserConfigSyncService
{
    private const string DefaultConfigFile = "user_config.json";
    private const string DefaultBaseUrl = "https://saas-ai.cn/v1/chat/completions";

    private static readonly JsonSerializerOptions ReadOpts = new()
    {
        PropertyNameCaseInsensitive = true,
        AllowTrailingCommas = true,
    };

    private static readonly JsonSerializerOptions WriteOpts = new()
    {
        WriteIndented = true,
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        Encoder = JavaScriptEncoder.UnsafeRelaxedJsonEscaping,
    };

    /// <summary>
    /// 把会话 ApiKey 同步到单个挂载项的配置文件。
    /// 会话无 Key、配置文件不存在或解析失败时静默跳过，返回 false。
    /// </summary>
    public bool Sync(MountedApp app, UserSession? session)
    {
        if (session is null || string.IsNullOrWhiteSpace(session.ApiKey))
            return false;

        var workDir = PathResolver.ResolveDir(app.WorkingDirectory);
        var fileName = string.IsNullOrWhiteSpace(app.ConfigFile) ? DefaultConfigFile : app.ConfigFile;
        var configPath = Path.IsPathRooted(fileName)
            ? fileName
            : Path.Combine(workDir, fileName);

        if (!File.Exists(configPath))
            return false;

        JsonObject root;
        try
        {
            var json = File.ReadAllText(configPath);
            root = string.IsNullOrWhiteSpace(json)
                ? new JsonObject()
                : JsonSerializer.Deserialize<JsonObject>(json, ReadOpts) ?? new JsonObject();
        }
        catch
        {
            return false;
        }

        if (!root.TryGetPropertyValue("lobster", out var lobsterNode) || lobsterNode is not JsonObject lobster)
        {
            lobster = new JsonObject();
            root["lobster"] = lobster;
        }

        // 只更新账号密钥；其余配置段保留
        lobster["api_key"] = session.ApiKey;

        if (!TryGetString(lobster, "base_url", out var baseUrl) || string.IsNullOrWhiteSpace(baseUrl))
            lobster["base_url"] = DefaultBaseUrl;

        File.WriteAllText(configPath, JsonSerializer.Serialize(root, WriteOpts), new UTF8Encoding(false));
        return true;
    }

    /// <summary>批量同步所有挂载项，单项失败不影响其他项。</summary>
    public void SyncAll(IEnumerable<MountedApp> apps, UserSession? session)
    {
        foreach (var app in apps)
            Sync(app, session);
    }

    private static bool TryGetString(JsonObject obj, string name, out string value)
    {
        value = string.Empty;
        if (!obj.TryGetPropertyValue(name, out var node) || node is not JsonValue v)
            return false;
        value = v.GetValue<string?>() ?? string.Empty;
        return true;
    }
}
