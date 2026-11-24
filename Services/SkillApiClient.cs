using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;

namespace Launcher.Services;

/// <summary>
/// 技能（云端数字员工）API 客户端，JWT Bearer 鉴权。
/// 对应后端：/api/skills/**。
/// </summary>
public class SkillApiClient
{
    private readonly HttpClient _http;
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };

    public SkillApiClient(string baseUrl, string token)
    {
        _http = new HttpClient { BaseAddress = new Uri(baseUrl) };
        _http.DefaultRequestHeaders.Authorization =
            new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);
    }

    // ===== DTO =====

    /// <summary>云端技能（数字员工）条目。</summary>
    public class SkillItem
    {
        public int Id { get; set; }
        public string Name { get; set; } = "";
        public string Version { get; set; } = "";
        public string Description { get; set; } = "";
        /// <summary>分类：wechat / copywriting / operations 等。</summary>
        public string Category { get; set; } = "";
        public string[] Tags { get; set; } = System.Array.Empty<string>();
        public int DownloadCount { get; set; }
        public int UsageCount { get; set; }
        /// <summary>是否已上线（status=active）。</summary>
        public bool IsActive { get; set; } = true;
        /// <summary>是否公开（is_public）。</summary>
        public bool IsPublic { get; set; } = true;

        /// <summary>公开状态展示名：公开 / 私密。</summary>
        public string VisibilityLabel => IsPublic ? "公开" : "私密";

        /// <summary>下载次数展示文案。</summary>
        public string DownloadLabel => $"下载 {DownloadCount}";

        /// <summary>
        /// 归属分组（依据 tags 判定，不用 category：后端 category 实际为 office/other/tool，无法表达归属）。
        /// tags 含 wechat/微信 → 微信客服专员；其余（办公/文案类）→ x.HR 完整版。
        /// 矩阵运营专员是独立完整员工，不挂任何子员工，因此不参与分组。
        /// </summary>
        public string CategoryLabel
        {
            get
            {
                var tags = Tags ?? System.Array.Empty<string>();
                bool Has(string t) => System.Array.Exists(tags, x =>
                    string.Equals(x, t, StringComparison.OrdinalIgnoreCase));

                if (Has("wechat") || Has("微信") || Has("微信客服") || Has("微信聊天")) return "微信员工";
                return "x.HR 员工";
            }
        }

        /// <summary>归属客户端 ID，用于点击后拉起对应本地程序。</summary>
        public string OwnerAppId => CategoryLabel == "微信员工" ? "wechat-rpa" : "full-client";

        /// <summary>归属客户端展示名，用于分组标题标注"在哪用"。</summary>
        public string OwnerAppLabel => CategoryLabel == "微信员工" ? "微信可用" : "x.HR 可用";
    }

    private class ErrorResp
    {
        public string? Message { get; set; }
        public string? Msg { get; set; }
    }

    /// <summary>兼容后端字段；status 取值 active / deleted，公开字段 is_public。</summary>
    private class SkillItemRaw
    {
        public int Id { get; set; }
        public string Name { get; set; } = "";
        public string Version { get; set; } = "";
        public string Description { get; set; } = "";
        public string Category { get; set; } = "";
        public string[]? Tags { get; set; }
        public int DownloadCount { get; set; }
        public int UsageCount { get; set; }
        public bool IsPublic { get; set; } = true;
        public string Status { get; set; } = "active";
    }

    // ===== 公开方法 =====

    /// <summary>拉取我发布的技能（上线状态）。失败抛异常。</summary>
    public async Task<List<SkillItem>> GetMySkillsAsync()
    {
        var resp = await _http.GetAsync("/api/skills/user");
        if (!resp.IsSuccessStatusCode) throw new Exception(await ParseError(resp));

        var body = await resp.Content.ReadAsStringAsync();
        // 兼容两种返回：数组本身，或 { "skills": [...] } / { "data": [...] } 包装。
        var wrapper = JsonSerializer.Deserialize<Dictionary<string, JsonElement>>(body, JsonOpts);
        JsonElement listEl = default;
        if (wrapper is not null)
        {
            foreach (var key in new[] { "skills", "data", "items", "results" })
            {
                if (wrapper.TryGetValue(key, out var el) && el.ValueKind == JsonValueKind.Array)
                {
                    listEl = el;
                    break;
                }
            }
        }

        List<SkillItemRaw>? raw;
        if (listEl.ValueKind == JsonValueKind.Array)
        {
            raw = JsonSerializer.Deserialize<List<SkillItemRaw>>(listEl.GetRawText(), JsonOpts);
        }
        else
        {
            raw = JsonSerializer.Deserialize<List<SkillItemRaw>>(body, JsonOpts);
        }

        var list = new List<SkillItem>();
        foreach (var r in raw ?? new List<SkillItemRaw>())
        {
            list.Add(new SkillItem
            {
                Id = r.Id,
                Name = r.Name,
                Version = r.Version,
                Description = r.Description,
                Category = r.Category,
                Tags = r.Tags ?? System.Array.Empty<string>(),
                DownloadCount = r.DownloadCount,
                UsageCount = r.UsageCount,
                IsPublic = r.IsPublic,
                IsActive = string.Equals(r.Status, "active", StringComparison.OrdinalIgnoreCase)
            });
        }

        return list;
    }

    private static async Task<string> ParseError(HttpResponseMessage resp)
    {
        try
        {
            var body = await resp.Content.ReadAsStringAsync();
            var err = JsonSerializer.Deserialize<ErrorResp>(body, JsonOpts);
            return err?.Message ?? err?.Msg ?? $"请求失败 ({(int)resp.StatusCode})";
        }
        catch
        {
            return $"请求失败 ({(int)resp.StatusCode})";
        }
    }
}
