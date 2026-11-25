# -*- coding: utf-8 -*-
"""标定：真实新消息 vs 噪声，在「相对暗度」上的差异幅度分布。

现行量化：rel = |v-bg|*255//bg，再 min(7, rel//32) → 只有 rel>=32（灰度比背景暗
31 级以上）才会跳出 0 档。微信对方气泡是白色、文字浅灰，8x8 降采样后 rel 只有
12~30，全部被吞成 0 档 → 气泡「隐形」。
"""
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


def compare(a, b):
    """返回 (cols,rows) 网格下两图的 rel 差异列表 [(idx, relA, relB)]"""
    sa, bga, ra = rel_map(Image.open(DEBUG / a))
    sb, bgb, rb = rel_map(Image.open(DEBUG / b))
    if sa != sb:
        return None
    diffs = [(i, ra[i], rb[i]) for i in range(len(ra)) if ra[i] != rb[i]]
    return sa, bga, bgb, diffs


def cluster_stats(diffs, cols):
    changed = {i for i, _, _ in diffs}
    seen, best = set(), 0
    for s in changed:
        if s in seen:
            continue
        stack, comp = [s], []
        seen.add(s)
        while stack:
            idx = stack.pop()
            comp.append(idx)
            r, c = divmod(idx, cols)
            for dr in (-1, 0, 1):
                for dc in (-1, 0, 1):
                    nr, nc = r + dr, c + dc
                    if 0 <= nr and 0 <= nc:
                        nb = nr * cols + nc
                        if nb in changed and nb not in seen:
                            seen.add(nb)
                            stack.append(nb)
        best = max(best, len(comp))
    return best


REAL = [
    ("墨 买房+500w (14:09:17→14:09:50)",
     "wechat_chat_cycle0003_墨_nochange_20261107_140917.png",
     "wechat_chat_cycle0005_墨_nochange_20261107_140950.png"),
    ("墨 4行消息 (10:30:27→10:30:47)",
     "wechat_chat_cycle0006_墨_nochange_20261107_103027.png",
     "wechat_chat_cycle0007_墨_nochange_20261107_103047.png"),
    ("墨 好的 (10:31:27→10:31:48)",
     "wechat_chat_cycle0009_墨_nochange_20261107_103127.png",
     "wechat_chat_cycle0010_墨_nochange_20261107_103148.png"),
    ("墨 12:06:29 真消息",
     "wechat_chat_cycle0006_墨_nochange_20261107_103027.png",
     "wechat_chat_cycle0003_墨_changed_20261107_120629.png"),
]
NOISE = [
    ("墨 边缘闪烁 (10:29:05→10:29:26)",
     "wechat_chat_cycle0002_墨_nochange_20261107_102905.png",
     "wechat_chat_cycle0003_墨_nochange_20261107_102926.png"),
    ("墨 10:29:26→10:29:46",
     "wechat_chat_cycle0003_墨_nochange_20261107_102926.png",
     "wechat_chat_cycle0004_墨_nochange_20261107_102946.png"),
    ("墨 10:31:48→10:32:08",
     "wechat_chat_cycle0010_墨_nochange_20261107_103148.png",
     "wechat_chat_cycle0011_墨_nochange_20261107_103208.png"),
    ("墨 14:09:50→14:10:27 (同内容)",
     "wechat_chat_cycle0005_墨_nochange_20261107_140950.png",
     "wechat_chat_cycle0007_墨_nochange_20261107_141027.png"),
    ("墨 14:10:27→14:11:01 (同内容)",
     "wechat_chat_cycle0007_墨_nochange_20261107_141027.png",
     "wechat_chat_cycle0009_墨_nochange_20261107_141101.png"),
]

print(f"{'样本':38s} {'档差异格':>8s} {'rel>=4':>7s} {'rel>=8':>7s} {'rel>=16':>8s} {'最大簇@rel>=8':>12s}")
print("-" * 92)
for group, cases in (("真实消息", REAL), ("噪声", NOISE)):
    print(f"--- {group} ---")
    for label, a, b in cases:
        r = compare(a, b)
        if not r:
            print(f"{label:38s} 尺寸不一致")
            continue
        (cols, rows), bga, bgb, diffs = r
        n4 = sum(1 for _, x, y in diffs if abs(x - y) >= 4)
        n8 = sum(1 for _, x, y in diffs if abs(x - y) >= 8)
        n16 = sum(1 for _, x, y in diffs if abs(x - y) >= 16)
        d8 = [(i, x, y) for i, x, y in diffs if abs(x - y) >= 8]
        cl = cluster_stats(d8, cols) if d8 else 0
        print(f"{label:38s} {len(diffs):>8d} {n4:>7d} {n8:>7d} {n16:>8d} {cl:>12d}")
    print()
