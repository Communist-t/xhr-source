# -*- coding: utf-8 -*-
"""
销售策略与高情商沟通官 桌面 GUI 启动器

后台线程起本地 HTTP API（复用 app.py 的 Handler），
主线程用 pywebview 开一个原生桌面窗口加载前端页面。
不弹浏览器、没有黑窗（用 pythonw.exe 跑）。
"""
from __future__ import annotations

import ctypes
import sys
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(RUNTIME_DIR))

import app  # noqa: E402
import webview  # noqa: E402


class ExportApi:
    """暴露给前端 JS 的本地能力：名单导出走系统「另存为」对话框。

    WebView2 默认不处理 blob 附件下载，前端直接点链接/对象 URL 不会存文件，
    所以由 Python 侧生成内容、弹原生保存框、写盘后回传路径。
    """

    def __init__(self):
        # 注意：窗口引用必须用下划线私有属性。pywebview 会反射遍历 js_api 的公开
        # 属性，若把 Window 对象挂在公开属性上，会顺着 native/AccessibilityObject
        # 一路递归到栈溢出，导致界面卡死。下划线开头的属性会被反射跳过。
        self._window = None

    def _set_window(self, window):
        self._window = window

    def export_contacts(self, fmt: str = ""):
        """弹原生保存框，用户在框里选格式（CSV/TXT/Excel/Word）后生成对应文件。

        fmt 可传入指定格式；留空则按保存框所选过滤器/文件后缀决定。
        """
        known = ("csv", "txt", "xlsx", "docx")
        fmt = (fmt or "").lower()
        if fmt not in known:
            fmt = ""

        if not app._load_export_contacts():
            return {"ok": False, "error": "名单为空，无可导出联系人"}
        if not self._window:
            return {"ok": False, "error": "窗口未初始化"}

        filters = [
            ("CSV 名单", "csv"),
            ("TXT 名单", "txt"),
            ("Excel 名单", "xlsx"),
            ("Word 名单", "docx"),
        ]
        # pywebview 过滤器格式为「描述 (*.ext)」，竖线后的扩展名由框架自己拼
        file_types = tuple(f"{label} (*.{ext})" for label, ext in filters)
        default_name = f"contacts_export.{fmt or 'csv'}"

        try:
            result = self._window.create_file_dialog(
                webview.SAVE_DIALOG,
                save_filename=default_name,
                file_types=file_types,
            )
        except Exception as e:
            return {"ok": False, "error": f"保存失败: {e}"}
        if not result:
            return {"ok": False, "canceled": True}

        path = Path(result[0])
        suffix = path.suffix.lower().lstrip(".")
        if suffix in known:
            fmt = suffix
        else:
            # 路径没带可识别后缀，按默认/所选类型补上扩展名
            fmt = fmt or "csv"
            path = path.with_suffix("." + fmt)

        try:
            body, _ = app.build_contacts_export(fmt)
            path.write_bytes(body)
            return {"ok": True, "path": str(path), "format": fmt}
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        except Exception as e:
            return {"ok": False, "error": f"保存失败: {e}"}


def _set_app_user_model_id():
    """Windows 下给进程一个独立 AppUserModelID，任务栏才会按本应用分组并采用窗口图标，
    而不是归到 pythonw.exe 的默认图标。失败静默忽略，不影响启动。"""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("com.lobsterai.wechatrpa")
    except Exception:
        pass


def _run_server():
    server = ThreadingHTTPServer((app.HOST, app.PORT), app.Handler)
    server.serve_forever()


def main():
    _set_app_user_model_id()
    # 初始化
    app.LOGS_DIR.mkdir(exist_ok=True)
    app.WEB_DIR.mkdir(exist_ok=True)
    app._started_at = time.time()

    # 后台起 API 服务
    t = threading.Thread(target=_run_server, daemon=True)
    t.start()
    time.sleep(0.6)  # 等端口绑定

    url = f"http://{app.HOST}:{app.PORT}/"
    export_api = ExportApi()
    window = webview.create_window(
        "销售策略与高情商沟通官",
        url,
        width=1200,
        height=800,
        min_size=(960, 640),
        background_color="#ffffff",
        js_api=export_api,
    )
    export_api._set_window(window)

    def on_closed():
        # 窗口关闭时收拾 RPA 子进程
        with app._state_lock:
            for p in (app._monitor_proc, app._moments_proc):
                if p and p.poll() is None:
                    try:
                        p.terminate()
                    except Exception:
                        pass
        # 句柄之外的遗留监控（比如上一次启动留下的）也一并停掉，
        # 否则关掉界面后监控还在后台闷着跑
        try:
            app._kill_detached_monitor()
        except Exception:
            pass

    window.events.closed += on_closed

    # 窗口/任务栏图标（与网页 favicon 同源的 app.ico；文件缺失则回退默认，不影响启动）
    icon_path = RUNTIME_DIR / "app.ico"
    webview.start(debug=False, icon=str(icon_path) if icon_path.is_file() else None)


if __name__ == "__main__":
    main()
