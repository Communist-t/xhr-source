# -*- coding: utf-8 -*-
"""量化：为什么「买房」「500w」两条气泡只产生 8 格差异。"""
from collections import Counter
from pathlib import Path

from PIL import Image

DEBUG = Path("runtime/screenshots/debug")
A = "wechat_chat_cycle0003_墨_nochange_20261107_140917.png"   # 27796 字节
B = "wechat_chat_cycle0011_墨_nochange_20261107_141134.png"  # 33289 字节


def cells_of(img):
    g = img.convert("L")
    w, h = g.size
    top = min(16, max(0, h // 3))
    right = max(0, w - 20)
    small = g.crop((0, top, right, h)).resize(
        (max(1, right // 8), max(1, (h - top) // 8)), Image.BOX)
    data = small.tobytes()
    bg = Counter(data).most_common(1)[0][0]
    quant = bytes(min(7, abs(v - bg) * 255 // (bg if bg >= 40 else 200) // 32) for v in data)
    return small.size, bg, data, quant


ia, ib = Image.open(DEBUG / A), Image.open(DEBUG / B)
(sa, bga, da, qa), (sb, bgb, db, qb) = cells_of(ia), cells_of(ib)
print(f"A size={sa} bg={bga}   B size={sb} bg={bgb}")
print(f"量化指纹差异格数 = {sum(1 for x, y in zip(qa, qb) if x != y)} / {len(qa)}")
print(f"原始灰度格差异>10 的格数 = {sum(1 for x, y in zip(da, db) if abs(x-y) > 10)}")
print(f"原始灰度格差异>3 的格数  = {sum(1 for x, y in zip(da, db) if abs(x-y) > 3)}")

cols = sa[0]
diff = [(i // cols, i % cols, da[i], db[i], qa[i], qb[i]) for i in range(len(da)) if da[i] != db[i]]
print(f"\n灰度不同的格子 (row, col, A灰度, B灰度, A量化, B量化):")
for r, c, va, vb, qa_, qb_ in diff[:40]:
    flag = "  <-- 量化后相同(被吞掉)" if qa_ == qb_ else ""
    print(f"  row={r:3d} col={c:3d}  A={va:3d} B={vb:3d}  qA={qa_} qB={qb_}{flag}")
same_quant = sum(1 for *_, x, y in diff if x == y)
print(f"\n灰度有变化但量化后归为同一档的格子: {same_quant} / {len(diff)}")

# 看「买房」「500w」所在行的实际像素
print("\n【买房/500w 区域像素统计】")
for name, img in (("A(第3轮)", ia), ("B(第11轮)", ib)):
    reg = img.convert("RGB").crop((30, 260, 200, 360))
    data = list(reg.getdata())
    n = len(data)
    white = sum(1 for p in data if min(p) > 240)
    near_white = sum(1 for p in data if 200 < min(p) <= 240)
    text = sum(1 for p in data if max(p) < 150)
    print(f"  {name}: 纯白(>240)={white*100//n}% 近白(200-240)={near_white*100//n}% 深色文字={text*100//n}%")