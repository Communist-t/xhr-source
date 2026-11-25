# -*- coding: utf-8 -*-
"""
朋友圈按钮坐标校准工具（纯文字流程）

流程：
  1. 自动激活微信主窗口
  2. 引导你依次把鼠标挪到 5 个位置，每步按回车确认
     - 最左侧"发现"图标
     - 发现页里"朋友圈"菜单（需要你先手动点一下发现）
     - 朋友圈 feed 右上角"相机"图标（需要你先手动点一下朋友圈）
     - 编辑弹窗的文字输入框（需要你先手动长按相机弹出来）
     - 编辑弹窗的"发表"按钮
  3. 自动算归一化比例，写回 moments_config.json
"""
from __future__ import annotations

import itertools
import json
import sys
import threading
import time
from pathlib import Path

RUNTIME_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(RUNTIME_DIR))

import pyautogui  # noqa: E402
import win32gui  # noqa: E402
from wechat_vision_rpa import ensure_wechat_visible, activate_window  # noqa: E402

CONFIG_PATH = RUNTIME_DIR / "moments_config.json"


def grab_pos(prompt: str, hwnd: int) -> tuple[float, float]:
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    w, h = r - l, b - t
    print(f"\n  >>> {prompt}")
    print(f"      主窗口矩形: left={l} top={t} size={w}x{h}")

    stop = threading.Event()

    def watch():
        for s in itertools.cycle("|/-\\"):
            if stop.is_set():
                break
            mx, my = pyautogui.position()
            rx = (mx - l) / w
            ry = (my - t) / h
            sys.stdout.write(f"\r      {s} 屏幕=({mx:>4},{my:>4})  相对=({rx:.3f},{ry:.3f})   ")
            sys.stdout.flush()
            time.sleep(0.15)

    threading.Thread(target=watch, daemon=True).start()
    input()
    stop.set()
    sys.stdout.write("\r" + " " * 60 + "\r")

    mx, my = pyautogui.position()
    rx = round((mx - l) / w, 4)
    ry = round((my - t) / h, 4)
    print(f"      已确认: 相对=({rx}, {ry})")
    return rx, ry


def main():
    print("=" * 60)
    print("朋友圈坐标校准（纯文字流程）")
    print("=" * 60)

    main_hwnd = ensure_wechat_visible()
    if not main_hwnd:
        print("[错误] 找不到微信主窗口")
        return
    activate_window(main_hwnd)
    time.sleep(0.5)

    steps = []

    # 1. 发现图标
    steps.append(("discover_icon", grab_pos(
        "【1/5】把鼠标移到最左侧窄边栏的「发现」图标上", main_hwnd)))

    input("\n  现在手动点一下那个发现图标，等发现页出来后按回车... ")

    # 2. 朋友圈菜单
    steps.append(("moments_menu", grab_pos(
        "【2/5】把鼠标移到发现页里的「朋友圈」菜单项上", main_hwnd)))

    input("\n  现在手动点一下「朋友圈」，等 feed 流加载出来后按回车... ")

    # 3. 相机
    steps.append(("camera_button", grab_pos(
        "【3/5】把鼠标移到朋友圈页面右上角的「相机」图标上（从右数第二个）", main_hwnd)))

    input("\n  现在手动长按那个相机图标约1秒，等纯文字编辑弹窗出来后按回车... ")

    # 4. 输入框
    steps.append(("text_box", grab_pos(
        "【4/5】把鼠标移到编辑弹窗里的文字输入区域（'这一刻的想法...'）", main_hwnd)))

    # 5. 发表按钮
    steps.append(("post_button", grab_pos(
        "【5/5】把鼠标移到编辑弹窗底部的「发表」按钮上", main_hwnd)))

    # 写回
    cfg = {}
    if CONFIG_PATH.exists():
        cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    for key, (rx, ry) in steps:
        cfg[key] = {"rx": rx, "ry": ry}
    cfg.setdefault("camera_long_press_seconds", 1.0)

    CONFIG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
    print("\n" + "=" * 60)
    print(f"[完成] 已写回 {CONFIG_PATH.name}")
    for key, (rx, ry) in steps:
        print(f"  {key:20s} = ({rx}, {ry})")
    print("=" * 60)


if __name__ == "__main__":
    main()
