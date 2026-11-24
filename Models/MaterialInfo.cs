using System.Text.Json.Serialization;

namespace Launcher.Models;

/// <summary>
/// 单个物料（可下载的子程序员工）描述条目。
/// 物料从壳包剥离，首次点用时按需下载解压到本地物料目录；
/// 清单由发布流水线生成并挂到更新源（Release 附件或后端接口）。
/// </summary>
public class MaterialInfo
{
    /// <summary>物料唯一标识，与 MountedApp.Id 对应（如 wechat-rpa / matrix-video）。</summary>
    [JsonPropertyName("material_id")]
    public string MaterialId { get; set; } = "";

    /// <summary>物料版本号（语义化），用于判断本地物料是否需要更新。</summary>
    [JsonPropertyName("material_version")]
    public string MaterialVersion { get; set; } = "";

    /// <summary>物料包 zip 下载直链。</summary>
    [JsonPropertyName("package_url")]
    public string PackageUrl { get; set; } = "";

    /// <summary>包体 SHA256（小写十六进制），留空跳过校验。</summary>
    [JsonPropertyName("sha256")]
    public string Sha256 { get; set; } = "";

    /// <summary>包体字节大小，0 表示未提供。</summary>
    [JsonPropertyName("size")]
    public long Size { get; set; }

    /// <summary>包内启动入口（相对物料解压根目录）；覆盖 MountedApp 的 ExePath。</summary>
    [JsonPropertyName("entry_exe")]
    public string EntryExe { get; set; } = "";

    /// <summary>启动参数（解释器模式下为脚本名），可空。</summary>
    [JsonPropertyName("entry_args")]
    public string? EntryArgs { get; set; }

    /// <summary>工作目录（相对物料解压根目录），可空，默认入口所在目录。</summary>
    [JsonPropertyName("work_dir")]
    public string? WorkDir { get; set; }
}

/// <summary>
/// 物料清单根结构：物料 id → 物料描述。客户端启动时拉取并与本地物料目录比对。
/// </summary>
public class MaterialManifest
{
    [JsonPropertyName("materials")]
    public List<MaterialInfo> Materials { get; set; } = new();
}
