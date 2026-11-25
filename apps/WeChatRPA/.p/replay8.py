# -*- coding: utf-8 -*-
"""回放：新量化桶(rel//8) 下，对每一对连续同联系人帧判定 changed，
与日志里的实际结论（真消息到达 / 同内容空转）对比。"""
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from PIL import Image  # noqa: E402

import wechat_vision_rpa as m  # noqa: E402

DEBUG = Path("runtime/screenshots/debug")

# (前帧, 后帧, 期望 changed, 说明) —— 期望值取自 log_20261107_140824.txt 与
# log_20261107_120542.txt 的人工核对结果
CASES = [
    ("wechat_chat_cycle0003_墨_nochange_20261107_140917.png",
     "wechat_chat_cycle0005_墨_nochange_20261107_140950.png", True,
     "墨 买房+500w 到达（旧代码漏判，用户投诉的那条）"),
    ("wechat_chat_cycle0005_墨_nochange_20261107_140950.png",
     "wechat_chat_cycle0007_墨_nochange_20261107_141027.png", False, "墨 同内容空转"),
    ("wechat_chat_cycle0007_墨_nochange_20261107_141027.png",
     "wechat_chat_cycle0009_墨_nochange_20261107_141101.png", False, "墨 同内容空转"),
    ("wechat_chat_cycle0009_墨_nochange_20261107_141101.png",
     "wechat_chat_cycle0011_墨_nochange_20261107_141134.png", False, "墨 同内容空转"),
    ("wechat_chat_cycle0001_墨_baseline_20261107_120548.png",
     "wechat_chat_cycle0003_墨_changed_20261107_120629.png", True, "墨 真消息(12:06)"),
    ("wechat_chat_cycle0003_墨_changed_20261107_120629.png",
     "wechat_chat_cycle0003_墨_confirmed_20261107_120642.png", False, "墨 复查后稳定"),
    ("wechat_chat_cycle0001_墨_baseline_20261107_115941.png",
     "wechat_chat_cycle0002_墨_nochange_20261107_120012.png", False, "墨 同内容(12:00)"),
    ("wechat_chat_cycle0002_墨_nochange_20261107_120012.png",
     "wechat_chat_cycle0003_墨_changed_20261107_120032.png", True, "墨 真消息(12:00)"),
    ("wechat_chat_cycle0004_水蜜桃_changed_20261107_140922.png",
     "wechat_chat_cycle0004_水蜜桃_confirmed_20261107_140930.png", None, "水蜜桃 复查帧(允许变化)"),
    ("wechat_chat_cycle0002_水蜜桃_baseline_20261107_140848.png",
     "wechat_chat_cycle0004_水蜜桃_changed_20261107_140922.png", None, "水蜜桃 右缘闪烁(现状即 changed)"),
    ("wechat_chat_cycle0010_水蜜桃_changed_20261107_141106.png",
     "wechat_chat_cycle0012_水蜜桃_changed_20261107_141140.png", True, "水蜜桃 真消息(7.96%)"),
    ("wechat_chat_cycle0001_墨_baseline_20261107_115452.png",
     "wechat_chat_cycle0002_墨_nochange_20261107_115522.png", False, "墨 同内容(11:55)"),
]


def main():
    fails = 0
    print(f"{'样本':44s} {'changed':>8s} {'期望':>6s}  细节")
    print("-" * 110)
    for a, b, want, note in CASES:
        pa, pb = DEBUG / a, DEBUG / b
        if not pa.exists() or not pb.exists():
            print(f"{note:44s} 缺文件")
            continue
        da = m.screenshot_digest(Image.open(pa))
        db = m.screenshot_digest(Image.open(pb))
        info = m.digest_change_info(db, da)
        got = info["changed"]
        if want is None:
            mark = "  "
            ok = True
        else:
            ok = got == want
            mark = "OK" if ok else "FAIL"
        if not ok:
            fails += 1
        print(f"{note:44s} {str(got):>8s} {str(want):>6s}  [{mark}] "
              f"{m.describe_change_info(info)}")
    print(f"\n不符 {fails} 处")
    return 1 if fails else 0


if __name__ == "__main__":
    sys.exit(main())
