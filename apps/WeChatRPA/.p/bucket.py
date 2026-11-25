# -*- coding: utf-8 -*-
"""选量化桶宽：在不同 bucket 下，真实消息 vs 噪声各自的差异格数 / 最大簇。"""
from collections import Counter
from pathlib import Path

from PIL import Image

DEBUG = Path("runtime/screenshots/debug")


def rel_of(img):
    g = img.convert("L")
    w, h = g.size
    top = min(16, max(0, h // 3))
    right = max(0, w - 20)
    small = g.crop((0, top, right, h)).resize(
        (max(1, right // 8), max(1, (h - top) // 8)), Image.BOX)
    data = small.tobytes()
    bg = Counter(data).most_common(1)[0][0]
    denom = bg if bg >= 40 else 200
    return small.size, bytes(min(255, abs(v - bg) * 255 // denom) for v in data)


def cluster(mask, cols):
    seen, best = set(), 0
    for s in mask:
        if s in seen:
            continue
        stack = [s]
        seen.add(s)
        n = 0
        while stack:
            idx = stack.pop()
            n += 1
            r, c = divmod(idx, cols)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nr, nc = r + dr, c + dc
                    nb = nr * cols + nc
                    if 0 <= nr and 0 <= nc and nb in mask and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        best = max(best, n)
    return best


REAL = [
    ("墨 买房+500w", "wechat_chat_cycle0003_墨_nochange_20261107_140917.png",
     "wechat_chat_cycle0005_墨_nochange_20261107_140950.png"),
    ("墨 120629", "wechat_chat_cycle0001_墨_baseline_20261107_120548.png",
     "wechat_chat_cycle0003_墨_changed_20261107_120629.png"),
    ("墨 120740", "wechat_chat_cycle0003_墨_confirmed_20261107_120642.png",
     "wechat_chat_cycle0005_墨_changed_20261107_120740.png"),
    ("墨 120921", "wechat_chat_cycle0005_墨_confirmed_20261107_120753.png",
     "wechat_chat_cycle0009_墨_changed_20261107_120921.png"),
    ("水蜜桃 141140", "wechat_chat_cycle0010_水蜜桃_changed_20261107_141106.png",
     "wechat_chat_cycle0012_水蜜桃_changed_20261107_141140.png"),
]
NOISE = [
    ("墨 同内容 950→027", "wechat_chat_cycle0005_墨_nochange_20261107_140950.png",
     "wechat_chat_cycle0007_墨_nochange_20261107_141027.png"),
    ("墨 同内容 027→101", "wechat_chat_cycle0007_墨_nochange_20261107_141027.png",
     "wechat_chat_cycle0009_墨_nochange_20261107_141101.png"),
    ("墨 同内容 101→134", "wechat_chat_cycle0009_墨_nochange_20261107_141101.png",
     "wechat_chat_cycle0011_墨_nochange_20261107_141134.png"),
    ("墨 复查稳定 629→642", "wechat_chat_cycle0003_墨_changed_20261107_120629.png",
     "wechat_chat_cycle0003_墨_confirmed_20261107_120642.png"),
    ("墨 120842 vs 120753", "wechat_chat_cycle0005_墨_confirmed_20261107_120753.png",
     "wechat_chat_cycle0007_墨_nochange_20261107_120842.png"),
    ("水蜜桃 复查 922→930", "wechat_chat_cycle0004_水蜜桃_changed_20261107_140922.png",
     "wechat_chat_cycle0004_水蜜桃_confirmed_20261107_140930.png"),
    ("水蜜桃 复查 1033→1040", "wechat_chat_cycle0008_水蜜桃_changed_20261107_141033.png",
     "wechat_chat_cycle0008_水蜜桃_confirmed_20261107_141040.png"),
    ("水蜜桃 复查 1106→1114", "wechat_chat_cycle0010_水蜜桃_changed_20261107_141106.png",
     "wechat_chat_cycle0010_水蜜桃_confirmed_20261107_141114.png"),
]

for bucket in (32, 16, 8):
    print(f"\n############ 量化桶 = rel//{bucket} ############")
    print(f"{'样本':26s} {'差异格':>7s} {'占比%':>7s} {'最大簇':>7s}  判定(簇>=12,行>=2,列>=4)")
    for group, cases in (("真实消息", REAL), ("噪声", NOISE)):
        print(f"-- {group} --")
        for label, a, b in cases:
            pa, pb = DEBUG / a, DEBUG / b
            if not pa.exists() or not pb.exists():
                print(f"{label:26s} 缺文件")
                continue
            (cols, rows), ra = rel_of(Image.open(pa))
            (c2, r2), rb = rel_of(Image.open(pb))
            if (cols, rows) != (c2, r2):
                continue
            qa = bytes(min(31, v // bucket) for v in ra)
            qb = bytes(min(31, v // bucket) for v in rb)
            mask = {i for i in range(len(qa)) if qa[i] != qb[i]}
            frac = len(mask) / len(qa) * 100
            cl = cluster(mask, cols) if mask else 0
            # 该簇的行列跨度
            verdict = "新消息" if cl >= 12 else "噪声"
            print(f"{label:26s} {len(mask):>7d} {frac:>6.2f}% {cl:>7d}  {verdict}")
