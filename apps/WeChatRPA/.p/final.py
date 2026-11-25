# -*- coding: utf-8 -*-
"""变化检测最终回归：真实帧对 + 合成用例 + 配置覆盖。

真实帧对的期望值取自日志人工核对（不是旧代码的输出）。注意：程序发送后会用
shot2 重设基准，而 baseline 调试图是发送前截的，因此「刚发过言」的帧对不能
直接拿 baseline 比 —— 这类用例改用 confirmed/changed 等同阶段帧。
"""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from PIL import Image  # noqa: E402

import wechat_vision_rpa as m  # noqa: E402

DEBUG = Path("runtime/screenshots/debug")
fails = []


def ck(label, got, want=True):
    ok = got == want
    print(f"  {'OK  ' if ok else 'FAIL'} {label}: got={got} want={want}")
    if not ok:
        fails.append(label)


def verdict(a, b):
    return m.digest_change_info(m.screenshot_digest(Image.open(DEBUG / b)),
                                m.screenshot_digest(Image.open(DEBUG / a)))


print("### 1. 真实帧对（同阶段、无发送干扰）")
REAL_PAIRS = [
    ("墨 买房+500w 到达 140917→140950",
     "wechat_chat_cycle0003_墨_nochange_20261107_140917.png",
     "wechat_chat_cycle0005_墨_nochange_20261107_140950.png", True),
    ("墨 同内容 140950→141027",
     "wechat_chat_cycle0005_墨_nochange_20261107_140950.png",
     "wechat_chat_cycle0007_墨_nochange_20261107_141027.png", False),
    ("墨 同内容 141027→141101",
     "wechat_chat_cycle0007_墨_nochange_20261107_141027.png",
     "wechat_chat_cycle0009_墨_nochange_20261107_141101.png", False),
    ("墨 同内容 141101→141134（用户投诉的那轮）",
     "wechat_chat_cycle0009_墨_nochange_20261107_141101.png",
     "wechat_chat_cycle0011_墨_nochange_20261107_141134.png", False),
    ("墨 复查后稳定 120629→120642",
     "wechat_chat_cycle0003_墨_changed_20261107_120629.png",
     "wechat_chat_cycle0003_墨_confirmed_20261107_120642.png", False),
    ("墨 真消息 120548→120629",
     "wechat_chat_cycle0001_墨_baseline_20261107_120548.png",
     "wechat_chat_cycle0003_墨_changed_20261107_120629.png", True),
    ("水蜜桃 真消息 141106→141140",
     "wechat_chat_cycle0010_水蜜桃_changed_20261107_141106.png",
     "wechat_chat_cycle0012_水蜜桃_changed_20261107_141140.png", True),
]
for label, a, b, want in REAL_PAIRS:
    if not (DEBUG / a).exists() or not (DEBUG / b).exists():
        print(f"  SKIP {label}（截图已被程序清理）")
        continue
    info = verdict(a, b)
    print(f"  {'OK  ' if info['changed'] == want else 'FAIL'} {label}: "
          f"changed={info['changed']} want={want} :: {m.describe_change_info(info)}")
    if info["changed"] != want:
        fails.append(label)

print("\n### 2. 合成用例（簇门槛/边界）")
cols, rows = 151, 97


def dg(idx_iterable):
    cells = bytearray(cols * rows)
    for i in idx_iterable:
        cells[i] = 5
    return f"{cols}x{rows}:" + bytes(cells).hex()


def block(x0, y0, w, h):
    return [r * cols + c for r in range(y0, y0 + h) for c in range(x0, x0 + w)]


base = dg([])
ck("2列x3行边缘竖条 = 噪声", m.screenshot_changed(dg(block(0, 20, 2, 3)), base), False)
ck("30列x1行细线 = 噪声", m.screenshot_changed(dg(block(10, 30, 30, 1)), base), False)
ck("4列x4行单字气泡 = 新消息", m.screenshot_changed(dg(block(4, 26, 4, 4)), base), True)
ck("11列x3行短消息 = 新消息", m.screenshot_changed(dg(block(6, 46, 11, 3)), base), True)
ck("28格(实测买房块) = 新消息", m.screenshot_changed(dg(block(5, 43, 7, 4)), base), True)
ck("整版位移60% = 新消息", m.screenshot_changed(dg(range(0, cols * rows, 2)), base), True)
ck("网格尺寸变化 = 视为变化",
   m.screenshot_changed(f"{cols+10}x{rows}:" + "00" * ((cols + 10) * rows), base), True)
ck("legacy 指纹 = 视为变化", m.screenshot_changed("legacy:abc", base), True)
ck("完全相同 = 无变化", m.screenshot_changed(base, base), False)

print("\n### 3. 配置覆盖仍生效")
tmp = Path(tempfile.mkdtemp())


def with_cfg(dedicated, user_cfg, fn):
    od, ou = m.CHANGE_DETECT_CONFIG_PATH, m.USER_CONFIG_PATH
    m.CHANGE_DETECT_CONFIG_PATH = tmp / "cd.json"
    m.USER_CONFIG_PATH = str(tmp / "uc.json")
    for p, v in ((m.CHANGE_DETECT_CONFIG_PATH, dedicated), (Path(m.USER_CONFIG_PATH), user_cfg)):
        if v is not None:
            p.write_text(json.dumps(v), encoding="utf-8")
        elif p.exists():
            p.unlink()
    try:
        return fn()
    finally:
        m.CHANGE_DETECT_CONFIG_PATH, m.USER_CONFIG_PATH = od, ou


small = dg(block(4, 26, 4, 2))  # 8 格，默认门槛下算噪声
ck("默认：8 格 = 噪声", with_cfg(None, {"lobster": {}},
                                lambda: m.screenshot_changed(small, base)), False)
ck("cluster_min_cells=8 → 变新消息",
   with_cfg({"cluster_min_cells": 8}, {"lobster": {}},
            lambda: m.screenshot_changed(small, base)), True)
ck("enabled=false → 只看整幅占比",
   with_cfg({"enabled": False}, {}, lambda: m.screenshot_changed(small, base)), False)

print("\n### 4. 之前的修复未被破坏")
src = Path("runtime/wechat_vision_rpa.py").read_text(encoding="utf-8")
ck("拆句不按逗号", "re.split(r'[。！？…]+', reply)" in src)
ck("识别失败不落基准", "识别失败，本轮不落基准" in src)
ck("记忆落盘/读回", src.count("append_sent_reply_to_memory(cname, reply)") >= 2
   and src.count("recent_replies_from_memory(") - 1 >= 4)
ck("复查默认 3 秒", m.DEFAULT_CHANGE_CONFIRM_CONFIG["delay_seconds"] == 3)
ck("节奏闸门默认 0", m._get_reply_delay_config()["min_reply_delay_seconds"] == 0)

print(f"\n结果: {'全部通过' if not fails else str(len(fails)) + ' 处不符 -> ' + ', '.join(fails)}")
sys.exit(1 if fails else 0)
