using System.Net.Http;
using System.Net.Http.Json;
using System.Text.Json;
using System.Text.Json.Serialization;
using Launcher.Models;

namespace Launcher.Services;

/// <summary>
/// 钱包 / 支付系统 API 客户端（JWT Bearer 鉴权）。
/// 对应后端接口：/api/wallet/**。
/// </summary>
public class WalletApiClient
{
    private readonly HttpClient _http;
    private static readonly JsonSerializerOptions JsonOpts = new()
    {
        PropertyNamingPolicy = JsonNamingPolicy.SnakeCaseLower,
        PropertyNameCaseInsensitive = true,
        DefaultIgnoreCondition = JsonIgnoreCondition.WhenWritingNull
    };

    public WalletApiClient(string baseUrl, string token)
    {
        _http = new HttpClient { BaseAddress = new Uri(baseUrl) };
        _http.DefaultRequestHeaders.Authorization =
            new System.Net.Http.Headers.AuthenticationHeaderValue("Bearer", token);
    }

    // ===== DTO =====

    /// <summary>钱包余额响应</summary>
    public class WalletBalance
    {
        public string Balance { get; set; } = "0.00";
        public string TotalRecharge { get; set; } = "0.00";
        public string TotalWithdraw { get; set; } = "0.00";
    }

    private record RechargeReq(decimal Amount, string PaymentMethod);

    /// <summary>充值订单创建响应</summary>
    public class RechargeOrder
    {
        public string Message { get; set; } = "";
        public string OrderId { get; set; } = "";
        public long TransactionId { get; set; }
        public PaymentParams? PaymentParams { get; set; }
        public string? PaymentUrl { get; set; }
    }

    public class PaymentParams
    {
        public string OrderId { get; set; } = "";
        public decimal Amount { get; set; }
        public string? CodeUrl { get; set; }
        public bool Mock { get; set; }
    }

    /// <summary>充值订单状态响应</summary>
    public class RechargeStatus
    {
        public string OrderId { get; set; } = "";
        public string Status { get; set; } = "";
        public decimal Amount { get; set; }
        public string? CreatedAt { get; set; }
    }

    /// <summary>支付渠道配置响应：后端实际开通的充值 / 提现方式。</summary>
    public class WalletConfig
    {
        public List<string> SupportPaymentMethods { get; set; } = new();
        public List<string> SupportWithdrawMethods { get; set; } = new();
    }

    private class ErrorResp
    {
        public string? Message { get; set; }
        public string? Msg { get; set; }
    }

    // ===== 公开方法 =====

    /// <summary>获取钱包余额。失败抛异常，Message 为后端返回信息。</summary>
    public async Task<WalletBalance> GetBalanceAsync()
    {
        var resp = await _http.GetAsync("/api/wallet/balance");
        if (!resp.IsSuccessStatusCode) throw new Exception(await ParseError(resp));
        var data = await resp.Content.ReadFromJsonAsync<WalletBalance>(JsonOpts);
        return data ?? new WalletBalance();
    }

    /// <summary>创建充值订单（微信 / 支付宝）。</summary>
    public async Task<RechargeOrder> CreateRechargeAsync(decimal amount, string paymentMethod)
    {
        var resp = await _http.PostAsJsonAsync("/api/wallet/recharge",
            new RechargeReq(amount, paymentMethod), JsonOpts);
        if (!resp.IsSuccessStatusCode) throw new Exception(await ParseError(resp));
        var data = await resp.Content.ReadFromJsonAsync<RechargeOrder>(JsonOpts);
        return data ?? throw new Exception("充值订单创建失败");
    }

    /// <summary>查询充值订单状态：pending / completed / failed。</summary>
    public async Task<RechargeStatus> GetRechargeStatusAsync(string orderId)
    {
        var resp = await _http.GetAsync($"/api/wallet/recharge/status/{Uri.EscapeDataString(orderId)}");
        if (!resp.IsSuccessStatusCode) throw new Exception(await ParseError(resp));
        var data = await resp.Content.ReadFromJsonAsync<RechargeStatus>(JsonOpts);
        return data ?? throw new Exception("订单状态查询失败");
    }

    /// <summary>拉取后端支付渠道配置；接口不可用时返回空列表，由调用方按兜底口径处理。</summary>
    public async Task<WalletConfig> GetWalletConfigAsync()
    {
        var resp = await _http.GetAsync("/api/wallet/config");
        if (!resp.IsSuccessStatusCode) throw new Exception(await ParseError(resp));
        var data = await resp.Content.ReadFromJsonAsync<WalletConfig>(JsonOpts);
        return data ?? new WalletConfig();
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
