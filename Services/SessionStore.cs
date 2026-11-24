using System.IO;
using System.Text.Json;
using Launcher.Models;

namespace Launcher.Services;

/// <summary>
/// 会话存储：把登录后的用户信息写到 %APPDATA%\xHR\session.json
/// 其他挂载的 exe 启动后读这个文件就能拿到 token / api_key / 用户信息。
/// </summary>
public class SessionStore
{
    private readonly string _dir;
    private readonly string _path;

    public SessionStore()
    {
        var appData = Environment.GetFolderPath(Environment.SpecialFolder.ApplicationData);
        _dir = Path.Combine(appData, "xHR");
        _path = Path.Combine(_dir, "session.json");
        Directory.CreateDirectory(_dir);
    }

    public void Save(UserSession session)
    {
        var json = JsonSerializer.Serialize(session, new JsonSerializerOptions { WriteIndented = true });
        File.WriteAllText(_path, json);
    }

    public UserSession? Load()
    {
        if (!File.Exists(_path)) return null;
        try
        {
            var json = File.ReadAllText(_path);
            return JsonSerializer.Deserialize<UserSession>(json);
        }
        catch
        {
            return null;
        }
    }

    public void Clear()
    {
        if (File.Exists(_path)) File.Delete(_path);
    }

    /// <summary>session.json 的完整路径，供其他 exe 读取</summary>
    public string SessionFilePath => _path;
}
