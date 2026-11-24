using System.IO;

namespace Launcher.Services;

/// <summary>
/// 路径解析：配置里的路径一律写成相对程序根目录（exe 所在目录）的相对路径，
/// 这里统一解析成绝对路径。绝对路径原样返回，便于兼容手工配置。
/// </summary>
public static class PathResolver
{
    /// <summary>程序根目录（exe / 程序集所在目录）。</summary>
    public static string BaseDir => AppContext.BaseDirectory;

    /// <summary>
    /// 把配置项里的路径解析为绝对路径。
    /// 相对路径拼到 <see cref="BaseDir"/> 下；空串返回空串；绝对路径原样返回。
    /// </summary>
    public static string Resolve(string? path)
    {
        if (string.IsNullOrWhiteSpace(path)) return string.Empty;
        if (Path.IsPathRooted(path)) return Path.GetFullPath(path);
        return Path.GetFullPath(Path.Combine(BaseDir, path));
    }

    /// <summary>同 <see cref="Resolve"/>，但返回目录路径（不校验存在性）。</summary>
    public static string ResolveDir(string? path) => Resolve(path);
}
