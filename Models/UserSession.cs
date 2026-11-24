namespace Launcher.Models;

/// <summary>
/// 登录后的用户会话，会序列化到 session.json 供其他挂载的 exe 读取
/// </summary>
public class UserSession
{
    /// <summary>JWT 访问令牌</summary>
    public string AccessToken { get; set; } = string.Empty;

    /// <summary>用户ID</summary>
    public int UserId { get; set; }

    /// <summary>用户名</summary>
    public string Username { get; set; } = string.Empty;

    /// <summary>邮箱</summary>
    public string Email { get; set; } = string.Empty;

    /// <summary>角色: user / developer / admin</summary>
    public string Role { get; set; } = string.Empty;

    /// <summary>推荐码</summary>
    public string? ReferralCode { get; set; }

    /// <summary>用户 API Key（调平台开放接口用）</summary>
    public string? ApiKey { get; set; }

    /// <summary>剩余时长</summary>
    public double RemainingDuration { get; set; }

    /// <summary>冻结时长</summary>
    public double FrozenDuration { get; set; }

    /// <summary>总时长</summary>
    public double TotalDuration { get; set; }

    /// <summary>钱包当前余额（元，字符串保留两位小数）</summary>
    public string Balance { get; set; } = "0.00";

    /// <summary>累计充值金额（元）</summary>
    public string TotalRecharge { get; set; } = "0.00";

    /// <summary>累计提现金额（元）</summary>
    public string TotalWithdraw { get; set; } = "0.00";

    /// <summary>登录时间（ISO8601）</summary>
    public string LoginAt { get; set; } = string.Empty;

    /// <summary>后端 API 地址</summary>
    public string ApiBaseUrl { get; set; } = string.Empty;
}
