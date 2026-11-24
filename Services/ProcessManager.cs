using System.Diagnostics;
using System.IO;
using Launcher.Models;

namespace Launcher.Services;

/// <summary>
/// 管理三个被挂载 exe 的拉起、状态监控、生命周期。
/// 所有子进程统一进入一个 JobObject，启动器退出时自动全部清理。
/// </summary>
public class ProcessManager : IDisposable
{
    private readonly JobObjectManager _job;
    private readonly Dictionary<string, Process> _running = new();

    /// <summary>进程状态变化事件：(appId, 是否正在运行)</summary>
    public event Action<string, bool>? OnProcessStateChanged;

    public ProcessManager()
    {
        _job = new JobObjectManager();
    }

    /// <summary>
    /// 启动一个被挂载程序。返回是否成功。
    /// 配置中的可执行路径 / 工作目录均按相对程序根目录解析。
    /// </summary>
    public bool Launch(MountedApp app, string? exePathOverride = null, string? workDirOverride = null, string? argumentsOverride = null)
    {
        if (_running.TryGetValue(app.Id, out var existing) && !existing.HasExited)
        {
            // 已经在跑了，不重复启动
            return true;
        }

        // 物料型员工：入口/工作目录由 MaterialService 解析后传入；内置型回退到程序根目录相对路径。
        var exePath = !string.IsNullOrWhiteSpace(exePathOverride)
            ? exePathOverride
            : PathResolver.Resolve(app.ExePath);
        var workDir = !string.IsNullOrWhiteSpace(workDirOverride)
            ? workDirOverride
            : PathResolver.Resolve(app.WorkingDirectory);

        if (!File.Exists(exePath))
        {
            OnProcessStateChanged?.Invoke(app.Id, false);
            return false;
        }

        var psi = new ProcessStartInfo
        {
            FileName = exePath,
            Arguments = argumentsOverride ?? app.Arguments ?? "",
            UseShellExecute = false
        };

        if (!string.IsNullOrWhiteSpace(workDir) && Directory.Exists(workDir))
            psi.WorkingDirectory = workDir;
        else
            psi.WorkingDirectory = Path.GetDirectoryName(exePath) ?? "";

        var proc = new Process { StartInfo = psi, EnableRaisingEvents = true };

        proc.Exited += (s, e) =>
        {
            _running.Remove(app.Id);
            OnProcessStateChanged?.Invoke(app.Id, false);
        };

        if (!proc.Start())
        {
            OnProcessStateChanged?.Invoke(app.Id, false);
            return false;
        }

        // 关键：立刻把进程塞进 JobObject，这样启动器退出时整个进程树都会被系统杀掉
        _job.AssignProcess(proc.Handle);

        _running[app.Id] = proc;
        OnProcessStateChanged?.Invoke(app.Id, true);
        return true;
    }

    /// <summary>
    /// 主动停止某个程序（连同其子进程）。
    /// </summary>
    public void Kill(string appId)
    {
        if (_running.TryGetValue(appId, out var proc) && !proc.HasExited)
        {
            try
            {
                proc.Kraft();
            }
            catch
            {
                // 进程可能已经退出
            }
        }
    }

    public bool IsRunning(string appId)
    {
        return _running.TryGetValue(appId, out var proc) && !proc.HasExited;
    }

    public void Dispose()
    {
        // Dispose JobObjectManager 会 CloseHandle，
        // 由于设置了 KILL_ON_JOB_CLOSE，系统会自动杀掉 Job 内所有进程
        _job.Dispose();
        GC.SuppressFinalize(this);
    }
}

/// <summary>
/// 扩展方法：尽量优雅关闭，失败则强杀
/// </summary>
internal static class ProcessExtensions
{
    public static void Kraft(this Process proc)
    {
        try
        {
            proc.CloseMainWindow();
            if (!proc.WaitForExit(3000))
                proc.Kill(entireProcessTree: true);
        }
        catch
        {
            try { proc.Kill(entireProcessTree: true); } catch { }
        }
    }
}
