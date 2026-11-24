using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Launcher.Models;

namespace Launcher.Services;

/// <summary>
/// 后端认证 API 客户端
/// </summary>
public class AuthApiClient
{
    private readonly HttpClient _http;
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };

    public AuthApiClient(string baseUrl)
    {
        _http = new HttpClient { BaseAddress = new Uri(baseUrl) };
    }

    // ===== DTO =====

    private record LoginReq(string Username, string Password);
    private record LoginResp(string AccessToken);

    private class RegisterReq
    {
        public string Username { get; set; } = "";
        public string Email { get; set; } = "";
        public string Password { get; set; } = "";
        // 后端字段为 referral_code（snake_case），与文档对齐
        [JsonPropertyName("referral_code")] public string? ReferralCode { get; set; }
    }

    private class CurrentUserResp
    {
        public int Id { get; set; }
        public string Username { get; set; } = "";
        public string Email { get; set; } = "";
        public string Role { get; set; } = "";
        public string? ReferralCode { get; set; }
        public double RemainingDuration { get; set; }
        public double FrozenDuration { get; set; }
        public double TotalDuration { get; set; }
        public string? Balance { get; set; }
        public string? TotalRecharge { get; set; }
        public string? TotalWithdraw { get; set; }
    }

    private class ApiKeyResp
    {
        public string ApiKey { get; set; } = "";
    }

    private class ErrorResp
    {
        public string? Message { get; set; }
        public string? Msg { get; set; }
    }

    // ===== 公开方法 =====

    /// <summary>登录，返回 access_token；失败抛异常，Message 为后端返回的错误信息</summary>
    public async Task<string> LoginAsync(string username, string password)
    {
        var resp = await _http.PostAsJsonAsync("/auth/login", new LoginReq(username, password), JsonOpts);
        if (!resp.IsSuccessStatusCode)
        {
            var err = await ParseError(resp);
            throw new Exception(err);
        }
        var data = await resp.Content.ReadFromJsonAsync<LoginResp>(JsonOpts);
        return data!.AccessToken;
    }

    /// <summary>注册</summary>
    public async Task RegisterAsync(string username, string email, string password, string? referralCode)
    {
        var body = new RegisterReq
        {
            Username = username,
            Email = email,
            Password = password,
            ReferralCode = referralCode
        };
        var resp = await _http.PostAsJsonAsync("/auth/register", body, JsonOpts);
        if (!resp.IsSuccessStatusCode)
        {
            var err = await ParseError(resp);
            throw new Exception(err);
        }
    }

    /// <summary>用 token 拉当前用户信息</summary>
    public async Task<UserSession> GetCurrentUserAsync(string token)
    {
        _http.DefaultRequestHeaders.Authorization =
            new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);

        var resp = await _http.GetAsync("/api/current_user");
        if (!resp.IsSuccessStatusCode)
        {
            var err = await ParseError(resp);
            throw new Exception(err);
        }
        var u = await resp.Content.ReadFromJsonAsync<CurrentUserResp>(JsonOpts);
        return new UserSession
        {
            AccessToken = token,
            UserId = u!.Id,
            Username = u.Username,
            Email = u.Email,
            Role = u.Role,
            ReferralCode = u.ReferralCode,
            RemainingDuration = u.RemainingDuration,
            FrozenDuration = u.FrozenDuration,
            TotalDuration = u.TotalDuration,
            Balance = u.Balance ?? "0.00",
            TotalRecharge = u.TotalRecharge ?? "0.00",
            TotalWithdraw = u.TotalWithdraw ?? "0.00"
        };
    }

    /// <summary>获取用户 API Key</summary>
    public async Task<string> GetApiKeyAsync(string token)
    {
        _http.DefaultRequestHeaders.Authorization =
            new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);

        var resp = await _http.GetAsync("/auth/get-api-key");
        if (!resp.IsSuccessStatusCode)
        {
            var err = await ParseError(resp);
            throw new Exception(err);
        }
        var data = await resp.Content.ReadFromJsonAsync<ApiKeyResp>(JsonOpts);
        return data!.ApiKey;
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
