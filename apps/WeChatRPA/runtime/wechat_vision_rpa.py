#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
微信视觉 RPA — 无坐标模式
============================
基于 win32gui 动态窗口定位 + pywinauto 键鼠操作 + 视觉AI聊天区分析，
彻底告别坐标录制，跨分辨率即开即用。

核心流程：
  查找微信窗口 → 动态获取窗口矩形 → 裁剪右侧聊天区截图 → 视觉AI分析 → 自动发送回复

用法：
  python wechat_vision_rpa.py                      # 监控模式：全部联系人跑完后等待15秒
  python wechat_vision_rpa.py --once 联系人名称    # 单次模式：截图→AI→发送后退出
  python wechat_vision_rpa.py --test-vision 联系人名 # 仅测试AI分析，不发送
  python wechat_vision_rpa.py --scan               # 扫描微信窗口信息
  python wechat_vision_rpa.py -i 15                # 自定义全部联系人跑完后的等待秒数
  按 ESC 停止监控
"""
from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import re
import sys
import threading
import time
import urllib.request
import uuid
from datetime import datetime
from pathlib import Path
from typing import Optional

# ============================================================
# 运行时路径设置
# ============================================================
_FROZEN = getattr(sys, 'frozen', False)
if _FROZEN:
    SCRIPT_DIR = Path(sys.executable).resolve().parent
else:
    SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

SCREENSHOT_DIR = SCRIPT_DIR / "screenshots"
# 聊天正式截图（每轮调 AI 前保存的画面）
CHAT_SHOT_DIR = SCREENSHOT_DIR / "chat"
# 调试/对比截图（baseline/changed/nochange 等前后帧对比图）
DEBUG_SCREENSHOT_DIR = SCREENSHOT_DIR / "compare"
CONVERSATION_MEMORY_DIR = SCRIPT_DIR / "conversation_memory"
# 达成事件本地流水（JSONL，一天一个文件）+ 待补传队列
EVENT_LOG_DIR = SCRIPT_DIR / "events"

# 配置文件路径（runtime/ 目录下的文件）
USER_CONFIG_PATH = str(SCRIPT_DIR / "user_config.json")
# 通知转发配置独立成文件：主程序启动 RPA 时会重写 user_config.json，
# 放在里面会被覆盖，所以单独存。
RELAY_CONFIG_PATH = SCRIPT_DIR / "relay_config.json"
# 变化判定阈值同理独立成文件：主程序启动 RPA 时会用只含 lobster 段的内容重写
# user_config.json，写在那里的 change_detect 段会被清掉。
CHANGE_DETECT_CONFIG_PATH = SCRIPT_DIR / "change_detect_config.json"
CONTACTS_CONFIG_PATH = SCRIPT_DIR / "my_contacts.json"
STRATEGY_CONFIG_PATH = SCRIPT_DIR / "chat_strategy_config.json"

# 全局
STOP_FLAG = False
FATAL_EXIT_REASON = ""
_sent_history_by_contact: dict[str, set[str]] = {}
_STRATEGY_INSTANCE = None
_LAST_AI_RESULT: dict = {}
_EXTRACT_FAILURE_COUNT = 0
_EXTRACT_DISABLED_UNTIL = 0.0
_goal_achieved_contacts: set[str] = set()

DEFAULT_CROP_CONFIG = {
    "use_fixed_crop": True,
    "fixed_left": 335,
    "fixed_top": 110,
    "fixed_right_margin": 10,
    "fixed_bottom_margin": 60,
    "left_ratio": 0.26,
    "top_ratio": 0.09,
    "right_margin_ratio": 0.01,
    "bottom_ratio": 0.28,
    "min_left": 320,
    "min_top": 76,
    "max_left": 340,
    "max_top": 145,
    "min_right_margin": 8,
    "min_bottom": 230,
    "min_width": 320,
    "min_height": 220,
    # 自适应裁底线的合理波动范围：检测结果若偏离「固定边距线」超过 tolerance
    # 就丢弃回退固定边距（防深色主题 / 宽空白带误判导致过度裁切）
    "detect_bottom_tolerance": 80,
    # 裁底线最多离窗口底边多少像素（保证至少保留底部消息区，不裁进输入栏）
    "detect_bottom_min_keep": 40,
}

DEFAULT_DEBUG_SCREENSHOT_CONFIG = {
    "save_each_cycle": True,
    "keep_latest": True,
}

DEFAULT_CHANGE_CONFIRM_CONFIG = {
    "enabled": True,
    # 检测到变化后等多久再复查。实测（20261107_120542 的 9 次复查）：8 秒配置
    # 下每轮实际耗时 13 秒（复查还要 activate + search + scroll + 截图），其中
    # 51s 纯属白等、有效率 56%。3 秒足以过滤「打了一半停一下」的输入抖动
    # （微信「对方正在输入」在停止输入后约 10 秒才消失，但分段发送间隔 <2s），
    # 又能砍掉约 5 秒无效等待。
    "delay_seconds": 3,
}

# 连续 N 轮「本地像素=other / 视觉模型=me」冲突后，按视觉判定强制写入截图
# 基准终止复查，避免无限空转烧 API（真有新消息时摘要会再次变化并重新触发）。
CONFLICT_COMMIT_THRESHOLD = 2

DEFAULT_WECHAT_WINDOW_CONFIG = {
    "fix_size": True,
    "width": 1584,
    "height": 1102,
}

DEFAULT_CONVERSATION_MEMORY_CONFIG = {
    "enabled": True,
    "account_id": "default_wechat",
    "max_messages_per_contact": 200,
    "prompt_tail_messages": 40,
}

ACTIVE_ACCOUNT_ID = ""
ACTIVE_AGENT_NAME = ""
ACTIVE_SKILL_ID = ""

# ============================================================
# 日志自动保存
# ============================================================
_LOG_DIR = SCRIPT_DIR / "logs"


class _Tee:
    """同时输出到终端和日志文件"""
    def __init__(self, file):
        self.file = file

    def write(self, data):
        sys.__stdout__.write(data)
        self.file.write(data)
        self.file.flush()

    def flush(self):
        sys.__stdout__.flush()
        self.file.flush()


def _init_log_file():
    """每次启动创建一个日志文件，重定向 stdout"""
    _LOG_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    log_path = _LOG_DIR / f"log_{ts}.txt"
    f = open(log_path, "w", encoding="utf-8")
    sys.stdout = _Tee(f)
    print(f"  [日志] 已保存至 {log_path}")


# ============================================================
# 策略 / 联系人加载
# ============================================================
def load_contacts_info() -> dict:
    """读取 my_contacts.json，构建 name -> {gender, chat_goal} 映射。"""
    if not CONTACTS_CONFIG_PATH.exists():
        print(f"  [错误] 联系人配置文件不存在: {CONTACTS_CONFIG_PATH}")
        return {}
    try:
        import json as _json
        with open(CONTACTS_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = _json.load(f)
        contacts = {}
        group = data.get("group_name", "")
        default_goal = data.get("default_chat_goal", "")
        for c in data.get("contacts", []):
            name = (c.get("name") or "").strip()
            if not name:
                continue
            contacts[name] = {
                "name": name,
                "gender": c.get("gender", "未知"),
                "chat_goal": c.get("chat_goal") or default_goal or group,
            }
        print(f"  [配置] 已加载 {len(contacts)} 个联系人: {', '.join(contacts.keys())}")
        return contacts
    except Exception as e:
        print(f"  [错误] 加载联系人失败: {e}")
        return {}


def load_start_contact_name() -> str:
    """读取起始联系人备注名；未配置、文件缺失或解析失败时返回空串（从第一个开始）。"""
    try:
        if CONTACTS_CONFIG_PATH.exists():
            with open(CONTACTS_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            return str(data.get("start_contact") or "").strip()
    except Exception:
        pass
    return ""


def _get_strategy():
    """初始化并返回 ai_chat_strategy.ChatStrategy 实例。"""
    global _STRATEGY_INSTANCE
    if _STRATEGY_INSTANCE is not None:
        return _STRATEGY_INSTANCE
    from ai_chat_strategy import VisualChatStrategy
    _STRATEGY_INSTANCE = VisualChatStrategy(
        user_config_path=USER_CONFIG_PATH,
    )
    return _STRATEGY_INSTANCE


# ============================================================
# 辅助工具
# ============================================================
def _setup_console_utf8():
    """设置控制台编码为 UTF-8（Windows 需要）。"""
    for stream in (sys.stdout, sys.stderr, sys.__stdout__, sys.__stderr__):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if sys.platform == "win32":
        try:
            import ctypes
            kernel32 = ctypes.windll.kernel32
            kernel32.SetConsoleCP(65001)
            kernel32.SetConsoleOutputCP(65001)
        except Exception:
            pass


def _enable_dpi_awareness():
    """让进程感知系统 DPI/缩放比例。

    若不声明 DPI 感知，Windows 会按虚拟化坐标上报 GetWindowRect，
    而 PIL.ImageGrab 抓的是物理像素，二者在 1K/2K 高分屏 + 缩放（125%/150%）
    下不一致，导致裁剪窗口错位、右下角(最新消息)被裁进输入栏。

    优先使用「按显示器 DPI 感知」（多显示器各用各的缩放），失败时依次
    回退到系统 DPI 感知和旧版 API。在 100% 缩放显示器上是空操作，无副作用。
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # 按显示器感知
        except Exception:
            try:
                ctypes.windll.shcore.SetProcessDpiAwareness(1)  # 系统 DPI 感知
            except Exception:
                ctypes.windll.user32.SetProcessDPIAware()
    except Exception:
        pass


def image_to_data_uri(pil_image) -> str:
    """PIL Image → data URI (base64 JPEG, 体积比 PNG 小 5-10 倍，传输更快)。"""
    buf = io.BytesIO()
    # JPEG 不支持 alpha 通道，先转为 RGB
    if pil_image.mode in ("RGBA", "LA", "P"):
        pil_image = pil_image.convert("RGB")
    pil_image.save(buf, format="JPEG", quality=85)
    b64 = base64.b64encode(buf.getvalue()).decode("utf-8")
    return f"data:image/jpeg;base64,{b64}"


# 聊天区感知指纹：判定「画面是否真的来了新消息」的规则说明见 digest_change_info()。
# 历史标定：
#   · 20260911_1539：聚焦变暗/边缘闪烁噪声 ≤0.7%，整版位移型真消息 ≥20% → 占比阈值 2%；
#   · 20261107_1030：窗口高、会话短时微信把消息顶部对齐，新气泡只多出一小块，
#     整幅占比仅 0.16%~1.19% → 增加「局部连通变化块」第二段判定；
#   · 20261107_1411：「买房」「500w」两条真消息在旧量化桶宽（rel//32）下只产生
#     8 格变化，被 cluster_min_cells=12 判成噪声 → 连续 4 轮漏回。根因是对方气泡
#     为白底浅灰字，降采样后相对暗度 rel 仅 12~30，全部落进 0 档「隐形」。
#     桶宽改 8 后同一对帧给出 28 格连通块（可识别），而内容未变的相邻帧差异
#     仍为 0 格（背景归一化抵消聚焦明暗漂移，与桶宽无关）→ 无额外误报代价。
FINGERPRINT_CHANGE_THRESHOLD = 0.02

# 变化判定参数（可用 runtime/change_detect_config.json 覆盖，见 load_change_detect_config）
DEFAULT_CHANGE_DETECT_CONFIG = {
    "enabled": True,
    # 整幅差异占比超过它 → 直接判定变化
    "strong_fraction": FINGERPRINT_CHANGE_THRESHOLD,
    # 占比低于它 → 纯噪声，连簇都不用算
    "min_fraction": 0.00005,
    # 局部连通变化簇门槛（1 格 = 8x8px）。降到很低，对方发"你好"这种短消息
    # （约 2x2=4 格）也能被识别，不再被当成噪声吞掉。
    "cluster_min_cells": 3,
    "cluster_min_rows": 1,
    "cluster_min_cols": 2,
}

# 指纹里的 1 格 = 原图 8x8 像素（用于把变化簇坐标换算回像素，便于看日志）
FINGERPRINT_CELL_PX = 8

# 指纹格式："{cols}x{rows}:{hex}"。带上网格尺寸才能做「连通簇」判断；旧版只有
# 裸 hex（或 legacy: 开头的全图 MD5），解析失败时保守视为「有变化」，宁可多分析
# 一次也不能漏回。
_FINGERPRINT_HEADER_RE = re.compile(r"^(\d+)x(\d+)$")


def _parse_digest(digest: str):
    """把指纹字符串解析成 (cols, rows, cells)。无法解析（旧格式/异常）返回 None。"""
    if not isinstance(digest, str) or ":" not in digest:
        return None
    head, _, payload = digest.partition(":")
    matched = _FINGERPRINT_HEADER_RE.match(head)
    if not matched:
        return None
    try:
        cols = int(matched.group(1))
        rows = int(matched.group(2))
        cells = bytes.fromhex(payload)
    except (TypeError, ValueError):
        return None
    if cols <= 0 or rows <= 0 or len(cells) != cols * rows:
        return None
    return cols, rows, cells


def _largest_change_cluster(new_cells: bytes, old_cells: bytes, cols: int, rows: int):
    """找 8 邻域连通的最大变化簇，返回 (格数, 行0, 行1, 列0, 列1)。"""
    changed = [i for i, pair in enumerate(zip(new_cells, old_cells)) if pair[0] != pair[1]]
    if not changed:
        return 0, 0, 0, 0, 0
    changed_set = set(changed)
    visited: set[int] = set()
    best = (0, 0, 0, 0, 0)
    for start in changed:
        if start in visited:
            continue
        stack = [start]
        visited.add(start)
        size = 0
        r0 = r1 = start // cols
        c0 = c1 = start % cols
        while stack:
            idx = stack.pop()
            size += 1
            r, c = divmod(idx, cols)
            if r < r0:
                r0 = r
            elif r > r1:
                r1 = r
            if c < c0:
                c0 = c
            elif c > c1:
                c1 = c
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = r + dr, c + dc
                    if nr < 0 or nr >= rows or nc < 0 or nc >= cols:
                        continue
                    nb = nr * cols + nc
                    if nb in changed_set and nb not in visited:
                        visited.add(nb)
                        stack.append(nb)
        if size > best[0]:
            best = (size, r0, r1, c0, c1)
    return best


def screenshot_digest(pil_image) -> str:
    """计算抗抖聊天区感知指纹（"{cols}x{rows}:{hex}"，用于判断内容是否真的变了）。

    旧实现是全图 PNG 的逐像素 MD5：窗口聚焦/失焦会让整体底色从 250 级
    漂到 238 级、滚动条顶部几行零星闪烁，这些「内容没变」的抖动都会翻转
    MD5，导致每轮误报新消息、反复调 AI 空转（20260911_1539 日志实证）。

    新实现：灰度 → 裁掉顶部边缘带 + 右侧滚动条列 → 1/8 降采样取格均值 →
    以画面主背景亮度为基准计算「相对暗度」并量化到 8 级。乘性变暗（背景
    250→238）在相对暗度下完全抵消；右侧滚动条滑块随悬停/滚动上下移动，
    裁掉最右 20px 后不污染指纹（20260914_1051 实证：新消息在折叠线下时
    唯一变化的就是最右列滚动条，占比 0.43%）。新气泡/整版滚移仍会改变大量
    格子的相对暗度。比较必须用 screenshot_changed()（汉明占比），不能 ==。"""
    try:
        from collections import Counter
        from PIL import Image
        g = pil_image.convert("L")
        width, height = g.size
        top = min(16, max(0, height // 3))
        right = max(0, width - 20)  # 裁掉右侧滚动条列
        g = g.crop((0, top, right, height))
        crop_w = right
        small = g.resize((max(1, crop_w // 8), max(1, (height - top) // 8)), Image.BOX)
        data = small.tobytes()
        if not data:
            raise ValueError("empty fingerprint")
        bg = Counter(data).most_common(1)[0][0]
        denom = bg if bg >= 40 else 200  # 全黑废图兜底，避免除零/爆炸
        out = bytearray()
        for v in data:
            rel = abs(v - bg) * 255 // denom
            # 量化桶宽 8（旧值 32）。对方气泡是白底 + 浅灰文字，8x8 降采样后
            # rel 只有 12~30；旧桶宽 32 会把它们全部归到 0 档，白底气泡在指纹里
            # 直接「隐形」——20261107_141134 实测「买房」「500w」两条真消息只产生
            # 8 格差异（rel//32），被 cluster_min_cells=12 判成噪声，连续 4 轮漏回。
            # 改成 //8 后同一对帧产生 28 格连通块（可识别），而内容未变的相邻帧
            # 差异仍是 0 格（背景归一化已抵消聚焦明暗漂移，桶宽不影响该保护）。
            out.append(min(31, rel // 8))
        return f"{small.size[0]}x{small.size[1]}:" + bytes(out).hex()
    except Exception:
        # 任何异常回退旧的全图 MD5（比对时按逐位相等处理，行为退化为旧版）
        buf = io.BytesIO()
        pil_image.save(buf, format="PNG")
        return "legacy:" + hashlib.md5(buf.getvalue()).hexdigest()


def screenshot_change_fraction(new_digest: str, old_digest: str) -> float:
    """两张截图指纹的差异格子占比（0.0 = 完全一致）。无法比较时返回 1.0。"""
    return digest_change_info(new_digest, old_digest)["fraction"]


def digest_change_info(new_digest: str, old_digest: str) -> dict:
    """比较两枚指纹，返回判定细节（日志与阈值逻辑共用，避免重复计算）。

    返回 {"changed", "fraction", "cluster_cells", "cluster_box", "reason"}：
      reason: no_baseline / same / legacy / geometry_mismatch /
              strong（整版位移）/ cluster（局部新气泡）/ noise / below_min / disabled
    """
    info = {
        "changed": False,
        "fraction": 0.0,
        "cluster_cells": 0,
        "cluster_box": None,
        "reason": "no_baseline",
    }
    if not new_digest or not old_digest:
        # 首次建立基准：只有两边状态不同才算变化，交由调用方决定是否分析
        info["changed"] = bool(new_digest) != bool(old_digest)
        info["fraction"] = 0.0 if new_digest == old_digest else 1.0
        return info
    if new_digest == old_digest:
        info["reason"] = "same"
        return info
    parsed_new = _parse_digest(new_digest)
    parsed_old = _parse_digest(old_digest)
    if not parsed_new or not parsed_old:
        # 旧格式/异常指纹无法做感知比较，保守视为全变（宁可多分析一次，也不能漏回）
        info.update(changed=True, fraction=1.0, reason="legacy")
        return info
    cols, rows, new_cells = parsed_new
    old_cols, old_rows, old_cells = parsed_old
    if cols != old_cols or rows != old_rows:
        info.update(changed=True, fraction=1.0, reason="geometry_mismatch")
        return info

    diff_cells = sum(1 for a, b in zip(new_cells, old_cells) if a != b)
    fraction = diff_cells / len(new_cells)
    info["fraction"] = fraction

    cfg = load_change_detect_config()
    if not cfg["enabled"]:
        info["changed"] = fraction > cfg["strong_fraction"]
        info["reason"] = "strong" if info["changed"] else "disabled"
        return info
    if fraction > cfg["strong_fraction"]:
        info.update(changed=True, reason="strong")
        return info
    if fraction < cfg["min_fraction"]:
        info["reason"] = "below_min"
        return info

    size, r0, r1, c0, c1 = _largest_change_cluster(new_cells, old_cells, cols, rows)
    info["cluster_cells"] = size
    info["cluster_box"] = (
        r0 * FINGERPRINT_CELL_PX,
        r1 * FINGERPRINT_CELL_PX,
        c0 * FINGERPRINT_CELL_PX,
        c1 * FINGERPRINT_CELL_PX,
    )
    if (
        size >= cfg["cluster_min_cells"]
        and (r1 - r0 + 1) >= cfg["cluster_min_rows"]
        and (c1 - c0 + 1) >= cfg["cluster_min_cols"]
    ):
        info.update(changed=True, reason="cluster")
    else:
        info["reason"] = "noise"
    return info


def describe_change_info(info: dict) -> str:
    """把 digest_change_info() 的结果格式化成一行日志。"""
    fraction = info.get("fraction") or 0.0
    reason = info.get("reason") or ""
    parts = [f"画面变化度 {fraction * 100:.2f}%"]
    size = info.get("cluster_cells") or 0
    box = info.get("cluster_box")
    if size:
        if box:
            parts.append(
                f"最大变化块 {size} 格 (y≈{box[0]}-{box[1]}px, x≈{box[2]}-{box[3]}px)"
            )
        else:
            parts.append(f"最大变化块 {size} 格")
    labels = {
        "strong": "整版位移 → 有新消息",
        "cluster": "局部新增 → 判定为新消息",
        "noise": "零星变化 → 视为噪声",
        "below_min": "占比过低 → 视为噪声",
        "same": "无变化",
        "no_baseline": "无基准",
        "legacy": "旧格式指纹 → 视为变化",
        "geometry_mismatch": "网格尺寸变化（窗口/裁剪范围变了）→ 视为变化",
        "disabled": "变化检测已关闭（仅整幅占比）",
    }
    parts.append(labels.get(reason, reason))
    return " | ".join(parts)


def screenshot_changed(new_digest: str, old_digest: str) -> bool:
    """True = 两张截图的聊天内容有真实变化（整版位移或局部新气泡）。"""
    return digest_change_info(new_digest, old_digest)["changed"]


def detect_last_message_sender_locally(pil_image) -> tuple[str, dict]:
    """
    Heuristic local check for the bottom-most visible bubble/text side.
    Only the bottom band is considered — older messages higher up in the
    column must not skew the median. 短会话（内容贴近顶部、底部为空）时自动
    回退到全图扫描，避免把顶部唯一一条消息漏判。返回 ("me"|"other"|"unknown", debug_info)。
    """
    try:
        im = pil_image.convert("RGB")
        width, height = im.size
        pix = im.load()

        # debug 088a05 实证：深色主题/窗口变暗时，dark_text 规则会命中气泡底色
        # 本身（整幅下半部连成 359px 大带、green=0），像素检测完全不可靠，
        # 必须返回 unknown，让 AI 判定独立生效。
        step = 3
        light_cnt = dark_cnt = 0
        for y in range(0, height, step):
            for x in range(0, width, step):
                r, g, b = pix[x, y]
                if r > 200 and g > 200 and b > 200:
                    light_cnt += 1
                elif max(r, g, b) < 90:
                    dark_cnt += 1
        if dark_cnt > light_cnt * 2:
            return "unknown", {"reason": "dark_theme", "light": light_cnt, "dark": dark_cnt}

        left_margin = 35
        right_margin = 35

        def _build_rows(start_y: int):
            rows: list[int] = []
            greens_rows: list[int] = []
            xs_rows: list[list[int]] = []
            for y in range(start_y, height):
                xs: list[int] = []
                greens = 0
                for x in range(left_margin, max(left_margin, width - right_margin)):
                    r, g, b = pix[x, y]
                    green = g > 165 and r < 190 and b < 190 and (g - max(r, b)) > 25
                    dark_text = r < 110 and g < 110 and b < 110
                    if green or dark_text:
                        xs.append(x)
                        if green:
                            greens += 1
                rows.append(len(xs))
                greens_rows.append(greens)
                xs_rows.append(xs)
            return rows, greens_rows, xs_rows

        start_y = max(0, int(height * 0.55))
        row_counts, green_counts, xs_by_row = _build_rows(start_y)

        # 噪声防护（debug 088a05 实证）：聊天区左缘存在 3px 宽绿色竖条残影
        # （y=760-786, x=151-153），旧阈值 >=3 会把它当成「对方最后一条消息」，
        # 经 handoff 守卫误触发人工接管。真实气泡每行 >=10 命中且横向跨度大；
        # 细条带直接跳过并继续向上搜索真正的最底部气泡。
        row_min = 8
        band_min_span = 15
        def _locate(rows, greens_rows, xs_rows, start_y):
            y = height - 1
            while y >= start_y:
                if rows[y - start_y] < row_min:
                    y -= 1
                    continue
                bottom = y
                top = y
                gap = 0
                while y >= start_y:
                    if rows[y - start_y] >= row_min:
                        top = y
                        gap = 0
                    else:
                        gap += 1
                        if gap >= 18:
                            break
                    y -= 1
                xs: list[int] = []
                greens = 0
                for yy in range(top, bottom + 1):
                    xs.extend(xs_rows[yy - start_y])
                    greens += greens_rows[yy - start_y]
                if xs and (max(xs) - min(xs)) >= band_min_span:
                    return (top, bottom), (xs, greens)
                # 横向跨度不足 = 细竖条残影，跳过继续向上找
                y = top - 1
            return None, None

        band = _locate(row_counts, green_counts, xs_by_row, start_y)
        if band[0] is None:
            # 底部带没有消息（单条消息贴顶的短会话）→ 回退全图扫描
            start_y = 0
            row_counts, green_counts, xs_by_row = _build_rows(start_y)
            band = _locate(row_counts, green_counts, xs_by_row, start_y)
            if band[0] is None:
                return "unknown", {"reason": "no_candidate"}
            band_info = "fallback_full"
        else:
            band_info = "bottom_band"

        (top, bottom), (xs, greens) = band
        if not xs:
            return "unknown", {"reason": "empty_band", "top": top, "bottom": bottom}

        # 合并转发/聊天记录卡片等宽版式横跨中线（内容既有左侧文本又有右侧/居中元素），
        # 无法可靠判定左右 → 返回 unknown，交由 AI 看图判断，避免误判成 me 导致漏回。
        _min_x = min(xs)
        _max_x = max(xs)
        if _min_x < width * 0.45 and _max_x > width * 0.55:
            return "unknown", {
                "reason": "cross_center_wide_card",
                "min_x": _min_x,
                "max_x": _max_x,
                "green_points": greens,
                "top": top,
                "bottom": bottom,
            }
        xs_sorted = sorted(xs)
        median_x = xs_sorted[len(xs_sorted) // 2]
        sender = "me" if median_x > width * 0.5 else "other"
        if greens > 80 and median_x > width * 0.45:
            sender = "me"
        elif greens < 20 and median_x < width * 0.55:
            sender = "other"

        return sender, {
            "top": top,
            "bottom": bottom,
            "points": len(xs),
            "green_points": greens,
            "median_x": median_x,
            "width": width,
            "height": height,
            "start_y": start_y,
            "band": band_info,
        }
    except Exception as e:
        return "unknown", {"reason": str(e)}


def change_cluster_side(box, image_width: int) -> str:
    """根据本轮最大变化块的 x 区间判断新增内容落在哪侧：左侧=对方 other，右侧=我方 me，
    居中或缺失=unknown。变化块是本轮真正新增的像素位置，比全图气泡扫描更能代表
    「这条新消息」来自哪一侧（贴顶短会话里全图扫描会被上方历史气泡带偏）。"""
    if not box or image_width <= 0:
        return "unknown"
    x0, x1 = box[2], box[3]
    cx = (x0 + x1) / 2
    if cx < image_width * 0.45:
        return "other"
    if cx > image_width * 0.55:
        return "me"
    return "unknown"


def _number_config(data: dict, key: str, default, min_value=None, max_value=None):
    """Read a numeric config value with bounds; invalid values fall back."""
    value = data.get(key, default)
    try:
        if isinstance(default, int):
            value = int(value)
        else:
            value = float(value)
    except (TypeError, ValueError):
        return default
    if min_value is not None and value < min_value:
        return default
    if max_value is not None and value > max_value:
        return default
    return value


def load_crop_config() -> dict:
    """加载截图裁剪配置。默认使用固定边距裁剪，可配置回退比例裁剪。"""
    cfg = dict(DEFAULT_CROP_CONFIG)
    try:
        with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return cfg

    raw = data.get("crop") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return cfg

    cfg["use_fixed_crop"] = bool(raw.get("use_fixed_crop", cfg["use_fixed_crop"]))
    cfg["fixed_left"] = _number_config(raw, "fixed_left", cfg["fixed_left"], 0)
    cfg["fixed_top"] = _number_config(raw, "fixed_top", cfg["fixed_top"], 0)
    cfg["fixed_right_margin"] = _number_config(raw, "fixed_right_margin", cfg["fixed_right_margin"], 0)
    cfg["fixed_bottom_margin"] = _number_config(raw, "fixed_bottom_margin", cfg["fixed_bottom_margin"], 0)
    cfg["left_ratio"] = _number_config(raw, "left_ratio", cfg["left_ratio"], 0.0, 0.8)
    cfg["top_ratio"] = _number_config(raw, "top_ratio", cfg["top_ratio"], 0.0, 0.5)
    cfg["right_margin_ratio"] = _number_config(raw, "right_margin_ratio", cfg["right_margin_ratio"], 0.0, 0.2)
    cfg["bottom_ratio"] = _number_config(raw, "bottom_ratio", cfg["bottom_ratio"], 0.0, 0.7)
    cfg["min_left"] = _number_config(raw, "min_left", cfg["min_left"], 0)
    cfg["min_top"] = _number_config(raw, "min_top", cfg["min_top"], 0)
    cfg["max_left"] = _number_config(raw, "max_left", cfg["max_left"], 0)
    cfg["max_top"] = _number_config(raw, "max_top", cfg["max_top"], 0)
    cfg["min_right_margin"] = _number_config(raw, "min_right_margin", cfg["min_right_margin"], 0)
    cfg["min_bottom"] = _number_config(raw, "min_bottom", cfg["min_bottom"], 0)
    cfg["min_width"] = _number_config(raw, "min_width", cfg["min_width"], 100)
    cfg["min_height"] = _number_config(raw, "min_height", cfg["min_height"], 100)
    cfg["detect_bottom_tolerance"] = _number_config(raw, "detect_bottom_tolerance", cfg["detect_bottom_tolerance"], 0, 1000)
    cfg["detect_bottom_min_keep"] = _number_config(raw, "detect_bottom_min_keep", cfg["detect_bottom_min_keep"], 0, 3000)
    return cfg


def load_debug_screenshot_config() -> dict:
    """加载调试截图配置。默认每轮保存裁剪后的聊天区截图。"""
    cfg = dict(DEFAULT_DEBUG_SCREENSHOT_CONFIG)
    try:
        with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return cfg

    raw = data.get("debug_screenshots") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return cfg

    if "save_each_cycle" in raw:
        cfg["save_each_cycle"] = bool(raw.get("save_each_cycle"))
    if "keep_latest" in raw:
        cfg["keep_latest"] = bool(raw.get("keep_latest"))
    return cfg


def load_wechat_window_config() -> dict:
    """加载微信窗口固定尺寸配置。"""
    cfg = dict(DEFAULT_WECHAT_WINDOW_CONFIG)
    try:
        with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return cfg

    raw = data.get("wechat_window") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return cfg

    if "fix_size" in raw:
        cfg["fix_size"] = bool(raw.get("fix_size"))
    cfg["width"] = _number_config(raw, "width", cfg["width"], 800, 5000)
    cfg["height"] = _number_config(raw, "height", cfg["height"], 600, 3000)
    return cfg


def load_change_confirm_config() -> dict:
    """加载变化确认配置。检测到变化后，等待一小段时间再复查稳定截图。"""
    cfg = dict(DEFAULT_CHANGE_CONFIRM_CONFIG)
    try:
        with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return cfg

    raw = data.get("change_confirm") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return cfg

    if "enabled" in raw:
        cfg["enabled"] = bool(raw.get("enabled"))
    cfg["delay_seconds"] = _number_config(raw, "delay_seconds", cfg["delay_seconds"], 0, 180)
    return cfg


def _apply_change_detect_overrides(cfg: dict, raw) -> dict:
    """把一段 JSON 里的 change_detect 字段合并进 cfg（缺省字段保持默认）。"""
    if not isinstance(raw, dict):
        return cfg
    if "enabled" in raw:
        cfg["enabled"] = bool(raw.get("enabled"))
    cfg["strong_fraction"] = _number_config(
        raw, "strong_fraction", cfg["strong_fraction"], 0.0001, 1.0
    )
    cfg["min_fraction"] = _number_config(
        raw, "min_fraction", cfg["min_fraction"], 0.0, cfg["strong_fraction"]
    )
    cfg["cluster_min_cells"] = int(
        _number_config(raw, "cluster_min_cells", cfg["cluster_min_cells"], 1, 2000)
    )
    cfg["cluster_min_rows"] = int(
        _number_config(raw, "cluster_min_rows", cfg["cluster_min_rows"], 1, 200)
    )
    cfg["cluster_min_cols"] = int(
        _number_config(raw, "cluster_min_cols", cfg["cluster_min_cols"], 1, 500)
    )
    return cfg


def load_change_detect_config() -> dict:
    """加载「是否出现新消息」的变化判定参数。

    默认值针对实测数据标定（见 DEFAULT_CHANGE_DETECT_CONFIG 注释）。不同分辨率/
    DPI/主题的噪声水平不一样，若日志里出现误判（把噪声当新消息，或真消息仍被
    忽略），改 runtime/change_detect_config.json 即可（格式：{"cluster_min_cells": 8}）。

    优先读独立文件：LobsterAI 主程序每次从界面启动 RPA 都会用只含 lobster 段的
    内容重写 user_config.json（见 src/main/skills/wechatRpaController.ts），放在
    user_config.json 里会被覆盖掉，所以独立的 change_detect_config.json 更持久。
    没有独立文件时再兼容读 user_config.json 的 change_detect 段。
    """
    cfg = dict(DEFAULT_CHANGE_DETECT_CONFIG)

    for path in (CHANGE_DETECT_CONFIG_PATH, Path(USER_CONFIG_PATH)):
        try:
            with open(path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except Exception:
            continue
        if not isinstance(data, dict):
            continue
        # 独立文件允许直接写扁平字段，也允许包一层 change_detect
        raw = data.get("change_detect") if "change_detect" in data else data
        if isinstance(raw, dict) and raw:
            cfg = _apply_change_detect_overrides(cfg, raw)
            break
    return cfg


def load_conversation_memory_config() -> dict:
    """加载按联系人落盘的文字对话记忆配置。"""
    cfg = dict(DEFAULT_CONVERSATION_MEMORY_CONFIG)
    try:
        with open(USER_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return cfg

    raw = data.get("conversation_memory") if isinstance(data, dict) else None
    if not isinstance(raw, dict):
        return cfg

    if "enabled" in raw:
        cfg["enabled"] = bool(raw.get("enabled"))
    account_id = str(raw.get("account_id") or cfg["account_id"]).strip()
    cfg["account_id"] = account_id or "default_wechat"
    cfg["max_messages_per_contact"] = _number_config(
        raw,
        "max_messages_per_contact",
        cfg["max_messages_per_contact"],
        20,
        2000,
    )
    cfg["prompt_tail_messages"] = _number_config(
        raw,
        "prompt_tail_messages",
        cfg["prompt_tail_messages"],
        10,
        200,
    )
    return cfg


def _get_proactive_config() -> dict:
    """读取主动发送配置（chat_strategy_config.json）。

    - proactive_enabled: 是否允许主动发消息
    - proactive_interval_seconds: 首次沉默多久后主动破冰（默认 1800s = 30min）
    - proactive_second_interval_seconds: 第二次主动间隔（默认 3600s = 1h）
    - proactive_max_attempts: 最多主动几次后转纯被动（默认 2；0 = 不限次）

    阶梯节奏：沉默满首次间隔 → 主动破冰（第1次）→ 再隔第二次间隔 → 主动追问（第2次）
    → 仍不回则转被动，不再主动。对方一旦回复，主动次数清零重新计数。
    """
    cfg = {
        "proactive_enabled": False,
        "proactive_interval_seconds": 1800,
        "proactive_second_interval_seconds": 3600,
        "proactive_max_attempts": 2,
    }
    if not STRATEGY_CONFIG_PATH.exists():
        return cfg
    try:
        with open(STRATEGY_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            if "proactive_enabled" in data:
                cfg["proactive_enabled"] = bool(data["proactive_enabled"])
            if "proactive_interval_seconds" in data:
                cfg["proactive_interval_seconds"] = int(data["proactive_interval_seconds"])
            if "proactive_second_interval_seconds" in data:
                cfg["proactive_second_interval_seconds"] = int(data["proactive_second_interval_seconds"])
            if "proactive_max_attempts" in data:
                cfg["proactive_max_attempts"] = int(data["proactive_max_attempts"])
    except Exception:
        pass
    return cfg


def _get_reply_delay_config() -> dict:
    """Read min_reply_delay_seconds from chat_strategy_config.json.

    策略要求「不秒回」的最低等待时长：检测到对方新消息后先等满该秒数，再
    调 AI 生成回复，模拟真人节奏。默认 0（关闭，检测到就回复）；设成 N 秒即
    「至少等 N 秒才回」。策略文本里写「N 分钟后才回」也不影响本参数——程序按
    这里的秒数执行节奏。
    """
    cfg = {"min_reply_delay_seconds": 0}
    if not STRATEGY_CONFIG_PATH.exists():
        return cfg
    try:
        with open(STRATEGY_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict) and "min_reply_delay_seconds" in data:
            cfg["min_reply_delay_seconds"] = _number_config(
                data, "min_reply_delay_seconds", 0, 0, 3600
            )
    except Exception:
        pass
    return cfg


def _get_max_reply_messages() -> int:
    """Read max_reply_messages from chat_strategy_config.json.

    每次回复拆分发送的消息条数上限，由技能配置弹窗的「高级功能设置」写入。
    默认 5（与历史写死行为一致），范围 1~10。
    """
    if not STRATEGY_CONFIG_PATH.exists():
        return 5
    try:
        with open(STRATEGY_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            return _number_config(data, "max_reply_messages", 5, 1, 10)
    except Exception:
        pass
    return 5


def _get_send_config() -> dict:
    """读取发送节奏配置（chat_strategy_config.json），缺省用内置默认值。

    - send_message_interval: 多条回复逐条发出的间隔秒数（默认 1）
    - poll_interval: 全部联系人跑完后的轮询等待秒数（默认 15）
    - min_reply_delay_seconds: 检测到新消息后先等几秒再回复（默认 0）
    """
    cfg = {
        "send_message_interval": 1.0,
        "poll_interval": 15,
        "min_reply_delay_seconds": 0,
    }
    if not STRATEGY_CONFIG_PATH.exists():
        return cfg
    try:
        with open(STRATEGY_CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        if isinstance(data, dict):
            cfg["send_message_interval"] = float(_number_config(data, "send_message_interval", 1.0, 0.1, 10))
            cfg["poll_interval"] = int(_number_config(data, "poll_interval", 15, 1, 300))
            cfg["min_reply_delay_seconds"] = int(_number_config(data, "min_reply_delay_seconds", 0, 0, 300))
    except Exception:
        pass
    return cfg


def safe_filename_part(value: str) -> str:
    """Windows-safe filename fragment."""
    import re
    text = (value or "").strip() or "unknown"
    return re.sub(r'[\\/:*?"<>|\s]+', "_", text)[:40]


def save_debug_screenshot(shot, contact_name: str, cycle: int, reason: str) -> Optional[Path]:
    """每轮保存裁剪后的聊天区截图，用于检查 AI 实际看到的图片。"""
    cfg = load_debug_screenshot_config()
    if not cfg.get("save_each_cycle", True) and not cfg.get("keep_latest", True):
        return None

    DEBUG_SCREENSHOT_DIR.mkdir(parents=True, exist_ok=True)
    contact = safe_filename_part(contact_name)
    reason_part = safe_filename_part(reason)
    saved_path = None

    if cfg.get("save_each_cycle", True):
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        saved_path = DEBUG_SCREENSHOT_DIR / f"wechat_chat_cycle{cycle:04d}_{contact}_{reason_part}_{ts}.png"
        shot.save(saved_path)

    if cfg.get("keep_latest", True):
        latest_path = DEBUG_SCREENSHOT_DIR / f"latest_{contact}.png"
        shot.save(latest_path)
        saved_path = saved_path or latest_path

    if saved_path:
        print(f"  [调试截图] 已保存 {saved_path}")
    return saved_path


def current_account_id() -> str:
    """Return the active WeChat account namespace for conversation memory."""
    if ACTIVE_ACCOUNT_ID.strip():
        return ACTIVE_ACCOUNT_ID.strip()
    cfg = load_conversation_memory_config()
    return str(cfg.get("account_id") or "default_wechat").strip() or "default_wechat"


_AGENT_NAME_CACHE = ""


def current_agent_name() -> str:
    """当前员工（技能）名：命令行参数 > my_contacts.json 的 skill_name > 未分配。

    记忆目录与事件流水都按这个名字隔离，保证同一客户在不同员工下各记各的。
    """
    global _AGENT_NAME_CACHE
    if ACTIVE_AGENT_NAME.strip():
        return ACTIVE_AGENT_NAME.strip()
    if _AGENT_NAME_CACHE:
        return _AGENT_NAME_CACHE
    name = ""
    try:
        if CONTACTS_CONFIG_PATH.exists():
            with open(CONTACTS_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            name = str(data.get("skill_name") or "").strip()
    except Exception:
        name = ""
    _AGENT_NAME_CACHE = name or "未分配"
    return _AGENT_NAME_CACHE


_SKILL_ID_CACHE = ""


def current_skill_id() -> str:
    """当前员工的服务器技能 id：命令行参数 > my_contacts.json 的 skill_id。

    与 current_agent_name() 配套：手机端按 id 优先匹配技能，名字只作兜底；
    读不到就返回空串，归属匹配退回名字口径。
    """
    global _SKILL_ID_CACHE
    if ACTIVE_SKILL_ID.strip():
        return ACTIVE_SKILL_ID.strip()
    if _SKILL_ID_CACHE:
        return _SKILL_ID_CACHE
    sid = ""
    try:
        if CONTACTS_CONFIG_PATH.exists():
            with open(CONTACTS_CONFIG_PATH, "r", encoding="utf-8") as f:
                data = json.load(f)
            sid = str(data.get("skill_id") or "").strip()
    except Exception:
        sid = ""
    _SKILL_ID_CACHE = sid
    return _SKILL_ID_CACHE


def contact_memory_path(contact_name: str, account_id: str = "") -> Path:
    """Return the per-contact memory file path: <agent>/<account>/<contact>.json."""
    agent = safe_filename_part(current_agent_name())
    account = safe_filename_part(account_id or current_account_id())
    return CONVERSATION_MEMORY_DIR / agent / account / f"{safe_filename_part(contact_name)}.json"


def _legacy_memory_path(contact_name: str, account_id: str = "") -> Path:
    """旧版路径 <account>/<contact>.json，仅用于兼容读取历史记忆。"""
    account = safe_filename_part(account_id or current_account_id())
    return CONVERSATION_MEMORY_DIR / account / f"{safe_filename_part(contact_name)}.json"


def load_contact_memory(contact_name: str) -> dict:
    """Load one contact's persisted text conversation memory."""
    path = contact_memory_path(contact_name)
    if not path.exists():
        legacy = _legacy_memory_path(contact_name)
        if legacy.exists():
            path = legacy
        else:
            return {
                "agent_name": current_agent_name(),
                "account_id": current_account_id(),
                "contact_name": contact_name,
                "created_at": datetime.now().isoformat(timespec="seconds"),
                "updated_at": "",
                "messages": [],
            }
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict):
            raise ValueError("memory root is not an object")
        data.setdefault("agent_name", current_agent_name())
        data.setdefault("account_id", current_account_id())
        data.setdefault("contact_name", contact_name)
        data.setdefault("messages", [])
        if not isinstance(data["messages"], list):
            data["messages"] = []
        return data
    except Exception as e:
        print(f"  [记忆] 读取 {contact_name} 历史失败，将新建: {e}")
        return {
            "agent_name": current_agent_name(),
            "account_id": current_account_id(),
            "contact_name": contact_name,
            "created_at": datetime.now().isoformat(timespec="seconds"),
            "updated_at": "",
            "messages": [],
        }


def save_contact_memory(contact_name: str, memory: dict) -> None:
    """Persist one contact's text conversation memory."""
    cfg = load_conversation_memory_config()
    messages = memory.get("messages") if isinstance(memory, dict) else []
    if not isinstance(messages, list):
        messages = []
    max_messages = int(cfg.get("max_messages_per_contact", 200))
    memory["messages"] = messages[-max_messages:]
    memory["agent_name"] = current_agent_name()
    memory["account_id"] = current_account_id()
    memory["contact_name"] = contact_name
    memory["updated_at"] = datetime.now().isoformat(timespec="seconds")

    path = contact_memory_path(contact_name)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(memory, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)
    # 记忆有更新 → 后台节流合并上传聊天日志；异常不影响主流程
    enqueue_chat_log_sync(contact_name, memory)


def normalize_message_content(value: str) -> str:
    """Normalize message content for stable de-duplication."""
    import re
    text = str(value or "").strip()
    text = re.sub(r"\s+", " ", text)
    return text


def message_fingerprint(msg: dict) -> str:
    """Stable fingerprint for visible messages, tolerant of repeated screenshots."""
    sender = str(msg.get("sender") or "unknown").strip().lower()
    side = str(msg.get("side") or "").strip().lower()
    ts = str(msg.get("timestamp") or "").strip()
    content = normalize_message_content(str(msg.get("content") or ""))
    raw = f"{sender}|{side}|{ts}|{content}"
    return hashlib.sha1(raw.encode("utf-8", errors="ignore")).hexdigest()


def sanitize_extracted_message(raw: dict) -> Optional[dict]:
    """Convert one model-extracted message into the local memory schema."""
    if not isinstance(raw, dict):
        return None
    sender = str(raw.get("sender") or "unknown").strip().lower()
    if sender in ("对方", "other", "left"):
        sender = "other"
    elif sender in ("我", "我方", "me", "right", "self"):
        sender = "me"
    else:
        sender = "unknown"

    side = str(raw.get("side") or raw.get("last_message_side") or "").strip().lower()
    if side not in ("left", "right"):
        side = "left" if sender == "other" else "right" if sender == "me" else "unknown"

    content = normalize_message_content(str(raw.get("content") or raw.get("text") or ""))
    if not content:
        return None

    content_type = str(raw.get("content_type") or "text").strip().lower()
    if content_type not in ("text", "image", "sticker", "link", "file", "mixed", "unknown"):
        content_type = "text"

    msg = {
        "sender": sender,
        "side": side,
        "content_type": content_type,
        "content": content,
        "timestamp": raw.get("timestamp") or None,
        "seen_at": datetime.now().isoformat(timespec="seconds"),
        "source": "vision_extract",
    }
    msg["id"] = message_fingerprint(msg)
    return msg


def merge_visible_messages(contact_name: str, extracted_messages: list[dict]) -> tuple[dict, list[dict]]:
    """Merge visible extracted messages into per-contact memory."""
    cfg = load_conversation_memory_config()
    memory = load_contact_memory(contact_name)
    if not cfg.get("enabled", True):
        return memory, []

    existing = memory.get("messages", [])
    if not isinstance(existing, list):
        existing = []
    seen_ids = {str(m.get("id") or message_fingerprint(m)) for m in existing if isinstance(m, dict)}
    new_messages: list[dict] = []

    for raw in extracted_messages or []:
        msg = sanitize_extracted_message(raw)
        if not msg:
            continue
        if msg["id"] in seen_ids:
            continue
        existing.append(msg)
        seen_ids.add(msg["id"])
        new_messages.append(msg)

    memory["messages"] = existing
    save_contact_memory(contact_name, memory)
    if new_messages:
        print(f"  [记忆] {contact_name} 新增 {len(new_messages)} 条可见消息")
    else:
        print(f"  [记忆] {contact_name} 未提取到新的聊天消息")
    return memory, new_messages


def append_sent_reply_to_memory(contact_name: str, reply: str) -> None:
    """Record the outbound reply so future turns can avoid repeating it."""
    cfg = load_conversation_memory_config()
    if not cfg.get("enabled", True):
        return
    memory = load_contact_memory(contact_name)
    msg = {
        "sender": "me",
        "side": "right",
        "content_type": "text",
        "content": normalize_message_content(reply),
        "timestamp": datetime.now().strftime("%H:%M"),
        "seen_at": datetime.now().isoformat(timespec="seconds"),
        "source": "sent_by_rpa",
    }
    msg["id"] = message_fingerprint(msg)
    messages = memory.get("messages", [])
    if not isinstance(messages, list):
        messages = []
    if msg["id"] not in {str(m.get("id") or "") for m in messages if isinstance(m, dict)}:
        messages.append(msg)
    memory["messages"] = messages
    save_contact_memory(contact_name, memory)


def recent_replies_from_memory(contact_name: str, limit: int = 5) -> list[str]:
    """Read recent me-sent replies from persisted memory."""
    memory = load_contact_memory(contact_name)
    replies = [
        str(m.get("content") or "").strip()
        for m in memory.get("messages", [])
        if isinstance(m, dict) and m.get("sender") == "me" and str(m.get("content") or "").strip()
    ]
    return replies[-limit:]


def prompt_history_from_memory(memory: dict) -> list[dict]:
    """Return the tail of memory that should be sent to the text model."""
    cfg = load_conversation_memory_config()
    tail = int(cfg.get("prompt_tail_messages", 40))
    messages = memory.get("messages", []) if isinstance(memory, dict) else []
    if not isinstance(messages, list):
        return []
    return messages[-tail:]


def confirm_changed_screenshot(hwnd, contact_name: str, cycle: int, first_digest: str):
    """检测到变化后延迟复查，返回稳定后的截图和摘要。"""
    cfg = load_change_confirm_config()
    delay = int(cfg.get("delay_seconds", DEFAULT_CHANGE_CONFIRM_CONFIG["delay_seconds"]))
    if not cfg.get("enabled", True) or delay <= 0:
        return None, first_digest

    print(f"  [确认] 检测到变化，等待 {delay} 秒后复查最新聊天区")
    for _ in range(delay):
        if STOP_FLAG:
            break
        time.sleep(1)

    activate_window(hwnd)
    time.sleep(0.2)
    search_contact(contact_name)
    time.sleep(1.0)
    shot = capture_chat_message_area(hwnd)
    if not shot:
        print("  [确认] 复查截图失败，沿用首次变化截图")
        return None, first_digest

    digest = screenshot_digest(shot)
    save_debug_screenshot(shot, contact_name, cycle, "confirmed")
    if screenshot_changed(digest, first_digest):
        print(f"  [确认] {delay}秒内聊天区继续变化，改用复查后的最新截图")
    else:
        print("  [确认] 聊天区已稳定")
    return shot, digest


# ============================================================
# 微信窗口管理
# ============================================================
def _find_wechat_hwnds():
    """查找所有微信主窗口句柄。"""
    import win32gui
    hwnds = []

    def enum_cb(hwnd, _):
        if not win32gui.IsWindowVisible(hwnd):
            return
        title = win32gui.GetWindowText(hwnd)
        # 微信主窗口标题：中文版为 "微信"/以"微信"结尾；新版 Weixin 4.0 为 "Weixin"/"WeChat"
        t = title.strip()
        if (
            t == "微信" or t.endswith("微信")
            or t == "Weixin" or t.endswith("Weixin")
            or t == "WeChat" or t.endswith("WeChat")
        ):
            hwnds.append(hwnd)
    win32gui.EnumWindows(enum_cb, None)
    return hwnds


def ensure_wechat_window_size(hwnd) -> bool:
    """按配置固定微信窗口大小，避免裁剪范围随手动缩放漂移。

    高分屏/小屏下会把目标尺寸裁剪到屏幕工作区以内，否则窗口底边
    （输入框 + 最新消息）会延伸到屏幕外被裁掉，导致最新单条消息看不到。
    """
    cfg = load_wechat_window_config()
    if not cfg.get("fix_size", True):
        return True

    target_width = int(cfg["width"])
    target_height = int(cfg["height"])
    try:
        import win32api
        import win32con
        import win32gui

        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)

        # 用窗口所在显示器的工作区（排除任务栏）约束目标尺寸，
        # 保证窗口完全落在屏幕内，底边不会被裁掉。
        try:
            monitor = win32api.MonitorFromWindow(hwnd, win32con.MONITOR_DEFAULTTONEAREST)
            mi = win32api.GetMonitorInfo(monitor)
            work = mi["Work"]
            work_width = work[2] - work[0]
            work_height = work[3] - work[1]
            if target_height > work_height:
                target_height = work_height - 4
                print(f"  [窗口] 目标高度超出工作区，已收缩到 {target_height}px")
            if target_width > work_width:
                target_width = work_width - 4
                print(f"  [窗口] 目标宽度超出工作区，已收缩到 {target_width}px")
        except Exception:
            pass

        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        cur_width = right - left
        cur_height = bottom - top
        if abs(cur_width - target_width) <= 2 and abs(cur_height - target_height) <= 2:
            return True

        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetWindowPos(
            hwnd,
            0,
            0,
            0,
            target_width,
            target_height,
            win32con.SWP_NOMOVE | win32con.SWP_NOZORDER,
        )
        time.sleep(0.2)
        print(f"  [窗口] 已固定微信窗口大小: {target_width}x{target_height}")
        return True
    except Exception as e:
        print(f"  [窗口] 固定微信窗口大小失败: {e}")
        return False


def ensure_wechat_visible():
    """确保微信窗口可见并返回句柄。有多个微信实例时让用户选择。"""
    import win32gui
    import win32con
    import win32process
    import psutil

    hwnds = _find_wechat_hwnds()
    if not hwnds:
        print("  [错误] 未找到微信窗口，请先打开微信")
        # 尝试启动微信
        wechat_paths = [
            os.path.expandvars(r"%ProgramFiles%\Tencent\WeChat\WeChat.exe"),
            os.path.expandvars(r"%ProgramFiles(x86)%\Tencent\WeChat\WeChat.exe"),
        ]
        for wp in wechat_paths:
            if os.path.exists(wp):
                os.startfile(wp)
                print("  [信息] 正在启动微信...")
                time.sleep(5)
                hwnds = _find_wechat_hwnds()
                break
        if not hwnds:
            return None

    if len(hwnds) == 1:
        hwnd = hwnds[0]
    else:
        print(f"\n  [选择] 发现 {len(hwnds)} 个微信窗口：")
        for i, h in enumerate(hwnds):
            title = win32gui.GetWindowText(h)
            _, pid = win32process.GetWindowThreadProcessId(h)
            try:
                proc = psutil.Process(pid)
                exe = proc.exe()
            except Exception:
                exe = "?"
            print(f"    {i+1}. {title} (PID:{pid}) {exe}")
        choice = input("  [选择] 请选择要控制的微信 (1-{}): ".format(len(hwnds))).strip()
        try:
            idx = int(choice) - 1
            hwnd = hwnds[idx]
        except (ValueError, IndexError):
            print("  [错误] 无效选择，默认使用第一个")
            hwnd = hwnds[0]

    # 激活窗口
    try:
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        ensure_wechat_window_size(hwnd)
        win32gui.SetForegroundWindow(hwnd)
        time.sleep(0.5)
    except Exception:
        pass
    return hwnd


def activate_window(hwnd):
    """将窗口带到前台，内置三策略容错（AttachThreadInput → BringWindowToTop → ShowWindow 直调）。"""
    import win32con
    import win32gui
    import win32process
    try:
        if win32gui.IsIconic(hwnd):
            win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        win32gui.SetForegroundWindow(hwnd)
        return True
    except Exception:
        pass
    try:
        # Windows 前台锁限制时，先提升调用线程输入级别再设置前台窗口。
        import win32api
        cur_thread = win32api.GetCurrentThreadId()
        fg_hwnd = win32gui.GetForegroundWindow()
        if fg_hwnd and fg_hwnd != hwnd:
            fg_thread, _ = win32process.GetWindowThreadProcessId(fg_hwnd)
            tgt_thread, _ = win32process.GetWindowThreadProcessId(hwnd)
            win32process.AttachThreadInput(cur_thread, fg_thread, True)
            win32process.AttachThreadInput(cur_thread, tgt_thread, True)
            win32gui.BringWindowToTop(hwnd)
            win32gui.SetForegroundWindow(hwnd)
            win32process.AttachThreadInput(cur_thread, tgt_thread, False)
            win32process.AttachThreadInput(cur_thread, fg_thread, False)
            return True
    except Exception:
        pass
    try:
        win32gui.BringWindowToTop(hwnd)
        win32gui.ShowWindow(hwnd, win32con.SW_RESTORE)
        return True
    except Exception:
        return False


def _grab_via_printwindow(hwnd):
    """用 PrintWindow 直接抓取窗口自身内容，避免被其它窗口遮挡/黑屏。

    返回 PIL Image；抓取失败或不可渲染时返回 None，由上层回退到屏幕抓取。
    """
    import ctypes
    import ctypes.wintypes

    import win32gui
    from PIL import Image

    user32 = ctypes.windll.user32
    gdi32 = ctypes.windll.gdi32

    left, top, right, bottom = win32gui.GetWindowRect(hwnd)
    width = max(1, right - left)
    height = max(1, bottom - top)

    hwnd_dc = user32.GetWindowDC(hwnd)
    if not hwnd_dc:
        return None
    mem_dc = gdi32.CreateCompatibleDC(hwnd_dc)
    bitmap = gdi32.CreateCompatibleBitmap(hwnd_dc, width, height)
    if not bitmap:
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
        return None
    gdi32.SelectObject(mem_dc, bitmap)

    PW_RENDERFULLCONTENT = 0x00000002
    ok = user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
    if not ok:
        ok = user32.PrintWindow(hwnd, mem_dc, 0)

    img = None
    try:
        if ok:
            stride = ((width * 3 + 3) // 4) * 4

            class BITMAPINFOHEADER(ctypes.Structure):
                _fields_ = [
                    ("biSize", ctypes.wintypes.DWORD),
                    ("biWidth", ctypes.wintypes.LONG),
                    ("biHeight", ctypes.wintypes.LONG),
                    ("biPlanes", ctypes.wintypes.WORD),
                    ("biBitCount", ctypes.wintypes.WORD),
                    ("biCompression", ctypes.wintypes.DWORD),
                    ("biSizeImage", ctypes.wintypes.DWORD),
                    ("biXPelsPerMeter", ctypes.wintypes.LONG),
                    ("biYPelsPerMeter", ctypes.wintypes.LONG),
                    ("biClrUsed", ctypes.wintypes.DWORD),
                    ("biClrImportant", ctypes.wintypes.DWORD),
                ]

            bmi = BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(BITMAPINFOHEADER)
            bmi.biWidth = width
            bmi.biHeight = -height  # 自上而下
            bmi.biPlanes = 1
            bmi.biBitCount = 24
            bmi.biCompression = 0  # BI_RGB
            bmi.biSizeImage = stride * height
            buf = ctypes.create_string_buffer(bmi.biSizeImage)
            lines = gdi32.GetDIBits(mem_dc, bitmap, 0, height, buf, ctypes.byref(bmi), 0)
            if lines == height:
                img = Image.frombytes("RGB", (width, height), buf.raw, "raw", "BGR", stride)
    finally:
        gdi32.DeleteObject(bitmap)
        gdi32.DeleteDC(mem_dc)
        user32.ReleaseDC(hwnd, hwnd_dc)
    return img


def _looks_like_blank_capture(img) -> bool:
    """粗略判定截图是否整幅接近纯黑（抓取失败/遮挡的典型特征）。"""
    try:
        gray = img.convert("L").resize((64, 48))
        pixels = list(gray.getdata())
        n = len(pixels)
        if n == 0:
            return True
        mean = sum(pixels) / n
        var = sum((p - mean) * (p - mean) for p in pixels) / n
        return mean < 60 and var < 60
    except Exception:
        return False


def _grab_screen_bbox(left, top, right, bottom):
    """按屏幕坐标抓一块区域。

    必须带 all_screens=True：Pillow 默认只抓「主屏」，坐标一旦是负数
    （微信被拖到左侧副屏），整块区域会返回纯黑（实测 mean=0），
    表现就是日志里「截图疑似全黑」但程序靠 PrintWindow 兜底照样能回复。
    """
    import PIL.ImageGrab
    try:
        return PIL.ImageGrab.grab(bbox=(left, top, right, bottom), all_screens=True)
    except TypeError:
        # 老版本 Pillow 没有 all_screens 参数
        return PIL.ImageGrab.grab(bbox=(left, top, right, bottom))


def _window_spans_virtual_screen(left, top, right, bottom) -> bool:
    """窗口是否越过主屏边界（副屏/负坐标），越界时必须用全屏抓取模式。"""
    import ctypes
    u = ctypes.windll.user32
    vx, vy = u.GetSystemMetrics(76), u.GetSystemMetrics(77)
    vw, vh = u.GetSystemMetrics(78), u.GetSystemMetrics(79)
    return (left < vx or top < vy or right > vx + vw or bottom > vy + vh)


def capture_full_window(hwnd):
    """全窗口截图，返回 PIL Image。

    主路径用屏幕抓取；抓到纯黑（被遮挡/前台失败/副屏坐标）时先唤醒重抓，
    仍失败才用 PrintWindow 兜底重抓一次。避免在微信刚启动/未就绪时直接调
    PrintWindow（微信为 Chromium 渲染，过早请求 DWM 合成帧可能引发崩溃）。
    """
    import win32gui
    try:
        # 确保窗口在前台
        activate_window(hwnd)
        ensure_wechat_window_size(hwnd)
        time.sleep(0.3)
        left, top, right, bottom = win32gui.GetWindowRect(hwnd)
        width = right - left
        height = bottom - top
        if width <= 0 or height <= 0:
            return None

        if left < 0 or top < 0:
            print(f"  [截图] 微信窗口在副屏 (x={left}, y={top})，按全屏模式抓取")
        shot = _grab_screen_bbox(left, top, right, bottom)
        if shot is None:
            return None

        # 抓到黑屏（前台切换失败/被遮挡）——先强制唤醒重抓，仍失败才走 PrintWindow
        if _looks_like_blank_capture(shot):
            print("  [截图] 截图疑似全黑，强制唤醒微信并重试...")
            activate_window(hwnd)
            time.sleep(0.6)
            from PIL import Image
            left, top, right, bottom = win32gui.GetWindowRect(hwnd)
            shot = _grab_screen_bbox(left, top, right, bottom)
            if shot is None:
                return None
            if _looks_like_blank_capture(shot):
                print("  [截图] 屏幕抓取仍全黑，尝试 PrintWindow 兜底...")
                shot = _grab_via_printwindow(hwnd) or Image.new("RGB", (width, height), "black")
        return shot
    except Exception as e:
        print(f"  [错误] 截图失败: {e}")
        return None


def detect_input_box_top(full_shot, default_bottom_margin=185, search_margin=260):
    """
    Locate the y (window row) just above WeChat's input box so the crop bottom
    never slices into the newest message.

    WeChat's message area has a light background; the input box below it is a
    differently-shaded band (toolbar). We sample a vertical strip in the
    left/center columns, read the dominant background color at the very bottom
    (= input box), then walk upward to the first row that clearly switches away
    from that color (= top edge of the input box). Falls back to
    `height - default_bottom_margin` when detection is inconclusive.
    """
    try:
        im = full_shot.convert("RGB")
        width, height = im.size
        pix = im.load()
        x0 = max(0, int(width * 0.25))
        x1 = min(width, int(width * 0.7))
        step = 3
        probe_rows = range(height - 1, max(0, height - 1 - search_margin), -1)

        def dominant_color(y):
            from collections import Counter
            counts = Counter()
            for x in range(x0, x1, step):
                counts[pix[x, y][0:3]] += 1
            if not counts:
                return None
            return counts.most_common(1)[0][0]

        # 1) Anchor on the input-box background color near the bottom.
        anchor = None
        sampled = 0
        for y in probe_rows:
            c = dominant_color(y)
            if c is not None:
                anchor = c
                sampled += 1
                if sampled >= 3:
                    break
        if anchor is None:
            return None

        # 2) Walk upward; the input-box top is where the color diverges.
        for y in probe_rows:
            c = dominant_color(y)
            if c is None:
                continue
            diff = max(abs(c[0] - anchor[0]), abs(c[1] - anchor[1]), abs(c[2] - anchor[2]))
            if diff > 12:
                return y
        return max(0, height - default_bottom_margin)
    except Exception:
        return None


def crop_chat_message_area(full_shot):
    """从微信整窗截图中裁出右侧消息区，排除左侧列表和底部输入框。"""
    width, height = full_shot.size
    cfg = load_crop_config()

    if cfg.get("use_fixed_crop", True):
        left = int(cfg["fixed_left"])
        top = int(cfg["fixed_top"])
        right = width - int(cfg["fixed_right_margin"])
        # 自适底边：尽量用像素探测输入框上沿，避免固定边距把最新消息裁掉。
        # 在 1K/高分屏、缩放下 GetWindowRect 与物理像素不一致时尤其重要。
        detected_bottom = detect_input_box_top(full_shot, cfg.get("fixed_bottom_margin", 185))
        fixed_bottom = height - int(cfg["fixed_bottom_margin"])
        if detected_bottom is not None and detected_bottom > top:
            # 安全边界：输入框上沿只会在「固定边距线」附近合理波动。
            # 检测结果一旦低于固定线（更靠近窗口底边），就等于把输入框里的
            # 未发送草稿/工具栏裁进了截图，AI 会把草稿当成最后一条消息。
            # 因此只允许在上方小范围波动（找回更多聊天区），绝不越过固定线。
            lower_bound = max(top, fixed_bottom - cfg.get("detect_bottom_tolerance", 80))
            upper_bound = min(fixed_bottom, height - cfg.get("detect_bottom_min_keep", 40))
            if lower_bound <= detected_bottom <= upper_bound:
                bottom = detected_bottom
                print(f"  [截图] 自适应裁底: 输入框上沿 y={detected_bottom}（固定线 y={fixed_bottom}）")
            else:
                bottom = fixed_bottom
                print(
                    f"  [截图] 自适应裁底检测越界（y={detected_bottom}），回退固定边距 y={fixed_bottom}"
                )
        else:
            bottom = fixed_bottom
        crop_mode = (
            f"fixed left={cfg['fixed_left']}, top={cfg['fixed_top']}, "
            f"right_margin={cfg['fixed_right_margin']}, bottom_margin={cfg['fixed_bottom_margin']}"
            + (f" (detected input top y={detected_bottom})" if detected_bottom is not None else "")
        )
    else:
        # 微信 PC 默认布局大致为：左窄导航 + 会话列表 + 右侧聊天区。
        # 左侧列表宽度通常接近固定值；宽窗口下不能按比例无限右移，否则会裁掉左侧头像/白色气泡。
        left = max(cfg["min_left"], int(width * cfg["left_ratio"]))
        top = max(cfg["min_top"], int(height * cfg["top_ratio"]))
        if cfg.get("max_left", 0) > 0:
            left = min(left, cfg["max_left"])
        if cfg.get("max_top", 0) > 0:
            top = min(top, cfg["max_top"])
        right = width - max(cfg["min_right_margin"], int(width * cfg["right_margin_ratio"]))
        bottom = height - max(cfg["min_bottom"], int(height * cfg["bottom_ratio"]))
        crop_mode = (
            f"ratio left={cfg['left_ratio']}, top={cfg['top_ratio']}, "
            f"max_left={cfg.get('max_left')}, max_top={cfg.get('max_top')}, bottom={cfg['bottom_ratio']}"
        )

    left = max(0, min(int(left), width))
    top = max(0, min(int(top), height))
    right = max(left, min(int(right), width))
    bottom = max(top, min(int(bottom), height))

    # 窗口过小或布局异常时，保守回退到整窗，避免截出空图。
    if right - left < cfg["min_width"] or bottom - top < cfg["min_height"]:
        print("  [截图] 聊天区裁剪过小，回退整窗截图")
        return full_shot

    print(
        f"  [截图] 已裁剪聊天消息区: ({left},{top})-({right},{bottom}) / {width}x{height} "
        f"[{crop_mode}]"
    )
    return full_shot.crop((left, top, right, bottom))


def capture_chat_message_area(hwnd):
    """截图右侧聊天消息区，避免左侧会话列表/底部输入框干扰视觉判断。

    截图前先滚到最新消息——微信可能停留在旧的滚动位置（出现「N 条新消息」
    悬浮按钮），新消息在可视区下方时聊天区像素完全不变，变化检测和视觉
    AI 都看不到新消息（20260914_1051 实证）。"""
    scroll_chat_to_bottom(hwnd)
    full_shot = capture_full_window(hwnd)
    if not full_shot:
        return None
    return crop_chat_message_area(full_shot)


def search_contact(name):
    """在微信中搜索联系人。"""
    import pywinauto.keyboard as kb
    import win32gui
    print(f"  [操作] 搜索联系人: {name}")
    try:
        # Ctrl+F 打开搜索
        kb.send_keys("^f")
        time.sleep(0.5)
        # 输入联系人名
        kb.send_keys(name)
        time.sleep(1)
        # Enter 进入聊天
        kb.send_keys("{ENTER}")
        time.sleep(0.5)
        print(f"  [完成] 已进入 {name} 的聊天")
        return True
    except Exception as e:
        print(f"  [错误] 搜索联系人失败: {e}")
        return False


def scroll_chat_to_bottom(hwnd):
    """把消息区滚动到最底部，保证最新一条消息在截图里可见。

    微信可能停留在旧的滚动位置（右侧出现「N 条新消息」悬浮按钮）。此时
    对方发来的新消息全在可视区下方，聊天区截图内容不变、只有滚动条滑块
    移动（20260914_1051 日志实证：c4->c5 变化全部集中在最右列 x=1216，
    占比 0.43%），变化检测与 AI 分析都看不到新消息，表现为「不回复」。
    """
    try:
        import pyautogui
        import win32gui
        crop = load_crop_config()
        left = int(crop.get("fixed_left", 340))
        top = int(crop.get("fixed_top", 145))
        right_margin = int(crop.get("fixed_right_margin", 15))
        bottom_margin = int(crop.get("fixed_bottom_margin", 160))
        l, t, r, b = win32gui.GetWindowRect(hwnd)
        cx = l + left + max(1, (r - l - left - right_margin)) // 2
        cy = t + top + max(1, (b - t - top - bottom_margin)) // 2
        pyautogui.moveTo(cx, cy, duration=0.15)
        pyautogui.scroll(-10)
        time.sleep(0.4)
        return True
    except Exception as e:
        print(f"  [滚动] 无法滚动到最新消息: {e}")
        return False


def trim_incomplete_reply(reply: str) -> str:
    """去掉被 max_tokens 截断的半句话，避免发出去「你这边要是还没定下来，我」这种残句。

    判据（保守，避免误伤只是没写句号的完整句）：
    最后一条不以句末标点收尾，且末字是「悬空字」（我/你/就/再/和/把/是/要…这类
    只能接后续内容、不能收句的字），才认定被截断。
    截断后优先保到最后一个逗号处（保住的是完整分句），连分句都没有就整条丢掉。
    """
    if not reply:
        return reply
    lines = [ln.strip() for ln in reply.split('\n') if ln.strip()]
    if not lines:
        return ""
    closing = "。！？…～!?.~」』）)]）"
    dangling = "我你他她它就都而但却又再和跟把被让给是要能会在到从向为以因所还更最"
    last = lines[-1]
    if last[-1] in closing:
        return reply
    if len(last) <= 5:
        return reply
    if last[-1] not in dangling:
        return reply
    cut = -1
    for i in range(len(last) - 1, -1, -1):
        if last[i] in "，、；：,":
            cut = i
            break
    if cut >= 4:
        lines[-1] = last[:cut].strip() + "。"
        print(f"  [截断] 末条被切成半句，已收到「{lines[-1][:24]}」")
        return "\n".join(lines)
    print(f"  [截断] 末条「{last[-14:]}」不完整且无可收尾处，整条丢弃")
    lines = lines[:-1]
    return "\n".join(lines)


def _send_split_messages(reply, hwnd=None) -> int:
    """按标点拆句，逐条发送（AI 生成几条就发几条）。返回实际执行发送的条数；
    残句被清空、无有效分句时返回 0，供调用方判断是否真的发了出去。"""
    import pywinauto.keyboard as kb
    import pyperclip
    import re
    reply = trim_incomplete_reply(reply)
    if not reply.strip():
        print("  [发送] 回复全是残句，已取消发送")
        return 0
    # 先激活微信窗口，确保输入框有焦点
    if hwnd:
        activate_window(hwnd)
        time.sleep(0.5)
    # 优先按换行切分：AI 在生成时用「换行分隔多条消息」，按行切分即得实际条数。
    # 无换行时只按句末标点（。！？…）兜底拆句，绝不按「，；、」拆 ——
    # 逗号切分会把一句完整的话剁成「两分钟」「不耽误您时间」「您要是方便」
    # 这种半截气泡（20261107_120542 日志 + 实际聊天截图实证），读起来非常怪。
    if '\n' in reply:
        parts = [p.strip() for p in reply.split('\n') if p.strip()]
    else:
        parts = re.split(r'[。！？…]+', reply)
        parts = [p.strip() for p in parts if p.strip()]
    if not parts:
        parts = [reply]
    # 条数上限由 chat_strategy_config.json 的 max_reply_messages 配置（默认 5），
    # 避免连续刷屏和重复表达（超出部分合并到末条）
    max_parts = _get_max_reply_messages()
    if len(parts) > max_parts:
        parts = parts[:max_parts - 1] + [' '.join(parts[max_parts - 1:])]
    send_interval = _get_send_config()["send_message_interval"]
    sent = 0
    for i, part in enumerate(parts):
        # 去除首尾标点
        part = part.strip('，；。！？…、：')
        if not part:
            continue
        print(f"  [分段] 第{i+1}/{len(parts)}条")
        time.sleep(0.3)
        pyperclip.copy(part)
        time.sleep(0.2)
        kb.send_keys("^v")
        time.sleep(0.2)
        kb.send_keys("{ENTER}")
        time.sleep(send_interval)
        sent += 1
    return sent


def _check_config_files() -> bool:
    """检查配置文件是否存在。"""
    return CONTACTS_CONFIG_PATH.exists() and STRATEGY_CONFIG_PATH.exists()


def _clean_reply(reply: str) -> str:
    """清理回复文本：压平连续空格/制表符，但保留换行（换行是多条消息的结构分隔）。"""
    import re
    lines = [re.sub(r'[ \t]+', ' ', ln.strip()) for ln in reply.split('\n')]
    return '\n'.join(ln for ln in lines if ln)


def strip_model_commentary(reply: str) -> str:
    """Remove model analysis/commentary that should never be sent to WeChat."""
    import re
    text = reply.strip()
    markers = [
        "这回复看起来",
        "这句话看起来",
        "对方对",
        "对方似乎",
        "对方应该",
        "可以顺势",
        "可以接着",
        "比如问",
        "建议",
        "说明对方",
        "从截图",
        "根据截图",
        "策略",
        "分析",
    ]
    cut_at = len(text)
    for marker in markers:
        idx = text.find(marker)
        if idx > 0:
            cut_at = min(cut_at, idx)
        elif idx == 0:
            return ""
    if cut_at != len(text):
        text = text[:cut_at].rstrip("，,。.!！?？；;：:、 ")
        print("  [AI] 已裁掉模型分析旁白")

    text = re.sub(r"（[^）]*(分析|策略|建议|截图|对方|顺势)[^）]*）", "", text)
    text = re.sub(r"\([^)]*(analysis|strategy|suggest|screenshot)[^)]*\)", "", text, flags=re.IGNORECASE)
    return text.strip()


def contains_model_commentary(reply: str) -> bool:
    bad_patterns = [
        "这回复看起来",
        "可以顺势",
        "比如问",
        "说明对方",
        "根据截图",
        "策略要求",
        "应该回复",
    ]
    return any(p in reply for p in bad_patterns)


def starts_with_laugh(reply: str) -> bool:
    import re
    return bool(re.match(r"^\s*(哈哈哈*|嘻嘻+|嘿嘿+|hhh+|😂+|🤣+)", reply or "", re.IGNORECASE))


def reduce_laugh_prefix(reply: str, recent_replies: list[str]) -> str:
    """Avoid starting too many consecutive replies with laugh fillers."""
    import re
    recent_laughs = sum(1 for old in recent_replies[-3:] if starts_with_laugh(old))
    if recent_laughs == 0 or not starts_with_laugh(reply):
        return reply

    cleaned = re.sub(
        r"^\s*(哈哈哈*|嘻嘻+|嘿嘿+|hhh+|😂+|🤣+)[，,。.\s～~！!]*",
        "",
        reply,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    if cleaned:
        print("  [AI] 已去掉重复笑声开头")
        return cleaned
    return reply


def reply_similarity(a: str, b: str) -> float:
    """Lightweight character-bigram similarity for near-duplicate reply blocking."""
    def grams(s: str) -> set[str]:
        import re
        compact = re.sub(r"\s+", "", s or "")
        if len(compact) < 2:
            return {compact} if compact else set()
        return {compact[i:i + 2] for i in range(len(compact) - 1)}

    ga = grams(a)
    gb = grams(b)
    if not ga or not gb:
        return 0.0
    return len(ga & gb) / len(ga | gb)


def is_echoing_other_message(reply: str, other_text: str) -> bool:
    """Block replies that simply repeat the other person's latest message."""
    text = (other_text or "").strip()
    if not text or text.startswith("["):
        return False
    compact_reply = "".join((reply or "").split())
    compact_other = "".join(text.split())
    if not compact_reply or not compact_other:
        return False
    # 对方消息太短（<4字，如"？""好""嗯"）时，做严格复读判断，不做模糊相似度
    if len(compact_other) < 4:
        return compact_reply == compact_other
    if compact_reply == compact_other:
        return True
    if compact_other in compact_reply and len(compact_other) >= 6:
        return True
    return reply_similarity(compact_reply, compact_other) >= 0.8


def asks_known_yes_answer_again(reply: str, other_text: str) -> bool:
    """Block asking a yes/no fact again after the other person has just confirmed it."""
    import re
    other = "".join((other_text or "").split())
    if not re.search(r"(会啊|会呀|会的|喜欢|挺喜欢|去过|经常|常去)", other):
        return False
    compact = "".join((reply or "").split())
    repeated_manzhan = [
        "经常去漫展",
        "会去漫展",
        "喜欢去漫展",
        "去漫展玩",
        "看相关漫展",
        "参加活动吗",
        "看相关作品比较多",
    ]
    if "漫展" in compact and any(p in compact for p in repeated_manzhan):
        return True
    if "活动" in compact and ("会去" in compact or "经常去" in compact or "参加活动吗" in compact):
        return True
    return False


def positive_acceptance_needs_reply(other_text: str) -> bool:
    """Obvious positive acceptance from the other side should be acknowledged."""
    import re
    text = "".join((other_text or "").split())
    if not text or text.startswith("["):
        return False
    if any(word in text for word in ("不一定", "不太想", "不想", "不去", "没空", "算了")):
        return False
    return bool(re.search(r"(好啊|好呀|可以啊|可以呀|行啊|行呀|说定了|一定去|有时间.*去|到时候.*去|约|逛逛)", text))


_PUNCT_ONLY_CHARS = set("。，、,.!！?？~～…;；:：\"'“”‘’()（）[]【】<>《》-—_+*/\\|= \t\r\n")
_BARE_ACK_CORES = {"嗯", "哦", "呃", "啊", "呀", "哈", "呵", "好", "行", "是", "对", "ok"}


def bare_message_needs_reply(other_text: str) -> bool:
    """对方只发一个标点/单个字/单个表情时，也是一条要接的话。

    视觉模型容易把「。」「嗯」这类极短消息判成「没有实际内容 → 无需回复」，
    实际对方是在等接话。RPA 层用这个判据兜底，避免对话直接断在这里。
    """
    import re
    text = "".join((other_text or "").split())
    if not text:
        return False
    if text.startswith("["):
        # [表情: xx] / [表情包: xx] 属于对方发来的互动信号，需要接；
        # 图片/文件/链接不在这里兜底，交给模型正常判定。
        return text.startswith("[表情")
    core = "".join(ch for ch in text if ch not in _PUNCT_ONLY_CHARS).strip()
    if not core:
        return True
    return len(core) <= 1 or core.lower() in _BARE_ACK_CORES


_BARE_REPLY_CASUAL = ("嗯嗯，我在呢～", "嗯嗯，你说～", "在的，你说～", "嗯？怎么啦～")
_BARE_REPLY_BUSINESS = ("嗯嗯，您说，我这边在听～", "在的，您讲～", "嗯，您继续说～")
_bare_reply_idx = 0


def fallback_bare_message_reply(other_text: str, chat_goal: str = "") -> str:
    """对方只发了标点/单字时的一句自然接话。

    轮换多个说法：同一条回复在同一会话里第二次会被重复回复保护拦掉，
    固定一句会导致第二次再碰上句号时又发不出去。
    """
    global _bare_reply_idx
    pool = _BARE_REPLY_BUSINESS if is_business_goal(chat_goal) else _BARE_REPLY_CASUAL
    text = pool[_bare_reply_idx % len(pool)]
    _bare_reply_idx += 1
    return text


# 商务目标特征词：约会目标排除与兜底文案共用这一份判据，保证口径单一来源。
# 20260915 实证：目标「推动产品销售进程邀约会晤」不含旧表任何词、却含约会词
# 「邀约」→ 商务目标被误判为线下约会 → 模型业务回复被替换成「吃饭出去玩」模板。
BUSINESS_GOAL_TERMS = (
    "开会", "详谈", "商务", "合作", "项目", "会议", "演示", "面谈", "线上会",
    "会晤", "约见", "洽谈", "拜访", "销售", "签约", "提案", "报价", "路演",
)


def is_business_goal(chat_goal: str) -> bool:
    """Whether the configured goal is a business/meeting objective."""
    text = "".join((chat_goal or "").split())
    return any(p in text for p in BUSINESS_GOAL_TERMS)


def fallback_positive_acceptance_reply(other_text: str, chat_goal: str = "") -> str:
    """Acknowledge an obvious acceptance; phrasing must match the goal type."""
    text = other_text or ""
    if is_business_goal(chat_goal):
        if any(p in text for p in ("时间", "安排", "排期", "定", "约")):
            return "好的，那我这边先把时间和参与人安排好，稍后同步你～"
        return "收到，那我这边整理下方案，找时间跟你对一下细节～"
    if "逛" in text or "去" in text or "说定" in text:
        return "说定啦，到时候有空一起去逛逛～"
    return "那就说定啦～"


def is_offline_meet_goal(chat_goal: str) -> bool:
    """Whether the configured goal is a personal offline date (约会式).

    debug 088a05 实证：目标「推动对方愿意见面或开会详谈…项目合作」含「见面」
    被误判为约会目标 → 吃饭兜底覆盖模型的商务回复。商务词在场时不算约会目标。
    """
    text = "".join((chat_goal or "").split())
    if any(p in text for p in BUSINESS_GOAL_TERMS):
        return False
    return any(p in text for p in ("线下", "约会", "邀约", "见面", "吃饭", "出去玩", "喝奶茶"))


def is_self_echo_last_text(last_text: str, recent_replies: Optional[list[str]]) -> bool:
    """True when the model's quoted 'other' message is actually our own sent text."""
    compact = "".join((last_text or "").split()).strip("～~。.!！…")
    if len(compact) < 6:
        return False
    for sent in recent_replies or []:
        sc = "".join((sent or "").split())
        if compact in sc or sc in compact:
            return True
    return False


def has_concrete_offline_invite(text: str) -> bool:
    """Detect whether a reply already contains a concrete offline invitation."""
    compact = "".join((text or "").split())
    return any(
        p in compact
        for p in (
            "吃饭",
            "吃个饭",
            "火锅",
            "喝奶茶",
            "喝杯",
            "看电影",
            "出来",
            "出去玩",
            "逛逛",
            "见面",
            "找天",
            "约个",
            "约一下",
            "定个时间",
            "有空一起",
            "忙完一起",
        )
    )


def should_push_offline_goal(other_text: str, reply: str, chat_goal: str, recent_replies: Optional[list[str]] = None) -> bool:
    """For offline goals, avoid treating soft acknowledgements as the end of the thread."""
    if not is_offline_meet_goal(chat_goal):
        return False
    text = "".join((other_text or "").split())
    if not text or text.startswith("["):
        return False
    if any(p in text for p in ("不一定", "不太想", "不想", "不去", "没空", "算了", "改天再说")):
        return False
    if has_concrete_offline_invite(reply):
        return False

    cue = any(
        p in text
        for p in (
            "收到",
            "ok",
            "好的",
            "好吧",
            "行",
            "嗯嗯",
            "哈哈",
            "借你吉言",
            "借你的吉言",
            "接你吉言",
            "接你的吉言",
            "那必须",
            "可以",
            "有空",
            "忙完",
            "顺利",
            "验收",
            "谢谢",
        )
    )
    if not cue:
        return False

    recent_text = "\n".join(recent_replies or [])
    if has_concrete_offline_invite(recent_text):
        return False
    return True


def fallback_offline_goal_reply(other_text: str, recent_replies: Optional[list[str]] = None) -> str:
    """Natural meet-up push when the model misses the configured offline goal."""
    text = "".join((other_text or "").split())
    options = []
    recent_text = "\n".join(recent_replies or [])
    if has_concrete_offline_invite(recent_text):
        options.append("那先这么说定，等你方便了咱们就定个时间～")
        options.append("行，那等你有空咱们就把时间定一下～")
    if "吉言" in text:
        options.append("那必须借你吉言，等你忙完这阵子，找天一起吃个饭放松下～")
    if any(p in text for p in ("戏", "剧组", "搭戏", "合拍", "演")):
        options.append("这梗可以，别光线上聊了，找天出来吃个饭当面聊更有意思～")
    if any(p in text for p in ("忙", "验收", "客户", "顺利")):
        options.append("你先忙正事，等这阵忙完了，咱俩找天吃个饭放松下～")
    if any(p in text.lower() for p in ("ok", "okk")) or any(p in text for p in ("收到", "好的", "好吧")):
        options.append("收到就行，你先忙你的，等忙完找天出来吃个饭或者逛逛～")
    options.append("等你忙完这阵子，咱俩找天吃个饭或者出去玩一趟～")

    recent = set((recent_replies or [])[-8:])
    for option in options:
        if option not in recent:
            return option
    return options[-1]


def fallback_other_message_reply(other_text: str, chat_goal: str = "", recent_replies: Optional[list[str]] = None) -> str:
    """Short local fallback when the last visible message is from the other side."""
    text = "".join((other_text or "").split())
    if should_push_offline_goal(other_text, "", chat_goal, recent_replies):
        return fallback_offline_goal_reply(other_text, recent_replies)
    if any(p in text for p in ("晚安", "早点睡", "早睡", "睡吧", "休息吧")):
        return "好嘞，那你也早点休息，晚安～"
    if any(p in text for p in ("早啊", "早上好", "早呀")):
        return "早呀，今天也顺顺利利～"
    if any(p in text.lower() for p in ("ok", "okk")) or any(p in text for p in ("好的", "好嘞", "好呀", "行", "嗯嗯")):
        return "好嘞～"
    if "客户" in text or "验收" in text:
        return "明白啦，那明天验收顺顺利利～"
    return "好嘞，我知道啦～"


def extract_visible_chat_from_image(image_data_uri: str, contact_name: str) -> dict:
    """Use the vision model only to transcribe/describe the visible chat area."""
    strategy = _get_strategy()
    if strategy is None:
        return {"error": "strategy_unavailable", "messages": []}
    user_id = strategy._default_api_user_id()
    if not user_id:
        return {"error": "user_unavailable", "messages": []}
    print("  [AI] 提取聊天区文字记录（图片/表情转文字描述）...")
    started = time.perf_counter()
    result = strategy.extract_chat_history_from_image(user_id=user_id, image_url=image_data_uri)
    elapsed = time.perf_counter() - started
    if result.get("error"):
        print(f"  [AI] 提取失败: {result.get('error')} [{elapsed:.1f}s]")
        if result.get("api_should_stop"):
            print(f"  [AI] {result.get('api_user_message') or 'AI 调用失败，请稍后再试。'}")
    else:
        print(f"  [AI] 已提取 {len(result.get('messages') or [])} 条可见消息 [{elapsed:.1f}s]")
    return result


def _merge_recent_replies(memory_replies: list[str], runtime_replies: list[str]) -> list[str]:
    """Merge persisted and in-memory recent replies while preserving order."""
    merged: list[str] = []
    for item in (memory_replies or []) + (runtime_replies or []):
        text = str(item or "").strip()
        if text and text not in merged:
            merged.append(text)
    return merged[-5:]


def sent_history_for_contact(contact_name: str) -> set[str]:
    """In-process duplicate guard scoped per contact."""
    key = (contact_name or "unknown").strip() or "unknown"
    return _sent_history_by_contact.setdefault(key, set())


def show_human_handoff_popup(contact_name: str, reason: str, last_text: str = "") -> None:
    """Show a blocking Windows popup so the operator can take over."""
    title = "微信自动化需要人工接管"
    message = f"联系人：{contact_name or '未知'}\n原因：{reason or '对话需要人工判断'}"
    if last_text:
        message += f"\n对方最后一句：{last_text[:120]}"
    try:
        import ctypes
        flags = 0x00001000 | 0x00040000 | 0x00010000 | 0x00000040
        ctypes.windll.user32.MessageBoxW(0, message, title, flags)
    except Exception:
        try:
            import tkinter as tk
            from tkinter import messagebox
            root = tk.Tk()
            root.withdraw()
            messagebox.showinfo(title, message)
            root.destroy()
        except Exception as e:
            print(f"  [接管] 弹框失败: {e}")


# ============================================================
# 达成事件：本地记录 + 转发到服务器（手机端拉取）
# ============================================================
# 设计要点
#   1. 事件先写本地 JSONL（可信流水），再尝试转发；转发失败不影响 RPA 主流程。
#   2. 事件先落 pending/ 再发送，成功即删除 —— 进程被关掉也不会丢。
#   3. 绑定键 = sha256(账号 api_key)[:32]，和手机端算法一致；服务端只存哈希。
#   4. 转发全部在后台线程做，监控循环永远不会被网络阻塞。

_RELAY_PENDING_DIR = EVENT_LOG_DIR / "pending"
_RELAY_RETRY_DELAYS = (1.0, 3.0, 10.0, 30.0)


def _read_json_dict(path) -> dict:
    """读 JSON 文件；不存在或格式错误都返回空 dict，绝不抛异常。"""
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except FileNotFoundError:
        return {}
    except Exception as e:
        print(f"  [转发] 读取配置失败 {path}: {e}")
        return {}


def _relay_settings() -> dict:
    """读取 runtime/relay_config.json（优先）或 user_config.json 的 ``relay`` 段。

    ``bind_key`` 无需手填：由 ``user_config.json`` 的 ``lobster.api_key``
    （账号 API Key）派生。手机端用同一个 Key 算出同一个值，两端天然对齐，
    服务端只保存这个哈希，拿不到明文 Key。

    环境变量 ``RPA_RELAY_URL`` / ``RPA_RELAY_KEY`` 可临时覆盖，方便测试。
    """
    settings = {
        "enabled": False,
        "url": "",
        "key": "",
        "bind_key": "",
        "suppress_popup": False,
    }

    user_cfg = _read_json_dict(USER_CONFIG_PATH)
    relay_file = _read_json_dict(RELAY_CONFIG_PATH)
    # relay_config.json 优先，退回 user_config.json 的 relay 段
    relay = relay_file if relay_file else user_cfg.get("relay")
    if not isinstance(relay, dict):
        relay = {}

    url = str(relay.get("url") or "").strip().rstrip("/")
    url = os.environ.get("RPA_RELAY_URL", url).strip().rstrip("/")
    settings["url"] = url
    settings["key"] = os.environ.get("RPA_RELAY_KEY", str(relay.get("key") or "").strip()).strip()
    settings["suppress_popup"] = bool(relay.get("suppress_popup", False))
    settings["enabled"] = bool(relay.get("enabled", bool(url))) and bool(url)

    bind_key = str(relay.get("bind_key") or "").strip()
    if not bind_key:
        lobster = user_cfg.get("lobster")
        api_key = ""
        if isinstance(lobster, dict):
            api_key = str(lobster.get("api_key") or "").strip()
        if api_key:
            bind_key = hashlib.sha256(api_key.encode("utf-8")).hexdigest()[:32]
    settings["bind_key"] = bind_key
    return settings


def log_relay_status() -> None:
    """启动时打一行，方便和手机端设置页里的绑定键对照。"""
    settings = _relay_settings()
    if not settings["enabled"]:
        print("  [转发] 未启用（runtime/relay_config.json 里 enabled=false 或 url 为空）")
        return
    if not settings["bind_key"]:
        print("  [转发] 已启用但缺少账号 API Key，无法计算绑定键，手机端收不到")
        return
    print(
        f"  [转发] 服务器 {settings['url']} · 绑定键 {settings['bind_key'][:8]}…"
        "（与手机端「通知设置」里的一致即连通）"
    )


def _pc_id() -> str:
    """本机稳定标识，便于手机端区分是哪台电脑上报的。"""
    try:
        import platform as _platform

        node = (_platform.node() or "").strip()
    except Exception:
        node = ""
    if not node:
        return "pc-unknown"
    return "pc-" + hashlib.sha1(node.encode("utf-8")).hexdigest()[:8]


def _build_goal_event(
    event_type: str,
    contact_name: str,
    reason: str,
    last_text: str = "",
    source: str = "rule",
    chat_goal: str = "",
) -> dict:
    contact = (contact_name or "未知").strip() or "未知"
    text = (reason or "对话需要人工接管。").strip()
    return {
        "event_id": uuid.uuid4().hex,
        "event_type": event_type,
        "contact": contact,
        "reason": text,
        "last_text": (last_text or "").strip()[:200],
        "chat_goal": chat_goal or "",
        "source": source,
        "skill_name": current_agent_name(),
        "skill_id": current_skill_id(),
        "account_id": current_account_id(),
        "pc_id": _pc_id(),
        "occurred_at": int(time.time() * 1000),
        "title": "有客户达成目标" if event_type == "goal_achieved" else "微信需要人工接管",
        "summary": f"{contact} · {text}"[:200],
    }


def _append_event_log(event: dict) -> None:
    """本地流水，一天一个文件；即使服务器全挂，本地也有据可查。"""
    try:
        EVENT_LOG_DIR.mkdir(parents=True, exist_ok=True)
        day = datetime.now().strftime("%Y%m%d")
        path = EVENT_LOG_DIR / f"goal_events_{day}.jsonl"
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")
    except Exception as e:
        print(f"  [转发] 写本地事件流水失败: {e}")


def _pending_file(event_id: str) -> Path:
    return _RELAY_PENDING_DIR / f"{event_id}.json"


def _save_pending(event: dict) -> None:
    try:
        _RELAY_PENDING_DIR.mkdir(parents=True, exist_ok=True)
        _pending_file(event["event_id"]).write_text(
            json.dumps(event, ensure_ascii=False), encoding="utf-8"
        )
    except Exception as e:
        print(f"  [转发] 写待补传文件失败: {e}")


def _drop_pending(event_id: str) -> None:
    try:
        _pending_file(event_id).unlink(missing_ok=True)
    except Exception:
        pass


def _post_relay_event(settings: dict, event: dict) -> tuple[bool, str]:
    """POST /notify。成功返回 (True, "")，失败返回 (False, 原因)。"""
    body = json.dumps(
        {
            "eventId": event["event_id"],
            "bindKey": settings["bind_key"],
            "title": event.get("title") or "有客户达成目标",
            "body": event.get("summary") or "",
            "contact": event.get("contact") or "",
            "agentName": event.get("skill_name") or "",
            "skillId": event.get("skill_id") or "",
            "accountId": event.get("account_id") or "",
            "reason": event.get("reason") or "",
            "eventType": event.get("event_type") or "goal_achieved",
            "pcId": event.get("pc_id") or "",
            "occurredAt": event.get("occurred_at") or int(time.time() * 1000),
        },
        ensure_ascii=False,
    ).encode("utf-8")

    headers = {"Content-Type": "application/json"}
    if settings.get("key"):
        headers["X-Relay-Key"] = settings["key"]

    req = urllib.request.Request(
        f"{settings['url']}/notify", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=6) as resp:
            if 200 <= resp.status < 300:
                return True, ""
            return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, str(e)


def _relay_deliver(settings: dict, event: dict) -> None:
    """后台线程：带退避重试地投递；彻底失败则保留 pending 等下次启动补传。"""
    total = len(_RELAY_RETRY_DELAYS)
    for attempt in range(1, total + 1):
        ok, err = _post_relay_event(settings, event)
        if ok:
            _drop_pending(event["event_id"])
            print(
                f"  [转发] 已送达服务器（第 {attempt} 次）: "
                f"{event['contact']} · {event['event_type']}"
            )
            return
        print(f"  [转发] 第 {attempt}/{total} 次失败: {err}")
        if attempt < total:
            time.sleep(_RELAY_RETRY_DELAYS[attempt - 1])
    print("  [转发] 投递失败，已留在 events/pending，下次启动自动补传")


def emit_goal_event(
    event_type: str,
    contact_name: str,
    reason: str,
    last_text: str = "",
    source: str = "rule",
    chat_goal: str = "",
) -> dict:
    """记录一条达成/接管事件并异步转发；任何异常都不影响主流程。"""
    try:
        event = _build_goal_event(event_type, contact_name, reason, last_text, source, chat_goal)
        _append_event_log(event)

        settings = _relay_settings()
        if not settings["enabled"]:
            print("  [转发] 未配置 relay.url，仅记录本地事件")
            return event
        if not settings["bind_key"]:
            print("  [转发] 缺少账号 API Key，无法计算绑定键，仅记录本地事件")
            return event

        _save_pending(event)  # 先落盘：进程随时被关也不丢
        threading.Thread(
            target=_relay_deliver, args=(settings, event), daemon=True, name="relay-deliver"
        ).start()
        return event
    except Exception as e:
        print(f"  [转发] 生成事件失败: {e}")
        return {}


def replay_pending_events(max_items: int = 50) -> None:
    """启动时补传上次没发出去的事件（家宽抖动、服务器重启都会走到这里）。"""
    try:
        if not _RELAY_PENDING_DIR.exists():
            return
        files = sorted(_RELAY_PENDING_DIR.glob("*.json"))[:max_items]
        if not files:
            return
        settings = _relay_settings()
        if not settings["enabled"] or not settings["bind_key"]:
            return
        print(f"  [转发] 发现 {len(files)} 条待补传事件，开始补传")
    except Exception:
        return

    def _run() -> None:
        for path in files:
            try:
                event = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                continue
            ok, err = _post_relay_event(settings, event)
            if not ok:
                print(f"  [转发] 补传失败（保留到下次）: {err}")
                break
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            print(f"  [转发] 补传成功: {event.get('contact')}")

    threading.Thread(target=_run, daemon=True, name="relay-replay").start()


# ============================================================
# 聊天日志同步：记忆更新 → 增量上报到服务器（手机端按员工归档）
# ============================================================
# 设计要点：
#   1. 记忆落盘后只入队，后台线程按节流窗口合并，避免截图周期每轮都打服务器；
#   2. 首次基线整段尾部上传，之后只传本地 ack 集合里没有的新消息；
#   3. 投递失败落 logs_pending/ 待补传，启动时 replay_pending_logs() 重发；
#   4. 任何异常都兜住，绝不影响 RPA 主循环。
_LOG_SYNC_THROTTLE_SEC = 5.0
_LOG_MAX_MESSAGES = int(DEFAULT_CONVERSATION_MEMORY_CONFIG["max_messages_per_contact"])
_RELAY_LOG_PENDING_DIR = EVENT_LOG_DIR / "logs_pending"
_LOG_ACK_FILE = EVENT_LOG_DIR / "log_ack.json"

_log_queue: dict[str, str] = {}  # contact_key -> contact_name
_log_worker_started = False
_log_worker_lock = threading.Lock()


def _contact_key(contact_name: str, account_id: str, agent: str) -> str:
    return f"{agent}|{account_id}|{contact_name}"


def _load_log_ack() -> dict:
    try:
        if _LOG_ACK_FILE.exists():
            data = json.loads(_LOG_ACK_FILE.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
    except Exception as e:
        print(f"  [日志同步] 读取 ack 状态失败: {e}")
    return {}


def _save_log_ack(ack: dict) -> None:
    try:
        _RELAY_LOG_PENDING_DIR.mkdir(parents=True, exist_ok=True)
        _LOG_ACK_FILE.write_text(json.dumps(ack, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        print(f"  [日志同步] 写 ack 状态失败: {e}")


def enqueue_chat_log_sync(contact_name: str, memory: dict) -> None:
    """记忆落盘后调用：把该联系人加入同步队列，后台线程节流合并上传。"""
    try:
        if not load_conversation_memory_config().get("enabled", True):
            return
        settings = _relay_settings()
        if not settings["enabled"] or not settings["bind_key"]:
            return
        if not isinstance(memory, dict):
            return
        agent = current_agent_name()
        account = current_account_id()
        key = _contact_key(contact_name, account, agent)
        _log_queue[key] = contact_name
        _start_log_sync_worker()
    except Exception as e:
        print(f"  [日志同步] 入队失败: {e}")


def _start_log_sync_worker() -> None:
    global _log_worker_started
    with _log_worker_lock:
        if _log_worker_started:
            return
        _log_worker_started = True
    threading.Thread(target=_log_sync_worker_loop, daemon=True, name="relay-log-sync").start()


def _build_log_record(contact_name: str, memory: dict) -> Optional[dict]:
    agent = current_agent_name()
    account = current_account_id()
    skill_id = current_skill_id()
    messages = memory.get("messages") if isinstance(memory, dict) else []
    if not isinstance(messages, list):
        messages = []
    messages = messages[-_LOG_MAX_MESSAGES:]
    if not messages:
        return None

    ack = _load_log_ack()
    key = _contact_key(contact_name, account, agent)
    entry = ack.get(key) or {}
    acked_ids = set(entry.get("acked_ids") or [])
    # 首次基线：没有 ack 记录时整段尾部都作为基线上传；之后只传增量
    is_baseline = not acked_ids
    delta = [
        m for m in messages
        if isinstance(m, dict) and str(m.get("id") or "") not in acked_ids
    ]
    payload_msgs = messages if is_baseline else delta
    if not payload_msgs:
        return None

    last_seen = ""
    count = 0
    for m in messages:
        if isinstance(m, dict):
            count += 1
            ts = str(m.get("seen_at") or m.get("timestamp") or "")
            if ts > last_seen:
                last_seen = ts
    record_id = hashlib.sha1(
        f"{skill_id}|{account}|{contact_name}|{count}|{last_seen}".encode("utf-8")
    ).hexdigest()

    return {
        "record_id": record_id,
        "contact": contact_name,
        "agent_name": agent,
        "skill_id": skill_id,
        "account_id": account,
        "pc_id": _pc_id(),
        "updated_at": int(time.time() * 1000),
        "messages": [
            {
                "id": str(m.get("id") or ""),
                "sender": str(m.get("sender") or "unknown"),
                "side": str(m.get("side") or ""),
                "content_type": str(m.get("content_type") or "text"),
                "content": str(m.get("content") or ""),
                "timestamp": m.get("timestamp"),
                "seen_at": m.get("seen_at"),
                "source": m.get("source") or "",
            }
            for m in payload_msgs if isinstance(m, dict)
        ],
        "_ack_key": key,
        "_all_ids": [str(m.get("id") or "") for m in messages if isinstance(m, dict)],
    }


def _post_chat_log(settings: dict, record: dict) -> tuple[bool, str]:
    body = json.dumps(
        {
            "recordId": record["record_id"],
            "bindKey": settings["bind_key"],
            "skillId": record["skill_id"],
            "skillName": record["agent_name"],
            "accountId": record["account_id"],
            "contact": record["contact"],
            "pcId": record["pc_id"],
            "updatedAt": record["updated_at"],
            "messages": record["messages"],
        },
        ensure_ascii=False,
    ).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if settings.get("key"):
        headers["X-Relay-Key"] = settings["key"]
    req = urllib.request.Request(
        f"{settings['url']}/chat-log", data=body, headers=headers, method="POST"
    )
    try:
        with urllib.request.urlopen(req, timeout=8) as resp:
            if 200 <= resp.status < 300:
                return True, ""
            return False, f"HTTP {resp.status}"
    except Exception as e:
        return False, str(e)


def _log_pending_file(record_id: str) -> Path:
    return _RELAY_LOG_PENDING_DIR / f"{record_id}.json"


def _mark_log_acked(record: dict) -> None:
    ack = _load_log_ack()
    ack[record["_ack_key"]] = {
        "skill_id": record["skill_id"],
        "skill_name": record["agent_name"],
        "account_id": record["account_id"],
        "contact": record["contact"],
        "acked_ids": record["_all_ids"][-_LOG_MAX_MESSAGES:],
    }
    _save_log_ack(ack)


def _sync_one_log(settings: dict, record: dict) -> bool:
    """投递一条日志记录；成功更新 ack，失败保留 pending。"""
    ok, err = _post_chat_log(settings, record)
    if not ok:
        try:
            _RELAY_LOG_PENDING_DIR.mkdir(parents=True, exist_ok=True)
            _log_pending_file(record["record_id"]).write_text(
                json.dumps(record, ensure_ascii=False), encoding="utf-8"
            )
        except Exception:
            pass
        print(f"  [日志同步] 投递失败: {err}")
        return False

    _mark_log_acked(record)
    try:
        _log_pending_file(record["record_id"]).unlink(missing_ok=True)
    except Exception:
        pass
    print(f"  [日志同步] 已上传 {record['contact']} · {len(record['messages'])} 条")
    return True


def _log_sync_worker_loop() -> None:
    while True:
        time.sleep(_LOG_SYNC_THROTTLE_SEC)
        try:
            settings = _relay_settings()
            if not settings["enabled"] or not settings["bind_key"]:
                _log_queue.clear()
                continue
            if not _log_queue:
                continue
            pending = list(_log_queue.items())
            _log_queue.clear()
            for _key, contact in pending:
                try:
                    memory = load_contact_memory(contact)
                    record = _build_log_record(contact, memory)
                    if record:
                        _sync_one_log(settings, record)
                except Exception as e:
                    print(f"  [日志同步] 处理 {contact} 失败: {e}")
        except Exception as e:
            print(f"  [日志同步] 后台循环异常: {e}")


def replay_pending_logs(max_items: int = 50) -> None:
    """启动时补传上次没发出去的日志记录。"""
    try:
        if not _RELAY_LOG_PENDING_DIR.exists():
            return
        files = sorted(_RELAY_LOG_PENDING_DIR.glob("*.json"))[:max_items]
        if not files:
            return
        settings = _relay_settings()
        if not settings["enabled"] or not settings["bind_key"]:
            return
        print(f"  [日志同步] 发现 {len(files)} 条待补传日志，开始补传")
    except Exception:
        return

    def _run() -> None:
        for path in files:
            try:
                record = json.loads(path.read_text(encoding="utf-8"))
            except Exception:
                try:
                    path.unlink(missing_ok=True)
                except Exception:
                    pass
                continue
            ok, err = _post_chat_log(settings, record)
            if not ok:
                print(f"  [日志同步] 补传失败（保留到下次）: {err}")
                break
            _mark_log_acked(record)
            try:
                path.unlink(missing_ok=True)
            except Exception:
                pass
            print(f"  [日志同步] 补传成功: {record.get('contact')}")

    threading.Thread(target=_run, daemon=True, name="relay-log-replay").start()


def notify_mobile_message(payload: dict) -> bool:
    """［已废弃］旧的 8899 网关桩，保留签名只为兼容，内部走 relay。"""
    try:
        event = emit_goal_event(
            str(payload.get("type") or "goal_achieved"),
            str(payload.get("contact_name") or ""),
            str(payload.get("reason") or ""),
            str(payload.get("last_text") or ""),
        )
        return bool(event)
    except Exception as e:
        print(f"  [转发] 兼容调用失败: {e}")
        return False


def request_human_handoff(
    contact_name: str,
    reason: str,
    last_text: str = "",
    stop_monitor: bool = True,
    source: str = "rule",
) -> None:
    """Notify the operator to take over: 手机推送 + 本地弹窗。

    stop_monitor=True  → 致命接管：置停止标志，弹窗保持阻塞（进程即将退出）。
    stop_monitor=False → 单个联系人达成：只通知，继续监控其他联系人，
                         弹窗放后台线程，绝不阻塞监控循环。
    """
    global STOP_FLAG, FATAL_EXIT_REASON
    text = reason or "对话需要人工接管。"
    if stop_monitor:
        FATAL_EXIT_REASON = text
        STOP_FLAG = True
    print(f"  [接管] {text}")

    settings = _relay_settings()
    emit_goal_event(
        "human_handoff" if stop_monitor else "goal_achieved",
        contact_name,
        text,
        last_text,
        source=source,
    )

    if stop_monitor:
        # 进程即将退出，阻塞弹窗可以让操作员看到原因再关闭
        show_human_handoff_popup(contact_name, text, last_text)
    elif not settings.get("suppress_popup"):
        # 后台线程弹：以前这里会卡死整个监控循环
        threading.Thread(
            target=show_human_handoff_popup,
            args=(contact_name, text, last_text),
            daemon=True,
            name="handoff-popup",
        ).start()


_ACK_BARE_TEXTS = {
    "嗯", "嗯嗯", "嗯哪", "嗯呐", "好", "好的", "好滴", "好呀", "好啊", "行", "可以",
    "ok", "okk", "okay", "是", "是的", "对", "对的", "没问题", "那行", "成", "中",
}

# 我方上一句带这些标记 = 在提问 / 邀约 / 让确认，对方的短肯定就是「答应」
_PREV_ASK_MARKERS = (
    "吗", "么", "好不好", "行不行", "可以吗", "要不要", "怎么样", "如何", "是不是",
    "是否", "可行", "合适", "哪天", "什么时候", "几号", "几点", "方便", "一起",
    "约", "见个面", "见一面", "定一下", "确认", "安排", "发我", "帮我看", "你看呢",
    "你觉得", "有空",
)

# 对方明确表示「别再发了 / 不感兴趣」时，不能静默装死：先发一句礼貌收尾，再停止对该联系人的自动回复。
# 实测（20261111_123245 日志第 6/7 轮）：对方说「你不要再给我发消息了」，
# 模型按 reply 口径给出空 reply → 程序一句话都不回，显得很不礼貌。
_REJECT_MARKERS = (
    "不要再给我发", "不要再发了", "别给我发", "别再给我发", "不要发消息", "别再发消息",
    "不要发信息", "别再发信息", "不用再发", "不用发了", "别发了", "不要再联系",
    "别联系我", "不要打扰我", "别打扰我", "别烦我", "不要烦我", "不感兴趣",
    "不需要了", "拉黑", "投诉", "举报你", "别烦", "骚扰", "别打扰", "别推了", "别推销",
    "删了我", "不想聊", "没兴趣", "烦我", "再烦",
)

_REJECT_REPLY_POOL = (
    "好的，那就不打扰您了。",
    "明白，抱歉打扰您了，您忙您的。",
    "好的，那我先不打扰您了，有需要随时找我。",
    "收到，不好意思打扰您，祝您一切顺利。",
)
_reject_reply_idx = 0

# 已对某人发过收尾语 → 本进程内不再自动回复他（重启后才恢复）
_REJECTED_CONTACTS: set[str] = set()


def is_reject_stop_other_text(text: str) -> bool:
    """对方是否明确表示「别再发了／不感兴趣」。"""
    t = (text or "").strip()
    if not t:
        return False
    t = t.replace("您", "你")
    return any(m in t for m in _REJECT_MARKERS)


def fallback_reject_reply() -> str:
    """礼貌收尾语轮换池：避免被「已发过相同内容」拦掉。"""
    global _reject_reply_idx
    reply = _REJECT_REPLY_POOL[_reject_reply_idx % len(_REJECT_REPLY_POOL)]
    _reject_reply_idx += 1
    return reply


def last_me_message_text(contact_name: str) -> str:
    """取我方最近一条已发消息，用于判断对方短回应到底在回应什么。"""
    try:
        memory = load_contact_memory(contact_name)
    except Exception:
        return ""
    for msg in reversed(memory.get("messages") or []):
        if not isinstance(msg, dict):
            continue
        if str(msg.get("sender") or "").strip().lower() == "me":
            return str(msg.get("content") or "").strip()
    return ""


def ack_is_agreement(other_text: str, prev_me_text: str) -> bool:
    """对方的「嗯/嗯嗯/好/行/可以」是不是在同意我方上一句。

    上下文联动是这里的关键：同一个「嗯嗯」——
    - 我方上一句是提问/邀约/让确认 → 对方是在答应，应顺势推进下一步；
    - 我方上一句只是陈述或闲聊 → 才是敷衍/收尾。
    只按字面把「嗯嗯」一律判成敷衍，会把真答应直接转人工，丢掉成交机会。
    """
    compact = "".join((other_text or "").split()).strip("，。,.!！?？~～…")
    if not compact:
        return False
    if compact.lower() not in _ACK_BARE_TEXTS:
        return False
    prev = (prev_me_text or "").strip()
    if not prev:
        return False
    if prev.rstrip().endswith(("？", "?", "吗", "呢")):
        return True
    return any(k in prev for k in _PREV_ASK_MARKERS)


def fallback_agreement_reply(chat_goal: str = "") -> str:
    """对方答应后顺势推进一句（按目标性质选口径，避免商务目标被写成私人约饭）。"""
    if is_business_goal(chat_goal):
        return "好，那我这边把时间确定一下，您哪天方便？"
    if is_offline_meet_goal(chat_goal):
        return "那说定了～你这周末哪天有空？"
    return "那咱们就这么说定了，你这边什么时候方便？"


def is_perfunctory_or_closing_other_text(text: str, prev_me_text: str = "") -> bool:
    """Detect low-signal or conversation-closing replies from the other side."""
    import re
    compact = "".join((text or "").split()).strip("，。,.!！?？~～…")
    if not compact or compact.startswith("["):
        return False
    # 上下文联动：对方的短肯定若是在答应我方上一句的提问/邀约，不算敷衍。
    if ack_is_agreement(compact, prev_me_text):
        return False
    normalized = compact.lower()
    if normalized in {"哦", "喔", "噢", "哦哦", "喔喔", "噢噢", "啊", "昂", "嗯", "嗯嗯", "额", "呃", "em", "emm", "emmm", "ok", "okk"}:
        return True
    if re.fullmatch(r"(哦|喔|噢|啊|昂|嗯|额|呃|哈|哈哈){1,4}", compact):
        return True
    closing_phrases = (
        "先这样",
        "就这样",
        "回头聊",
        "一会聊",
        "下次聊",
        "改天聊",
        "忙去了",
        "先忙",
        "去忙",
        "有事",
        "不聊了",
        "聊完了",
        "没啥聊",
        "没话说",
        "不知道说啥",
        "随便吧",
        "都行吧",
        "行吧",
        "好吧",
        "算了",
        "睡了",
        "晚安",
    )
    return any(p in compact for p in closing_phrases)


def is_goal_ready_for_human_handoff(text: str, chat_goal: str = "") -> bool:
    """Detect goal-accepted replies where a human should take over instead of looping."""
    compact = "".join((text or "").split()).strip("，。,.!！?？~～…")
    if not compact or compact.startswith("["):
        return False
    if any(p in compact for p in ("不一定", "不太想", "不想", "不去", "没空", "算了")):
        return False

    if is_offline_meet_goal(chat_goal):
        if any(
            p in compact
            for p in (
                "必须安排",
                "一定安排",
                "说定了",
                "约定了",
                "约起",
                "就这么定",
                "到时候见",
                "等你忙完",
                "忙完就去",
                "忙完约",
                "找时间去",
                "找时间约",
                "有空就约",
                "有空一起",
            )
        ):
            return True
        waiting_terms = ("随时待命", "等你号令", "听你号令", "等你召唤", "听你召唤", "你安排", "听你安排", "你定")
        if any(p in compact for p in waiting_terms):
            return True

    return False


def human_handoff_reason_for_other_text(text: str, chat_goal: str = "", prev_me_text: str = "") -> str:
    """Return a human-readable handoff reason for the other side's latest message."""
    if is_perfunctory_or_closing_other_text(text, prev_me_text):
        return "对方回复较敷衍或正在收尾，停止自动回复。"
    if is_goal_ready_for_human_handoff(text, chat_goal):
        return "对方已基本接受邀约或进入约定收口，停止自动回复，交给人工确认时间地点。"
    return ""


def maybe_handoff_for_latest_other_message(contact_name: str, result: dict, chat_goal: str = "") -> bool:
    """When goal is achieved, notify the operator and keep monitoring other contacts."""
    global _goal_achieved_contacts
    if not isinstance(result, dict):
        return False
    last_text = (result.get("last_message_text") or "").strip()
    sender = str(result.get("last_message_sender") or "").strip().lower()
    local_sender = str(result.get("local_last_sender") or "").strip().lower()
    # debug 088a05 实证：本地误报 other + AI 明确 me 时，关键词表拿「我方」文本
    # （如"那先不打扰 等你忙完…"）误命中"等你忙完"→ 误触发人工接管。AI 明确
    # 判定末条为我方时，本地单边信号不得放行 handoff。
    if sender in ("me", "我方"):
        return False
    if local_sender != "other" and sender not in ("other", "对方"):
        return False

    # 1) 模型显式判定目标已达成的路径（goal_reached=true + handoff_message）
    model_goal_reached = bool(result.get("goal_reached", False))
    model_handoff = (result.get("handoff_message") or "").strip()
    if model_goal_reached and not last_text:
        # 模型判定达成但无具体对方文本时，也直接转人工
        reason = model_handoff or "模型判定聊天目标已达成，等待人工确认。"
        _goal_achieved_contacts.add(contact_name)
        print(f"  [达成] {contact_name} 模型判定聊天目标已达成: {reason}")
        request_human_handoff(contact_name, reason, last_text, stop_monitor=False, source="model")
        print(f"  [达成] 跳过 {contact_name}，继续监控其他联系人")
        return True

    # 2) 关键词/规则判定的路径
    prev_me_text = last_me_message_text(contact_name)
    reason = human_handoff_reason_for_other_text(last_text, chat_goal, prev_me_text)
    # reply 口径：模型已经把要接的话写好了，就不能因为「敷衍/收尾」这类关键词
    # 把内容丢掉转人工。只有模型自己没写出内容时，才按敷衍转人工。
    if reason.startswith("对方回复较敷衍") and str(result.get("reply") or "").strip():
        print(f"  [联动] {contact_name} 关键词判敷衍但模型已给出内容，按 reply 为准继续发送")
        return False
    if not reason:
        if model_goal_reached:
            reason = model_handoff or "聊天目标已达成，请人工确认后续安排。"
        else:
            # 对方的短肯定是在答应我方上一句 → 不转人工，交给下游顺势推进
            if ack_is_agreement(last_text, prev_me_text):
                result["ack_agreement"] = True
                print(f"  [联动] {contact_name} 对方的「{last_text[:6]}」是在答应我方上一句，顺势推进不转人工")
            return False
    _goal_achieved_contacts.add(contact_name)
    print(f"  [达成] {contact_name} 聊天目标已达成: {reason}")
    request_human_handoff(
        contact_name, reason, last_text,
        stop_monitor=False,
        source="model" if model_goal_reached else "rule",
    )
    print(f"  [达成] 跳过 {contact_name}，继续监控其他联系人")
    return True


def stop_for_api_failure(message: str) -> None:
    """Stop monitoring after a fatal/retry-exhausted API failure."""
    global STOP_FLAG, FATAL_EXIT_REASON
    text = (message or "网络异常，请稍后再试。").strip()
    FATAL_EXIT_REASON = text
    STOP_FLAG = True
    print(f"  [停止] {text}")


def handle_api_stop_result(result: dict) -> bool:
    """Return True if an API result requires the monitor to stop."""
    if isinstance(result, dict) and result.get("api_should_stop"):
        stop_for_api_failure(result.get("api_user_message") or "AI 调用失败，请稍后再试。")
        return True
    return False


def note_extract_result(ok: bool) -> None:
    """Temporarily skip flaky structured extraction after repeated JSON failures."""
    global _EXTRACT_FAILURE_COUNT, _EXTRACT_DISABLED_UNTIL
    if ok:
        _EXTRACT_FAILURE_COUNT = 0
        return
    _EXTRACT_FAILURE_COUNT += 1
    if _EXTRACT_FAILURE_COUNT >= 2:
        _EXTRACT_DISABLED_UNTIL = time.time() + 600
        print("  [记忆] 截图转写连续失败，10分钟内直接使用旧截图保底路径")


def extract_temporarily_disabled() -> bool:
    """Whether structured extraction should be skipped for now."""
    if _EXTRACT_DISABLED_UNTIL <= time.time():
        return False
    remaining = int(_EXTRACT_DISABLED_UNTIL - time.time())
    print(f"  [记忆] 截图转写暂时跳过，还剩 {remaining} 秒，直接使用旧截图保底路径")
    return True


def _is_proactive_ready(
    proactive_enabled: bool,
    last_send: float,
    first_seen: float,
    now: float,
    interval_seconds: int,
    proactive_count: int = 0,
    second_interval_seconds: int = 3600,
    max_attempts: int = 2,
) -> bool:
    """Decide whether the contact is eligible for a proactive send.

    - Disabled -> never proactive.
    - 已主动次数达到上限 -> 转被动，不再主动。
    - 第 1 次主动：距首次监控 / 上次发送超过首次间隔。
    - 第 2 次及以后：距上次发送超过第二次间隔。
    """
    if not proactive_enabled:
        return False
    if max_attempts > 0 and proactive_count >= max_attempts:
        return False
    # 第二次起用更长的间隔；首次破冰用首次间隔
    interval = second_interval_seconds if proactive_count >= 1 else interval_seconds
    if last_send > 0:
        return now - last_send > interval
    if first_seen > 0:
        return now - first_seen > interval
    return False


def analyze_text_history_for_reply(
    contact_name: str,
    memory: dict,
    gender: str = "未知",
    chat_goal: str = "",
    allow_proactive: bool = False,
    recent_replies: Optional[list[str]] = None,
    local_last_sender: str = "",
    local_last_band: Optional[dict] = None,
) -> str:
    """Generate a reply from persisted text history instead of the raw screenshot."""
    global _LAST_AI_RESULT
    strategy = _get_strategy()
    if strategy is None:
        return ""

    history = prompt_history_from_memory(memory)
    if not history:
        print("  [AI] 跳过: 没有可用文字历史")
        return ""

    print("  [AI] 根据联系人文字历史生成回复...")
    try:
        started = time.perf_counter()
        result = strategy.generate_reply_from_text_history(
            contact_id=contact_name,
            chat_history=history,
            gender=gender,
            chat_goal=chat_goal or None,
            recent_replies=recent_replies or [],
            allow_proactive=allow_proactive,
        )
        elapsed = time.perf_counter() - started
        print(f"  [AI] 文字回复调用完成 [{elapsed:.1f}s]")
        if handle_api_stop_result(result):
            return ""
        _LAST_AI_RESULT[contact_name] = result
        if maybe_handoff_for_latest_other_message(contact_name, result, chat_goal):
            return ""

        if result.get("should_reply") is not True:
            last_text = (result.get("last_message_text") or "").strip()
            sender = str(result.get("last_message_sender") or "").strip().lower()
            _local_sender = str(result.get("local_last_sender") or local_last_sender or "").strip().lower()
            _local_is_other = _local_sender in ("other", "对方")
            if result.get("ack_agreement") and sender in ("other", "对方"):
                # 上下文联动：对方是在答应我方上一句，顺着目标推进，而不是回一句「好」
                _model_reply = (result.get("reply") or "").strip()
                result["should_reply"] = True
                result["reply"] = _model_reply or fallback_agreement_reply(chat_goal)
                print("  [联动] 对方短肯定=同意，按目标顺势推进下一步")
            elif sender in ("other", "对方") and (result.get("reply") or "").strip():
                # 以模型写出来的内容为准：模型已经把要发的话写好了，
                # 就不能再用 should_reply=false 把它丢掉（实测模型偶发「写了话但判不回」）。
                result["should_reply"] = True
                print("  [AI] 按 reply 为准: 模型已给出内容，放行发送")
            elif sender in ("other", "对方") and positive_acceptance_needs_reply(last_text):
                result["should_reply"] = True
                result["reply"] = fallback_positive_acceptance_reply(last_text, chat_goal)
                print("  [AI] 兜底: 对方最后一句是积极确认，生成短回复")
            elif sender in ("other", "对方") and bare_message_needs_reply(last_text):
                # 对方只发了一个句号/单字/表情，模型判成「无需回复」= 漏回；
                # 这类消息实际是在等接话，这里强制接一句。
                result["should_reply"] = True
                result["reply"] = fallback_bare_message_reply(last_text, chat_goal)
                print(f"  [AI] 兜底: 对方只发了「{last_text[:8]}」，强制接话")
            elif _local_is_other and not (result.get("reply") or "").strip():
                # 本轮变化块落在对方一侧（本地硬信号），但模型没识别出对方内容、reply 为空：
                # 不能就此装死，用兜底接话补上，避免对方新消息被漏回。
                result["should_reply"] = True
                result["last_message_sender"] = "other"
                result["reply"] = fallback_bare_message_reply(last_text, chat_goal)
                print("  [AI] 本地判为对方新消息但模型未给出内容，兜底接话")

        if result.get("should_reply") is True:
            # 确定性保护：被动回复时末条是我方就不发，避免自己接自己的话。
            # 主动发送模式例外：那时末条本来就是我方，主动开口是预期行为。
            # 方向交叉：模型判我方但本地像素判对方一侧时，以本地气泡位置为准，不误拦。
            _me_vision = str(result.get("last_message_sender") or "").strip().lower() in ("me", "我方")
            _other_local = str(result.get("local_last_sender") or local_last_sender or "").strip().lower() in ("other", "对方")
            if (not allow_proactive) and _me_vision and not _other_local:
                print("  [AI] 跳过: 末条是我方消息，不自己接自己的话")
                return ""
            reply = (result.get("reply") or "").strip()
            if not reply:
                # 判定了要回但模型没给内容：若末条是对方的极短消息（句号/单字/表情），
                # 用兜底接话补上，否则这条消息又会变成「无需回复」被吞掉。
                _lt = (result.get("last_message_text") or "").strip()
                _snd = str(result.get("last_message_sender") or "").strip().lower()
                _snd_local = str(result.get("local_last_sender") or local_last_sender or "").strip().lower()
                if (_snd in ("other", "对方") or _snd_local in ("other", "对方")) and bare_message_needs_reply(_lt):
                    reply = fallback_bare_message_reply(_lt, chat_goal)
                    print(f"  [AI] 要回但内容为空，末条是对方「{_lt[:8]}」，用兜底接话")
            if reply:
                reply = _clean_reply(reply)
                reply = strip_model_commentary(reply)
                if not reply:
                    print("  [AI] 跳过: 回复只包含模型分析旁白")
                    return ""
                if contains_model_commentary(reply):
                    print("  [AI] 跳过: 回复仍包含模型分析旁白")
                    return ""
                reply = reduce_laugh_prefix(reply, recent_replies or [])
                last_text = (result.get("last_message_text") or "").strip()
                if should_push_offline_goal(last_text, reply, chat_goal, recent_replies):
                    print(f"  [AI] 策略兜底触发: 原回复「{reply[:40]}」被替换为邀约模板（last_text={last_text[:30]}）")
                    reply = fallback_offline_goal_reply(last_text, recent_replies)
                if asks_known_yes_answer_again(reply, last_text):
                    print("  [AI] 跳过: 回复重复追问了对方刚确认过的信息")
                    return ""
                contact_sent_history = sent_history_for_contact(contact_name)
                if reply in contact_sent_history:
                    print("  [AI] 跳过: 已发过相同内容")
                    return ""

                sender = result.get("last_message_sender", "unknown")
                print(f"  [AI] 回复: {reply[:50]}... [last_sender={sender}]")
                contact_sent_history.add(reply)
                return reply

        print(
            "  [AI] 无需回复判定: "
            f"last_sender={result.get('last_message_sender', 'unknown')} "
            f"last_text={(result.get('last_message_text') or '')[:40]}"
        )
        print("  [AI] 无需回复")
        return ""
    except Exception as e:
        print(f"  [AI] 文字历史回复失败: {e}")
        return ""


def analyze_screenshot_via_memory(
    shot,
    contact_name: str,
    gender: str = "未知",
    chat_goal: str = "",
    allow_proactive: bool = False,
    recent_replies: Optional[list[str]] = None,
    local_last_sender: str = "unknown",
    local_last_band: Optional[dict] = None,
) -> str:
    """
    统一入口：是否回复仍由 AI 判定；本地方向信号（本轮变化块侧别 / 气泡位置）只做交叉验证，
    识别不可靠时为 unknown，不单独决定回不回。方向信号由主循环算好后传入。
    """
    print(f"  [本地判定] 气泡方向预判: {local_last_sender or 'unknown'}")
    data_uri = image_to_data_uri(shot)
    return analyze_full_window(
        data_uri,
        contact_name,
        gender=gender,
        chat_goal=chat_goal,
        allow_proactive=allow_proactive,
        recent_replies=recent_replies or [],
        local_last_sender=local_last_sender,
        local_last_band=local_last_band,
    )


def analyze_full_window(
    image_data_uri: str,
    contact_name: str,
    gender: str = "未知",
    chat_goal: str = "",
    allow_proactive: bool = False,
    recent_replies: Optional[list[str]] = None,
    local_last_sender: str = "",
    local_last_band: Optional[dict] = None,
) -> str:
    """
    旧截图直发保底路径：仅在文字提取失败/关闭时使用。
    仍然保留严格 should_reply、last_sender 和重复回复保护。
    """
    global _LAST_AI_RESULT
    strategy = _get_strategy()
    if strategy is None:
        return ""
    user_id = strategy._default_api_user_id()
    if not user_id:
        return ""

    print("  [AI] 保底: 直接分析聊天区截图...")
    try:
        final_goal = chat_goal if chat_goal else None
        _proactive_cfg = _get_proactive_config()
        _proactive_mins = int(_proactive_cfg["proactive_interval_seconds"] / 60)
        if allow_proactive and final_goal:
            final_goal = final_goal + f" 【已{_proactive_mins}分钟没联系，你可以主动发消息，但内容不能和上次重复】"
        elif allow_proactive:
            final_goal = f"【已{_proactive_mins}分钟没联系，你可以主动发消息，但内容不能和上次重复】"

        started = time.perf_counter()
        result = strategy.generate_reply_from_image(
            user_id=user_id,
            image_url=image_data_uri,
            gender=gender,
            contact_display_name=contact_name,
            chat_goal=final_goal,
            long_cycle_monitor=True,
            recent_replies=recent_replies or [],
            local_last_sender=local_last_sender,
            allow_proactive=allow_proactive,
            local_last_band=local_last_band,
        )
        elapsed = time.perf_counter() - started
        print(f"  [AI] 保底截图调用完成 [{elapsed:.1f}s]")
        if handle_api_stop_result(result):
            return ""
        if local_last_sender in ("me", "other"):
            result["local_last_sender"] = local_last_sender
        if local_last_band:
            result["local_last_band"] = local_last_band
        _LAST_AI_RESULT[contact_name] = result
        # debug 088a05 实证：变暗/异常截图下模型会把「我方刚发的话」引用成
        # 对方最后一条消息并回复自己。引用内容与已发历史重合即拒绝。
        # 主动发送模式例外：那时末条本来就是我方，主动开口是预期行为。
        # 方向交叉判断：文本虽与我方历史重合，但模型或本地像素任一判末条在对方一侧，
        # 说明是模型把对方短消息串成了上方我方长句，不能按回声吞掉，交给后续兜底判定。
        if not allow_proactive and is_self_echo_last_text(result.get("last_message_text") or "", recent_replies or []):
            _echo_vision = str(result.get("last_message_sender") or "").strip().lower()
            _echo_local = str(result.get("local_last_sender") or local_last_sender or "").strip().lower()
            _echo_is_other = _echo_vision in ("other", "对方") or _echo_local in ("other", "对方")
            if _echo_is_other:
                print(f"  [AI] 文本与我方历史重合，但方向判为对方（视觉={_echo_vision or '?'} 本地={_echo_local or '?'}），豁免自回复保护")
            else:
                print("  [AI] 跳过: 模型引用的最后一条消息实为我方已发内容（自回复保护）")
                return ""
        # 对方明确「别再发了」：模型通常直接给空 reply（实测），不能就此装死。
        # 先发一句礼貌收尾，再把这个联系人标记为不再自动回复。
        _reject_text = (result.get("last_message_text") or "").strip()
        _reject_sender = str(result.get("last_message_sender") or "").strip().lower()
        if _reject_sender in ("other", "对方") and is_reject_stop_other_text(_reject_text):
            _model_reply = (result.get("reply") or "").strip()
            # 只接受「短且像收尾」的模型回复；否则用兜底池，避免对方说别发了还继续推销
            _closing_ok = bool(_model_reply) and len(_model_reply) <= 40 and any(
                k in _model_reply for k in ("打扰", "好的", "明白", "收到", "理解", "抱歉", "祝")
            )
            reply = _model_reply if _closing_ok else fallback_reject_reply()
            _REJECTED_CONTACTS.add(contact_name)
            print(f"  [拒绝] {contact_name} 对方表示「{_reject_text[:20]}」，先发礼貌收尾再停止自动回复")
            result["should_reply"] = True
            result["last_message_sender"] = "other"
            result["last_message_text"] = _reject_text
            # 直接返回，跳过后面的回复清洗与去重（收尾语必须发出去）
            return _clean_reply(reply) or reply
        if maybe_handoff_for_latest_other_message(contact_name, result, chat_goal):
            return ""

        if result.get("should_reply") is not True:
            last_text = (result.get("last_message_text") or "").strip()
            sender = str(result.get("last_message_sender") or "").strip().lower()
            _local_sender = str(result.get("local_last_sender") or local_last_sender or "").strip().lower()
            _local_is_other = _local_sender in ("other", "对方")
            if result.get("ack_agreement") and sender in ("other", "对方"):
                # 上下文联动：对方是在答应我方上一句，顺着目标推进，而不是回一句「好」
                _model_reply = (result.get("reply") or "").strip()
                result["should_reply"] = True
                result["reply"] = _model_reply or fallback_agreement_reply(chat_goal)
                print("  [联动] 对方短肯定=同意，按目标顺势推进下一步")
            elif sender in ("other", "对方") and (result.get("reply") or "").strip():
                # 以模型写出来的内容为准：模型已经把要发的话写好了，
                # 就不能再用 should_reply=false 把它丢掉（实测模型偶发「写了话但判不回」）。
                result["should_reply"] = True
                print("  [AI] 按 reply 为准: 模型已给出内容，放行发送")
            elif sender in ("other", "对方") and positive_acceptance_needs_reply(last_text):
                result["should_reply"] = True
                result["reply"] = fallback_positive_acceptance_reply(last_text, chat_goal)
                print("  [AI] 兜底: 对方最后一句是积极确认，生成短回复")
            elif sender in ("other", "对方") and bare_message_needs_reply(last_text):
                # 对方只发了一个句号/单字/表情，模型判成「无需回复」= 漏回；
                # 这类消息实际是在等接话，这里强制接一句。
                result["should_reply"] = True
                result["reply"] = fallback_bare_message_reply(last_text, chat_goal)
                print(f"  [AI] 兜底: 对方只发了「{last_text[:8]}」，强制接话")
            elif _local_is_other and not (result.get("reply") or "").strip():
                # 本轮变化块落在对方一侧（本地硬信号），但模型没识别出对方内容、reply 为空：
                # 不能就此装死，用兜底接话补上，避免对方新消息被漏回。
                result["should_reply"] = True
                result["last_message_sender"] = "other"
                result["reply"] = fallback_bare_message_reply(last_text, chat_goal)
                print("  [AI] 本地判为对方新消息但模型未给出内容，兜底接话")

        if result.get("should_reply") is True:
            # 确定性保护：被动回复时末条是我方就不发，避免自己接自己的话。
            # 主动发送模式例外：那时末条本来就是我方，主动开口是预期行为。
            # 方向交叉：模型判我方但本地像素判对方一侧时，以本地气泡位置为准，不误拦。
            _me_vision = str(result.get("last_message_sender") or "").strip().lower() in ("me", "我方")
            _other_local = str(result.get("local_last_sender") or local_last_sender or "").strip().lower() in ("other", "对方")
            if (not allow_proactive) and _me_vision and not _other_local:
                print("  [AI] 跳过: 末条是我方消息，不自己接自己的话")
                return ""
            reply = (result.get("reply") or "").strip()
            if not reply:
                # 判定了要回但模型没给内容：若末条是对方的极短消息（句号/单字/表情），
                # 用兜底接话补上，否则这条消息又会变成「无需回复」被吞掉。
                _lt = (result.get("last_message_text") or "").strip()
                _snd = str(result.get("last_message_sender") or "").strip().lower()
                _snd_local = str(result.get("local_last_sender") or local_last_sender or "").strip().lower()
                if (_snd in ("other", "对方") or _snd_local in ("other", "对方")) and bare_message_needs_reply(_lt):
                    reply = fallback_bare_message_reply(_lt, chat_goal)
                    print(f"  [AI] 要回但内容为空，末条是对方「{_lt[:8]}」，用兜底接话")
            if reply:
                reply = _clean_reply(reply)
                reply = strip_model_commentary(reply)
                if not reply:
                    print("  [AI] 跳过: 回复只包含模型分析旁白")
                    return ""
                if contains_model_commentary(reply):
                    print("  [AI] 跳过: 回复仍包含模型分析旁白")
                    return ""
                reply = reduce_laugh_prefix(reply, recent_replies or [])
                last_text = (result.get("last_message_text") or "").strip()
                if should_push_offline_goal(last_text, reply, chat_goal, recent_replies):
                    print(f"  [AI] 策略兜底触发: 原回复「{reply[:40]}」被替换为邀约模板（last_text={last_text[:30]}）")
                    reply = fallback_offline_goal_reply(last_text, recent_replies)
                if asks_known_yes_answer_again(reply, last_text):
                    print("  [AI] 跳过: 回复重复追问了对方刚确认过的信息")
                    return ""
                contact_sent_history = sent_history_for_contact(contact_name)
                if reply in contact_sent_history:
                    print("  [AI] 跳过: 已发过相同内容")
                    return ""

                sender = result.get("last_message_sender", "unknown")
                print(f"  [AI] 回复: {reply[:50]}... [last_sender={sender}]")
                contact_sent_history.add(reply)
                return reply

        print(
            "  [AI] 无需回复判定: "
            f"last_sender={result.get('last_message_sender', 'unknown')} "
            f"last_text={(result.get('last_message_text') or '')[:40]}"
        )
        print("  [AI] 无需回复")
        return ""
    except Exception as e:
        print(f"  [AI] 保底截图路径错误: {e}")
        # 记录失败结果，监控循环据此区分「AI 判定无需回复」和「这次识别失败」，
        # 后者不落基准、下轮重试（详见 run_monitor 的 [AI] 识别失败 分支）。
        _LAST_AI_RESULT[contact_name] = {
            "should_reply": False,
            "reply": "",
            "last_message_sender": "unknown",
            "last_message_text": "",
            "error": str(e),
            "api_should_stop": False,
            "api_user_message": "",
        }
        return ""


def _esc_listener():
    """ESC 监听线程"""
    global STOP_FLAG
    try:
        import keyboard as kb
        kb.wait("esc")
        STOP_FLAG = True
        print("\n  [停止] 检测到 ESC，正在停止...")
    except Exception:
        pass


def sleep_after_full_contact_pass(processed_index: int, contact_count: int, interval: int) -> None:
    """Sleep only after every configured contact has been checked once."""
    if STOP_FLAG or contact_count <= 0 or processed_index % contact_count != 0:
        return
    wait = max(0, int(interval))
    if wait <= 0:
        return
    print(f"  [暂停] 已跑完全部联系人，等待 {wait} 秒后重新检查")
    for _ in range(wait):
        if STOP_FLAG:
            break
        time.sleep(1)


def run_monitor(interval: int = 15, max_cycles: int = 0) -> None:
    """
    多人轮询：加载 my_contacts.json 所有联系人，依次截图→AI分析→回复。
    """
    global STOP_FLAG, FATAL_EXIT_REASON, _goal_achieved_contacts
    STOP_FLAG = False
    FATAL_EXIT_REASON = ""
    _goal_achieved_contacts = set()

    # 自动日志：每次启动生成一个带时间戳的日志文件
    _init_log_file()

    # 上次没有转发成功的事件，启动时补传（家宽抖动 / 服务器重启）
    replay_pending_events()
    replay_pending_logs()
    log_relay_status()

    # 检查是否已有配置
    has_config = _check_config_files()

    if has_config:
        print("  [配置] 检测到已有配置文件，使用现有配置")
    else:
        print("  [配置] 配置文件不完整（缺少 my_contacts.json 或 chat_strategy_config.json）")
        print("  [配置] 请通过 runtime/ 下的 JSON 配置文件手动配置后重新运行")
        return

    contacts = load_contacts_info()
    if not contacts:
        print("  [错误] Word文档中没有联系人，请填写后重新运行")
        return

    # 轮询间隔优先取配置文件，命令行参数作兜底
    interval = _get_send_config()["poll_interval"] if interval <= 0 else interval

    contact_list = list(contacts.items())

    # 起始联系人：从名单里指定的位置开始轮询；未选 / 找不到则回退到第一个。
    # 只改起始下标，后续轮转、跳过、主动发送等逻辑保持不变。
    start_name = load_start_contact_name()
    idx = 0
    if start_name:
        for i, (cname, _) in enumerate(contact_list):
            if cname == start_name:
                idx = i
                break
        if start_name != contact_list[idx][0]:
            print(f"  [监控] 起始联系人「{start_name}」不在名单中，回退到第一个开始")

    print(f"\n  [监控] 账号记忆空间: {current_account_id()}")
    print(f"  [监控] {len(contact_list)} 个联系人 | 全部跑完后等待 {interval}s")
    print("  [监控] 按 ESC 停止\n")

    try:
        import keyboard
        t = threading.Thread(target=_esc_listener, daemon=True)
        t.start()
    except ImportError:
        print("  [警告] keyboard 模块未安装，无法使用 ESC 停止")

    # 获取微信窗口句柄
    hwnd = ensure_wechat_visible()
    if not hwnd:
        print("  [错误] 无法获取微信窗口")
        return

    cycle = 0
    _contact_digest: dict = {}
    _wait_reply: dict = {}
    _new_msg_since: dict[str, float] = {}
    _self_send_count: dict = {}
    _last_send_time: dict = {}
    _last_no_reply_time: dict = {}
    _conflict_streak: dict[str, int] = {}
    _conflict_last_fp: dict[str, str] = {}
    _recent_replies: dict[str, list[str]] = {}
    _first_seen_time: dict[str, float] = {}
    _proactive_count: dict[str, int] = {}

    # 读取主动发送配置
    _proactive_cfg = _get_proactive_config()
    _proactive_enabled = _proactive_cfg["proactive_enabled"]
    _proactive_interval = _proactive_cfg["proactive_interval_seconds"]
    _proactive_second_interval = _proactive_cfg["proactive_second_interval_seconds"]
    _proactive_max_attempts = _proactive_cfg["proactive_max_attempts"]
    _proactive_minutes = int(_proactive_interval / 60)
    _proactive_second_minutes = int(_proactive_second_interval / 60)
    if _proactive_enabled:
        print(f"  [主动] 主动发送已开启：首次 {_proactive_minutes} 分钟破冰，"
              f"第二次 {_proactive_second_minutes} 分钟追问，最多 {_proactive_max_attempts} 次后转被动")
    else:
        print(f"  [主动] 主动发送已关闭，仅被动回复")

    _delay_cfg = _get_reply_delay_config()
    _reply_delay = int(_delay_cfg["min_reply_delay_seconds"])
    if _reply_delay > 0:
        print(f"  [节奏] 收到新消息后延迟 {_reply_delay} 秒再回复（模拟真人不秒回）")

    while not STOP_FLAG:
        if max_cycles > 0 and cycle >= max_cycles:
            break
        cycle += 1
        cname, cinfo = contact_list[idx % len(contact_list)]
        idx += 1

        # 跳过已达成聊天目标的联系人，继续检查其他联系人
        if _goal_achieved_contacts:
            remaining = len(contact_list) - len(_goal_achieved_contacts)
            if remaining <= 0:
                print(f"\n  [达成] 所有联系人均已达成聊天目标，监控结束")
                break
            if cname in _goal_achieved_contacts:
                print(f"  [跳过] {cname} 已达成聊天目标，继续检查下一个联系人")
                sleep_after_full_contact_pass(idx, len(contact_list), max(1, int(interval)))
                continue

        # 对方已明确拒绝继续沟通（已发过礼貌收尾）→ 不再自动回复他
        if cname in _REJECTED_CONTACTS:
            print(f"  [跳过] {cname} 对方已表示不想再收到消息，本轮不再自动回复")
            sleep_after_full_contact_pass(idx, len(contact_list), max(1, int(interval)))
            continue

        print(f"\n  === 第{cycle}轮 / {cname} ===")

        # 等待对方回复机制 + 主动发送
        unlock_at = _wait_reply.get(cname, 0)
        last_send = _last_send_time.get(cname, 0)
        first_seen = _first_seen_time.get(cname, 0)
        proactive_count = _proactive_count.get(cname, 0)
        is_proactive_ready = _is_proactive_ready(
            _proactive_enabled, last_send, first_seen, time.time(),
            _proactive_interval, proactive_count,
            _proactive_second_interval, _proactive_max_attempts,
        )
        waiting_for_reply = unlock_at > time.time() and not is_proactive_ready
        wait_remaining = int(unlock_at - time.time()) if waiting_for_reply else 0
        if _proactive_enabled and _proactive_max_attempts > 0 and proactive_count >= _proactive_max_attempts:
            print(f"  [主动] {cname} 已主动 {proactive_count} 次未获回复，转被动，不再主动发")
        elif is_proactive_ready and unlock_at > time.time():
            print(f"  [主动] {cname} 已{_proactive_minutes}分钟没联系，跳过等待期")
        elif is_proactive_ready:
            print(f"  [主动] {cname} 已{_proactive_minutes}分钟没联系，允许主动发消息（第{proactive_count + 1}次）")

        # 刷新句柄
        if not activate_window(hwnd):
            print("  [重试] 重新获取窗口句柄...")
            hwnd = ensure_wechat_visible()
            if not hwnd or not activate_window(hwnd):
                time.sleep(interval)
                continue
        time.sleep(0.3)
        search_contact(cname)
        time.sleep(1.5)

        shot = capture_chat_message_area(hwnd)
        if not shot:
            sleep_after_full_contact_pass(idx, len(contact_list), interval)
            continue

        cur = screenshot_digest(shot)
        last_seen = _contact_digest.get(cname, "")

        if not last_seen:
            save_debug_screenshot(shot, cname, cycle, "baseline")
            _contact_digest[cname] = cur
            _first_seen_time[cname] = time.time()
            print(f"  [首次] {cname} 建立截图基准，并检查最后一条消息")

            CHAT_SHOT_DIR.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            shot_path = CHAT_SHOT_DIR / f"wechat_chat_initial_{ts}.png"
            shot.save(shot_path)
            print(f"  [保存] {shot_path.name}")

            # 首轮不再挂「不秒回」闸门：直接按最后一条消息决定是否回复。
            # （节奏延迟只作用于监控期间新到达的消息。）

            recent_for_contact = _merge_recent_replies(
                recent_replies_from_memory(cname), _recent_replies.get(cname, [])
            )
            # 主动模式下，首次接触允许发送招呼语破冰
            _first_allow_proactive = _proactive_enabled
            reply = analyze_screenshot_via_memory(
                shot, cname,
                gender=cinfo.get("gender", "未知"),
                chat_goal=cinfo.get("chat_goal", ""),
                allow_proactive=_first_allow_proactive,
                recent_replies=recent_for_contact,
            )
            if reply:
                sent_count = _send_split_messages(reply, hwnd)
                shot2 = capture_chat_message_area(hwnd)
                digest2 = screenshot_digest(shot2) if shot2 else None
                send_failed = (
                    sent_count <= 0
                    or digest2 is None
                    or not screenshot_changed(cur, digest2)
                )
                if send_failed:
                    print(f"  [发送] {cname} 破冰回复未实际发出（画面无新增气泡，条数={sent_count}），丢弃本轮基准，下轮重新分析")
                    _contact_digest.pop(cname, None)
                    _first_seen_time.pop(cname, None)
                    sleep_after_full_contact_pass(idx, len(contact_list), interval)
                    continue
                append_sent_reply_to_memory(cname, reply)
                _recent_replies.setdefault(cname, []).append(reply)
                _recent_replies[cname] = _recent_replies[cname][-5:]
                print(f"  [完成] 已回复 {cname}")
                _contact_digest[cname] = digest2
                _self_send_count[cname] = 1
                wait_sec = max(1, int(interval))
                _wait_reply[cname] = time.time() + wait_sec
                _last_send_time[cname] = time.time()
            else:
                print(f"  [首次] {cname} 无需回复")
                last_ai = _LAST_AI_RESULT.get(cname, {})
                ai_error = str(last_ai.get("error") or "").strip()
                if ai_error:
                    # 首次分析就失败：基准已经在上面写入，里面含着这条没处理的消息，
                    # 不丢基准的话下一轮开始就永远「无变化」。这里主动丢掉基准，
                    # 下一轮重新走「建立基准 → 分析」，避免消息被一次接口抖动吞掉。
                    _contact_digest.pop(cname, None)
                    _first_seen_time.pop(cname, None)
                    _last_no_reply_time.pop(cname, None)
                    print(f"  [AI] 识别失败，丢弃本轮基准，下一轮重新分析: {ai_error[:120]}")
                    sleep_after_full_contact_pass(idx, len(contact_list), interval)
                    continue
                last_sender = str(
                    last_ai.get("local_last_sender") or last_ai.get("last_message_sender") or ""
                ).strip().lower()
                if last_sender in ("me", "我方"):
                    _last_no_reply_time.pop(cname, None)
                    print(f"  [首次] {cname} 最后一条是我方消息，不进入无需回复冷却")
                else:
                    _last_no_reply_time[cname] = time.time()
            sleep_after_full_contact_pass(idx, len(contact_list), interval)
            continue

        change_info = digest_change_info(cur, last_seen)
        print(f"  [检测] {describe_change_info(change_info)}")
        changed = change_info["changed"]
        # 方向交叉信号：优先用本轮最大变化块的落点（左/右）判断新消息来自哪侧；
        # 变化块缺失（整版位移/强变/首次）时回退气泡像素扫描。仅作交叉验证，不替代 AI 判定。
        _change_box = change_info.get("cluster_box")
        _local_sender = change_cluster_side(_change_box, getattr(shot, "width", 0))
        _local_band: Optional[dict] = None
        if _local_sender == "unknown":
            try:
                _local_sender, _local_band = detect_last_message_sender_locally(shot)
            except Exception:
                _local_sender, _local_band = "unknown", None
        elif _change_box:
            _local_band = {
                "top": _change_box[0], "bottom": _change_box[1],
                "median_x": (_change_box[2] + _change_box[3]) / 2,
            }
        # 节奏闸门状态：延迟期内画面稳定也要在到点时放行，否则消息会被
        # 「无新消息，跳过」分支永久吞掉。
        delay_pending = _new_msg_since.get(cname, 0)
        delay_elapsed = bool(delay_pending) and (time.time() - delay_pending) >= _reply_delay

        if not changed and not delay_elapsed:
            if delay_pending:
                remain = int(_reply_delay - (time.time() - delay_pending))
                print(f"  [节奏] {cname} 画面稳定，延迟等待还需 {max(0, remain)} 秒")
                sleep_after_full_contact_pass(idx, len(contact_list), interval)
                continue
            if waiting_for_reply:
                save_debug_screenshot(shot, cname, cycle, "waiting_nochange")
                print(f"  [等待] 等待对方回复中，还剩{wait_remaining}秒；当前截图无变化")
                sleep_after_full_contact_pass(idx, len(contact_list), interval)
                continue
            elif is_proactive_ready:
                print(f"  [主动] {cname} 无新消息但已{_proactive_minutes}分钟，允许主动发")
            else:
                save_debug_screenshot(shot, cname, cycle, "nochange")
                print(f"  [检测] {cname} 无新消息，跳过")
                sleep_after_full_contact_pass(idx, len(contact_list), interval)
                continue

        if changed:
            save_debug_screenshot(shot, cname, cycle, "changed")
            print(f"  [检测] {cname} 有新消息变化")
        else:
            print(f"  [节奏] {cname} 延迟等待已满，按最后一条对方消息继续")

        # 节奏闸门：策略要求「对方最后一条回复 N 秒钟后方可回复」，模型无法感知
        # 时间，由循环强制等待。
        # 注意：计时从第一次检测到新内容开始，后续同批新消息（画面继续变化）
        # 不会重置计时器。若每轮变化都重置，对方持续发消息则计时器永远到不了
        # 终点，AI 永远没机会分析——20260914_140500 日志第 3–4 轮实证。
        # 主动发送就绪时不占用闸门，到点即发。
        if _reply_delay > 0 and not is_proactive_ready:
            if not _new_msg_since.get(cname):
                _new_msg_since[cname] = time.time()
            elapsed = time.time() - _new_msg_since[cname]
            if elapsed < _reply_delay:
                remain = int(_reply_delay - elapsed)
                print(f"  [节奏] {cname} 最新消息 {int(elapsed)} 秒前到达，还需等 {remain} 秒再回复（模拟真人节奏）")
                sleep_after_full_contact_pass(idx, len(contact_list), interval)
                continue
            _new_msg_since.pop(cname, None)

        # 冷却：上次判定无需回复后，在本轮间隔内不重复调 AI。
        last_no_reply = _last_no_reply_time.get(cname, 0)
        no_reply_cooldown = max(1, int(interval))
        if not is_proactive_ready and last_no_reply > 0 and time.time() - last_no_reply < no_reply_cooldown:
            remaining = int(no_reply_cooldown - (time.time() - last_no_reply))
            print(f"  [冷却] {cname} 刚判定无需回复，剩余{remaining}秒冷却；保留变化待下轮复查")
            sleep_after_full_contact_pass(idx, len(contact_list), interval)
            continue

        # 保存截图
        CHAT_SHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot_path = CHAT_SHOT_DIR / f"wechat_chat_{ts}.png"
        shot.save(shot_path)
        print(f"  [保存] {shot_path.name}")

        recent_for_contact = _merge_recent_replies(
            recent_replies_from_memory(cname), _recent_replies.get(cname, [])
        )
        reply = analyze_screenshot_via_memory(
            shot, cname,
            gender=cinfo.get("gender", "未知"),
            chat_goal=cinfo.get("chat_goal", ""),
            allow_proactive=is_proactive_ready,
            recent_replies=recent_for_contact,
            local_last_sender=_local_sender,
            local_last_band=_local_band,
        )
        if reply:
            sent_count = _send_split_messages(reply, hwnd)
            # 发送后抓帧校验：消息真的变成右侧气泡，画面指纹才会变。若发送前后画面
            # 零变化（或发送条数为 0 / 抓帧失败），说明键鼠没把消息送进去——输入框失焦、
            # 粘贴失败、回车被输入法上屏拦截、发送间隙窗口被打断都可能。这种情况绝不能
            # 写记忆、落基准，否则下轮对比「无变化」直接跳过，把对方这条新消息永久吞掉；
            # 保留发送前基准，下轮重新分析。
            shot2 = capture_chat_message_area(hwnd)
            digest2 = screenshot_digest(shot2) if shot2 else None
            send_failed = (
                sent_count <= 0
                or digest2 is None
                or not screenshot_changed(cur, digest2)
            )
            if send_failed:
                print(f"  [发送] {cname} 回复未实际发出（发送前后画面无新增气泡，条数={sent_count}），保留基准，下轮重新分析")
                _new_msg_since.pop(cname, None)
                sleep_after_full_contact_pass(idx, len(contact_list), interval)
                continue
            append_sent_reply_to_memory(cname, reply)
            _recent_replies.setdefault(cname, []).append(reply)
            _recent_replies[cname] = _recent_replies[cname][-5:]
            print(f"  [完成] 已回复 {cname}")
            # 发送后更新摘要基准
            _contact_digest[cname] = digest2
            count = _self_send_count.get(cname, 0) + 1
            _self_send_count[cname] = count
            # 主动/被动计数：对方发了新消息(changed) → 被动回复，主动次数清零重新计数；
            # 无新消息、到点主动发 → 主动次数 +1，达上限后转被动。
            if changed:
                _proactive_count[cname] = 0
            elif is_proactive_ready:
                _proactive_count[cname] = _proactive_count.get(cname, 0) + 1
                print(f"  [主动] 本次为主动发送，累计第 {_proactive_count[cname]} 次")
            wait_sec = max(1, int(interval))
            _wait_reply[cname] = time.time() + wait_sec
            _last_send_time[cname] = time.time()
            _new_msg_since.pop(cname, None)
            print(f"  [等待] 已回复，{wait_sec} 秒内等待对方新消息")
        else:
            print(f"  [完成] {cname} 无需回复")
            last_ai = _LAST_AI_RESULT.get(cname, {})
            ai_error = str(last_ai.get("error") or "").strip()
            if ai_error:
                # 接口超时/5xx/JSON 解析失败 ≠「对方不需要回复」。这种失败绝不能
                # 计入熔断或落基准，否则一条真实消息会被一次接口抖动永久吞掉
                # （20261107 反馈的「对面发消息但不回复」另一条成因）。
                _conflict_streak.pop(cname, None)
                _conflict_last_fp.pop(cname, None)
                _new_msg_since.pop(cname, None)
                _last_no_reply_time.pop(cname, None)
                _self_send_count.pop(cname, None)
                print(f"  [AI] 识别失败，本轮不落基准，下轮重试: {ai_error[:120]}")
                sleep_after_full_contact_pass(idx, len(contact_list), interval)
                continue
            local_sender = str(last_ai.get("local_last_sender") or "").strip().lower()
            vision_sender = str(last_ai.get("last_message_sender") or "").strip().lower()
            # 熔断器：连续多轮同一画面下本地=other 与视觉判定冲突（或均判无需回复
            # 但不写基准）会无限复查烧 API。画面摘要（抗抖）不变即视为同一内容，
            # 达到阈值后强制写基准落定；若真有新增消息，抗抖摘要会再次变化并重新触发。
            prev_conflict_fp = _conflict_last_fp.get(cname, "")
            if screenshot_changed(cur, prev_conflict_fp):
                _conflict_streak[cname] = 1
                _conflict_last_fp[cname] = cur
            else:
                _conflict_streak[cname] = _conflict_streak.get(cname, 0) + 1
            streak = _conflict_streak.get(cname, 1)
            if local_sender in ("me", "我方") and vision_sender in ("me", "我方"):
                _contact_digest[cname] = cur
                _conflict_streak.pop(cname, None)
                _conflict_last_fp.pop(cname, None)
                _new_msg_since.pop(cname, None)
                print(f"  [检测] {cname} 最后一条是我方消息，已写入基准，避免反复检测自己的回复")
                _last_no_reply_time.pop(cname, None)
            elif streak >= CONFLICT_COMMIT_THRESHOLD:
                _contact_digest[cname] = cur
                _conflict_streak.pop(cname, None)
                _conflict_last_fp.pop(cname, None)
                _new_msg_since.pop(cname, None)
                print(
                    f"  [熔断] {cname} 同一画面连续 {streak} 轮复查仍判定无需回复"
                    f"（本地={local_sender or '?'} 视觉={vision_sender or '?'}），"
                    f"强制写入基准落定；若对方有新消息将按摘要变化重新检测"
                )
                _last_no_reply_time[cname] = time.time()
            else:
                print(f"  [检测] {cname} 本次变化未写入基准，下轮继续复查（第 {streak}/{CONFLICT_COMMIT_THRESHOLD} 轮）")
                _last_no_reply_time[cname] = time.time()
            _self_send_count.pop(cname, None)
        sleep_after_full_contact_pass(idx, len(contact_list), interval)

    print(f"\n  [结束] 共 {cycle} 轮")
    if _goal_achieved_contacts:
        achieved_list = ", ".join(sorted(_goal_achieved_contacts))
        print(f"  [达成] 以下联系人已达成聊天目标: {achieved_list}")
    if FATAL_EXIT_REASON:
        print(f"  [结束原因] {FATAL_EXIT_REASON}")


def main() -> None:
    """入口：解析参数，启动监控或单次模式。"""
    try:
        _main_impl()
    except Exception as e:
        print(f"\n  [异常] 程序崩溃: {e}")
        import traceback
        traceback.print_exc()
    finally:
        # 以 pythonw.exe 启动时没有控制台、stdin 已关闭，input() 会抛 EOFError；
        # 仅在存在可读终端时才等待回车，否则直接退出，避免二次崩溃。
        try:
            import sys as _sys
            if _sys.stdin is not None and _sys.stdin.isatty():
                print()
                input("  按 Enter 键退出...")
        except (EOFError, OSError, ValueError):
            pass


def _main_impl() -> None:
    """main() 的实现体，异常由外层 main() 统一捕获并暂停。"""
    global ACTIVE_ACCOUNT_ID, ACTIVE_AGENT_NAME, ACTIVE_SKILL_ID
    import argparse
    parser = argparse.ArgumentParser(description="微信视觉 RPA — 无坐标模式")
    parser.add_argument("--once", type=str, default="", help="单次模式：联系人名称")
    parser.add_argument("--test-vision", type=str, default="", help="仅测试AI分析（不发送）")
    parser.add_argument("--scan", action="store_true", help="扫描微信窗口信息")
    parser.add_argument("--account-id", type=str, default="", help="当前微信账号的记忆空间ID，用于隔离同名联系人历史")
    parser.add_argument("--agent-name", type=str, default="", help="当前员工（技能）名，用于记忆与事件按员工隔离")
    parser.add_argument("--skill-id", type=str, default="", help="当前员工的服务器技能ID，用于手机端按id归属匹配")
    parser.add_argument("-i", "--interval", type=int, default=15, help="全部联系人跑完后的等待秒数")
    parser.add_argument("--max-cycles", type=int, default=0, help="最大循环次数")
    args = parser.parse_args()

    _setup_console_utf8()
    _enable_dpi_awareness()
    if args.account_id.strip():
        ACTIVE_ACCOUNT_ID = args.account_id.strip()
    if args.agent_name.strip():
        ACTIVE_AGENT_NAME = args.agent_name.strip()
    if getattr(args, "skill_id", "").strip():
        ACTIVE_SKILL_ID = args.skill_id.strip()
    print(f"  [记忆] 当前账号记忆空间: {current_account_id()} | 当前员工: {current_agent_name()}")

    if args.scan:
        hwnds = _find_wechat_hwnds()
        if not hwnds:
            print("未找到微信窗口")
            return
        import win32gui
        import win32process
        import psutil
        print(f"找到 {len(hwnds)} 个微信窗口：")
        for h in hwnds:
            title = win32gui.GetWindowText(h)
            rect = win32gui.GetWindowRect(h)
            _, pid = win32process.GetWindowThreadProcessId(h)
            try:
                proc = psutil.Process(pid)
                exe = proc.exe()
            except Exception:
                exe = "?"
            print(f"  HWND={h}  title={title}  rect={rect}  PID={pid}  exe={exe}")
        return

    if args.once or args.test_vision:
        name = args.once or args.test_vision
        hwnd = ensure_wechat_visible()
        if not hwnd:
            return
        activate_window(hwnd)
        time.sleep(0.3)
        search_contact(name)
        time.sleep(1.5)
        shot = capture_chat_message_area(hwnd)
        if not shot:
            return
        CHAT_SHOT_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.now().strftime("%Y%m%d_%H%M%S")
        shot.save(CHAT_SHOT_DIR / f"wechat_chat_{ts}.png")
        if args.once:
            recent_for_contact = recent_replies_from_memory(name)
            reply = analyze_screenshot_via_memory(
                shot,
                name,
                recent_replies=recent_for_contact,
            )
            if reply:
                _send_split_messages(reply, hwnd)
                append_sent_reply_to_memory(name, reply)
                print(f"  [完成] 已回复 {name}")
        else:
            print("  [测试] AI分析结果：")
            contacts = load_contacts_info()
            cinfo = contacts.get(name, {})
            recent_for_contact = recent_replies_from_memory(name)
            reply = analyze_screenshot_via_memory(
                shot, name,
                gender=cinfo.get("gender", "未知"),
                chat_goal=cinfo.get("chat_goal", ""),
                recent_replies=recent_for_contact,
            )
            print(f"  [测试] should_reply={'是' if reply else '否'}")
            if reply:
                print(f"  [测试] 回复内容: {reply}")
        return

    # 默认：监控模式
    run_monitor(interval=args.interval, max_cycles=args.max_cycles)


if __name__ == "__main__":
    main()
