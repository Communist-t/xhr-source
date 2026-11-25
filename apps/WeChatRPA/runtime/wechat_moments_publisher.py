# -*- coding: utf-8 -*-
"""
微信朋友圈自动发布（视觉定位版）

和聊天 RPA 同一套哲学：不写死坐标，每步截图让视觉 AI 找按钮在哪。
流程：
    截图主窗口 → AI 找"发现"图标 → 点
    截图 → AI 找"朋友圈"菜单 → 点
    截图 → AI 找"相机"图标 → 长按
    截图 → AI 找输入框 → 点+粘贴文案
    截图 → AI 找"发表"按钮 → 点
"""
from __future__ import annotations

import argparse
import base64
import io
import json
import sys
import time
from pathlib import Path
from typing import Optional

RUNTIME_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(RUNTIME_DIR))

import requests  # noqa: E402
import pyautogui  # noqa: E402
import pyperclip  # noqa: E402
import win32gui  # noqa: E402
from PIL import Image  # noqa: E402

from wechat_vision_rpa import (  # noqa: E402
    activate_window,
    ensure_wechat_visible,
    _grab_via_printwindow,
    _setup_console_utf8,
    image_to_data_uri,
)

CONFIG_PATH = RUNTIME_DIR / "moments_config.json"
QUEUE_PATH = RUNTIME_DIR / "moments_queue.json"
ASSETS_DIR = RUNTIME_DIR / "screenshots" / "moments"
DEBUG_DIR = RUNTIME_DIR / "screenshots" / "compare"
DEBUG_DIR.mkdir(parents=True, exist_ok=True)
ASSETS_DIR.mkdir(parents=True, exist_ok=True)

DEFAULT_CONFIG = {
    "step_delay_seconds": 2.5,
    "window_wait_seconds": 5.0,
    "post_interval_seconds": 300,
    "camera_long_press_seconds": 2.0,
    "debug_screenshots": True,
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    if CONFIG_PATH.exists():
        try:
            cfg.update(json.loads(CONFIG_PATH.read_text(encoding="utf-8")))
        except Exception:
            pass
    return cfg


# ---------------------------------------------------------------------------
# 视觉定位：截图 → 问 AI 按钮在哪
# ---------------------------------------------------------------------------

def _api_config() -> dict:
    uc_path = RUNTIME_DIR / "user_config.json"
    try:
        if not uc_path.exists():
            return {}
        raw = uc_path.read_text(encoding="utf-8").strip()
        if not raw:
            return {}
        uc = json.loads(raw)
        return uc.get("lobster", {}) if isinstance(uc, dict) else {}
    except Exception:
        return {}


def vision_locate(image: Image.Image, target: str) -> Optional[tuple[float, float]]:
    """截图 + 目标描述 → 返回归一化 (rx, ry)。找不到返回 None。"""
    api = _api_config()
    # 缩小截图到宽 800，减少 base64 体积加快传输和模型处理
    if image.width > 800:
        ratio = 800 / image.width
        image = image.resize((800, int(image.height * ratio)), Image.LANCZOS)
    # 低质量 JPEG，进一步减小体积
    buf = io.BytesIO()
    image.convert("RGB").save(buf, format="JPEG", quality=60)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    data_uri = f"data:image/jpeg;base64,{b64}"
    prompt = (
        f"这是微信PC版窗口截图。请在图中找到【{target}】这个UI元素。"
        f"我需要你点击它的精确中心点。"
        f"只返回JSON：{{\"rx\": 0.0到1.0的小数, \"ry\": 0.0到1.0的小数}}。"
        f"rx是横向位置比例（0=最左边缘，1=最右边缘），ry是纵向位置比例（0=最上边缘，1=最下边缘）。"
        f"返回的坐标必须是该元素可点击区域的正中心。不要解释，只返回JSON。"
    )
    payload = {
        "model": api.get("model", "qwen"),
        "messages": [{
            "role": "user",
            "content": [
                {"type": "image_url", "image_url": {"url": data_uri}},
                {"type": "text", "text": prompt},
            ],
        }],
        "temperature": 0.1,
        "max_tokens": 100,
    }
    headers = {
        "Authorization": f"Bearer {api.get('api_key', '')}",
        "Content-Type": "application/json",
    }
    try:
        r = requests.post(api["base_url"], headers=headers, json=payload, timeout=120)
        text = r.json()["choices"][0]["message"]["content"].strip()
        start = text.find("{")
        end = text.rfind("}")
        if start >= 0 and end > start:
            data = json.loads(text[start:end+1])
            rx = float(data["rx"])
            ry = float(data["ry"])
            return (max(0.0, min(1.0, rx)), max(0.0, min(1.0, ry)))
    except Exception as e:
        print(f"  [视觉] 定位失败: {e}")
    return None


def _debug_shot(img: Image.Image, tag: str):
    if not load_config().get("debug_screenshots"):
        return
    ts = time.strftime("%Y%m%d_%H%M%S")
    p = DEBUG_DIR / f"{ts}_{tag}.png"
    try:
        img.save(p)
        print(f"  [调试] {p.name}")
    except Exception:
        pass


def _click_relative(rx: float, ry: float, hwnd: int, delay: float = 0.5):
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    x = l + int((r - l) * rx)
    y = t + int((b - t) * ry)
    pyautogui.moveTo(x, y, duration=0.2)
    pyautogui.click()
    time.sleep(delay)
    print(f"  [点击] ({rx:.3f},{ry:.3f}) -> 屏幕({x},{y})")


def _long_press_relative(rx: float, ry: float, hwnd: int, seconds: float = 1.0):
    l, t, r, b = win32gui.GetWindowRect(hwnd)
    x = l + int((r - l) * rx)
    y = t + int((b - t) * ry)
    pyautogui.moveTo(x, y, duration=0.2)
    pyautogui.mouseDown()
    time.sleep(seconds)
    pyautogui.mouseUp()
    print(f"  [长按] ({rx:.3f},{ry:.3f}) 屏幕({x},{y}) {seconds}s")
    time.sleep(0.5)


def _screenshot(hwnd: int) -> Optional[Image.Image]:
    img = _grab_via_printwindow(hwnd)
    if img is None:
        # 回退：全屏截图裁窗口
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        img = pyautogui.screenshot(region=(l, t, r-l, b-t))
    return img


def find_and_click(hwnd: int, target: str, long_press: bool = False, cfg: dict = None):
    """截图 → AI 找位置 → 点击（或长按）。"""
    cfg = cfg or load_config()
    img = _screenshot(hwnd)
    if img is None:
        print(f"  [错误] 截图失败，无法定位 {target}")
        return False
    _debug_shot(img, f"before_{target[:6]}")
    pos = vision_locate(img, target)
    if pos is None:
        print(f"  [错误] AI 没找到【{target}】")
        return False
    rx, ry = pos
    print(f"  [定位] 【{target}】在 ({rx:.3f}, {ry:.3f})")
    if long_press:
        _long_press_relative(rx, ry, hwnd, cfg.get("camera_long_press_seconds", 1.0))
    else:
        _click_relative(rx, ry, hwnd, delay=cfg["step_delay_seconds"])
    time.sleep(cfg["window_wait_seconds"])
    return True


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------

def publish_moment(text: str, images: Optional[list[str]] = None) -> bool:
    cfg = load_config()

    hwnd = ensure_wechat_visible()
    if not hwnd:
        print("  [错误] 找不到微信主窗口")
        return False
    activate_window(hwnd)
    time.sleep(1.0)

    # 1. 点最左侧"发现"图标
    print("  [1/5] 找发现图标")
    if not find_and_click(hwnd, "微信窗口最左侧窄边栏的'发现'图标（绿色圆形图标）", cfg=cfg):
        return False

    # 2. 点"朋友圈"菜单
    print("  [2/5] 找朋友圈菜单")
    if not find_and_click(hwnd, "发现页中'朋友圈'菜单项（带彩色朋友圈图标和'朋友圈'文字）", cfg=cfg):
        return False

    # 3. 长按相机
    print("  [3/5] 找相机图标（长按）")
    if not find_and_click(hwnd, "朋友圈页面右上角的相机图标（从右数第二个，相机形状）",
                          long_press=True, cfg=cfg):
        return False

    # 4. 点输入框 + 粘贴
    print("  [4/5] 找输入框")
    if not find_and_click(hwnd, "纯文字朋友圈编辑弹窗中的文字输入区域（占位文字'这一刻的想法...'）", cfg=cfg):
        return False
    if text:
        pyperclip.copy(text)
        time.sleep(0.3)
        import pywinauto.keyboard as kb
        kb.send_keys("^v")
        time.sleep(0.5)
        print(f"  [文案] 已填入 {len(text)} 字")

    # 5. 点发表
    print("  [5/5] 找发表按钮")
    if not find_and_click(hwnd, "朋友圈编辑弹窗底部的'发表'按钮", cfg=cfg):
        return False

    print("  [完成] 已点击发表")
    return True


# ---------------------------------------------------------------------------
# 队列
# ---------------------------------------------------------------------------

def load_queue() -> list[dict]:
    if QUEUE_PATH.exists():
        try:
            return json.loads(QUEUE_PATH.read_text(encoding="utf-8")).get("items", [])
        except Exception:
            return []
    return []


def save_queue(items: list[dict]):
    QUEUE_PATH.write_text(
        json.dumps({"items": items}, ensure_ascii=False, indent=2), encoding="utf-8")


def run_queue_mode():
    cfg = load_config()
    while True:
        items = load_queue()
        pending = [it for it in items if it.get("status") != "done"]
        if not pending:
            print("  [队列] 没有待发的朋友圈")
            return
        for item in pending:
            ok = publish_moment(item.get("text", ""))
            item["status"] = "done" if ok else "failed"
            item["posted_at = time.strftime('%Y-%m-%d %H:%M:%S')"]
            save_queue(items)
            if ok:
                time.sleep(cfg["post_interval_seconds"])


def main():
    _setup_console_utf8()
    parser = argparse.ArgumentParser()
    parser.add_argument("--text", default="")
    parser.add_argument("--queue", action="store_true")
    args = parser.parse_args()

    if args.queue:
        run_queue_mode()
    elif args.text:
        ok = publish_moment(args.text)
        sys.exit(0 if ok else 1)
    else:
        parser.error("需要 --text 或 --queue")


if __name__ == "__main__":
    main()
