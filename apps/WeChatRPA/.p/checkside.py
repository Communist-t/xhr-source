# -*- coding: utf-8 -*-
"""确认那两处 FAIL 的差异出现在右侧（我方刚发送的气泡），而非左侧对方气泡。"""
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runtime"))
from PIL import Image  # noqa: E402

import wechat_vision_rpa as m  # noqa: E402

DEBUG = Path("runtime/screenshots/debug")

PAIRS = [
    ("115941→120012 (日志称 0.00%)",
     "wechat_chat_cycle0001_墨_baseline_20261107_115941.png",
     "wechat_chat_cycle0002_墨_nochange_20261107_120012.png"),
    ("115452→115522 (日志称 0.00%)",
     "wechat_chat_cycle0001_墨_baseline_20261107_115452.png",
     "wechat_chat_cycle0002_墨_nochange_20261107_115522.png"),
]

for label, a, b in PAIRS:
    ia, ib = Image.open(DEBUG / a), Image.open(DEBUG / b)
    da, db = m.screenshot_digest(ia), m.screenshot_digest(ib)
    ca, cb = m._parse_digest(da), m._parse_digest(db)
    cols = ca[0]
    width_px = cols * 8
    left = right = 0
    rows_by_side = {"left": set(), "right": set()}
    for i, (x, y) in enumerate(zip(ca[2], cb[2])):
        if x != y:
            r, c = divmod(i, cols)
            if c * 8 < width_px * 0.45:
                left += 1
                rows_by_side["left"].add(r * 8)
            else:
                right += 1
                rows_by_side["right"].add(r * 8)
    print(f"\n{label}")
    print(f"  差异格: 左侧(对方区)={left}  右侧(我方区)={right}")
    print(f"  右侧变化行 y 范围: {min(rows_by_side['right']) if rows_by_side['right'] else '-'}"
          f" - {max(rows_by_side['right']) if rows_by_side['right'] else '-'}")
    print(f"  说明: baseline 是发送前截的，程序实际基准是发送后的 shot2（未存盘），"
          f"\n        所以这里的右侧差异 = 我们自己刚发出去的气泡，属正常。")
