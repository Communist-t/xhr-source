using System.Text.Json.Serialization;

namespace Launcher.Models;

/// <summary>
/// 后端 GET /api/version/all 的 lite 渠道信息（壳子专用渠道）。
/// 后端上传时把文件重命名为 app-lite-&lt;版本&gt;.exe，download_url 为相对路径，
/// 需拼 api_base_url 前缀。lite 渠道不提供哈希，下载后跳过校验。
/// </summary>
public class VersionAllResponse
{
    [JsonPropertyName("android")]
    public object? Android { get; set; }

    [JsonPropertyName("desktop")]
    public object? Desktop { get; set; }

    [JsonPropertyName("lite")]
    public UpdateInfo? Lite { get; set; }
}

public class UpdateInfo
{
    [JsonPropertyName("version")]
    public string Version { get; set; } = "";

    [JsonPropertyName("latest_version")]
    public string LatestVersion { get; set; } = "";

    [JsonPropertyName("release_date")]
    public string? ReleaseDate { get; set; }

    [JsonPropertyName("changelog")]
    public List<string>? Changelog { get; set; }

    [JsonPropertyName("download_url")]
    public string DownloadUrl { get; set; } = "";

    /// <summary>壳子目标版本：优先 latest_version，缺失时回退 version。</summary>
    [JsonIgnore]
    public string TargetVersion =>
        !string.IsNullOrWhiteSpace(LatestVersion) ? LatestVersion : Version;

    [JsonIgnore]
    public string ReleaseNotes => Changelog is null or { Count: 0 } ? "" : string.Join("；", Changelog);
}
