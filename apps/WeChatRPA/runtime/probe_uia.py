# -*- coding: utf-8 -*-
"""探测微信窗口的 UIA 控件树，找出'朋友圈''相机''发表'这些按钮的控件名和位置。"""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
from wechat_vision_rpa import ensure_wechat_visible, activate_window

from pywinauto import Application

hwnd = ensure_wechat_visible()
if not hwnd:
    print("no wechat window")
    sys.exit(1)
activate_window(hwnd)
time.sleep(1)

app = Application(backend="uia").connect(handle=hwnd)
win = app.window(handle=hwnd)

print(f"窗口: {win.window_text()}")
print(f"矩形: {win.rectangle()}")
print("=" * 60)

# 遍历所有控件，打印有 name 的
def walk(ctrl, depth=0, max_depth=8):
    if depth > max_depth:
        return
    try:
        name = ctrl.window_text() or ""
        ctype = ctrl.element_info.control_type or ""
        rect = ctrl.rectangle()
        if name.strip():
            print(f"{'  '*depth}[{ctype}] {name!r}  rect=({rect.left},{rect.top},{rect.right},{rect.bottom})")
    except Exception:
        return
    try:
        for child in ctrl.children():
            walk(child, depth+1, max_depth)
    except Exception:
        pass

walk(win)
