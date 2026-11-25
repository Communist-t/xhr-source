# -*- coding: utf-8 -*-
"""标定：用日志标注「真实新消息 / 噪声」，找能分开两者的 rel 阈值。"""
from collections import Counter
from pathlib import Path

from PIL import Image

DEBUG = Path("runtime/screenshots/debug")


def rel_map(img):
    g = img.convert("L")
    w, h = g.size
    top = min(16, max(0, h // 3))
    right = max(0, w - 20)
    small = g.crop((0, top, right, h)).resize(
        (max(1, right // 8), max(1, (h - top) // 8)), Image.BOX)
    data = small.tobytes()
    bg = Counter(data).most_common(1)[0][0]
    denom = bg if bg >= 40 else 200
    return small.size, bg, bytes(min(255, abs(v - bg) * 255 // denom) for v in data)


def largest_cluster(mask_set, cols):
    seen, best = set(), 0
    for s in mask_set:
        if s in seen:
            continue
        stack, comp = [s], 0
        seen.add(s)
        while stack:
            idx = stack.pop()
            comp += 1
            r, c = divmod(idx, cols)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    if dr == 0 and dc == 0:
                        continue
                    nb = (r + dr) * cols + (c + dc)
                    if 0 <= r + dr and 0 <= c + dc and nb in mask_set and nb not in seen:
                        seen.add(nb)
                        stack.append(nb)
        best = max(best, comp)
    return best


# (标签, 前帧, 后帧, 是否真实新消息)
PAIRS = [
    ("墨 买房+500w 到达", "wechat_chat_cycle0003_墨_nochange_20261107_140917.png",
     "wechat_chat_cycle0005_墨_nochange_20261107_140950.png", True),
    ("墨 同内容 140950→141027", "wechat_chat_cycle0005_墨_nochange_20261107_140950.png",
     "wechat_chat_cycle0007_墨_nochange_20261107_141027.png", False),
    ("墨 同内容 141027→141101", "wechat_chat_cycle0007_墨_nochange_20261107_141027.png",
     "wechat_chat_cycle0009_墨_nochange_20261107_141101.png", False),
    ("墨 同内容 141101→141134", "wechat_chat_cycle0009_墨_nochange_20261107_141101.png",
     "wechat_chat_cycle0011_墨_nochange_20261107_141134.png", False),
    ("墨 baseline140830→140917", "wechat_chat_cycle0001_墨_baseline_20261107_140830.png",
     "wechat_chat_cycle0003_墨_nochange_20261107_140917.png", False),
    ("墨 120629 changed(真消息)", "wechat_chat_cycle0001_墨_baseline_20261107_120548.png",
     "wechat_chat_cycle0003_墨_changed_20261107_120629.png", True),
    ("墨 120629→120642 复查(稳定)", "wechat_chat_cycle0003_墨_changed_20261107_120629.png",
     "wechat_chat_cycle0003_墨_confirmed_20261107_120642.png", False),
    ("墨 120740 changed(真消息)", "wechat_chat_cycle0003_墨_confirmed_20261107_120642.png",
     "wechat_chat_cycle0005_墨_changed_20261107_120740.png", True),
    ("墨 120921 changed(真消息)", "wechat_chat_cycle0005_墨_confirmed_20261107_120753.png",
     "wechat_chat_cycle0009_墨_changed_20261107_120921.png", True),
    ("水蜜桃 baseline→140922(反复误报)", "wechat_chat_cycle0002_水蜜桃_baseline_20261107_140848.png",
     "wechat_chat_cycle0004_水蜜桃_changed_20261107_140922.png", False),
    ("水蜜桃 140922→140930 复查", "wechat_chat_cycle0004_水蜜桃_changed_20261107_140922.png",
     "wechat_chat_cycle0004_水蜜桃_confirmed_20261107_140930.png", False),
    ("水蜜桃 141106→141140(真消息)", "wechat_chat_cycle0010_水蜜桃_changed_20261107_141106.png",
     "wechat_chat_cycle0012_水蜜桃_changed_20261107_141140.png", True),
]

print(f"{'样本':34s} {'性质':>6s} {'rel>=8':>7s} {'rel>=12':>8s} {'rel>=16':>8s} {'rel>=24':>8s}  簇@rel>=12")
print("-" * 96)
for label, a, b, real in PAIRS:
    pa, pb = DEBUG / a, DEBUG / b
    if not pa.exists() or not pb.exists():
        print(f"{label:34s} 缺文件")
        continue
    (cols, rows), bga, ra = rel_map(Image.open(pa))
    (cols2, rows2), bgb, rb = rel_map(Image.open(pb))
    if (cols, rows) != (cols2, rows2):
        print(f"{label:34s} 网格不一致 {cols}x{rows} vs {cols2}x{rows2}")
        continue
    n = len(ra)
    d = [abs(x - y) for x, y in zip(ra, rb)]
    counts = [sum(1 for v in d if v >= k) for k in (8, 12, 16, 24)]
    mask = {i for i, v in enumerate(d) if v >= 12}
    cl = largest_cluster(mask, cols) if mask else 0
    print(f"{label:34s} {'真消息' if real else '噪声':>6s} "
          f"{counts[0]:>7d} {counts[1]:>8d} {counts[2]:>8d} {counts[3]:>8d} {cl:>10d}")
