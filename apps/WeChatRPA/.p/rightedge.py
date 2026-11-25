# -*- coding: utf-8 -*-
"""水蜜桃每轮都在 x≈1064-1152 出现 54 格块，复查又回退。看那到底是什么。"""
from pathlib import Path

from PIL import Image

DEBUG = Path("runtime/screenshots/debug")
A = "wechat_chat_cycle0002_水蜜桃_baseline_20261107_140848.png"
B = "wechat_chat_cycle0004_水蜜桃_changed_20261107_140922.png"
C = "wechat_chat_cycle0004_水蜜桃_confirmed_20261107_140930.png"


def art(path, box, cols=70, label=""):
    img = Image.open(DEBUG / path)
    reg = img.crop(box)
    w, h = reg.size
    rows = max(1, int(cols * h / w / 2))
    small = reg.convert("L").resize((cols, rows), Image.BOX)
    px = small.load()
    print(f"\n--- {label} box={box} ---")
    for y in range(rows):
        print("".join("#" if px[x, y] < 150 else ("+" if px[x, y] < 215 else ".") for x in range(cols)))


def stats(path, box, label):
    reg = Image.open(DEBUG / path).convert("RGB").crop(box)
    data = list(reg.getdata())
    n = len(data)
    green = sum(1 for p in data if p[1] > 165 and p[0] < 190 and p[2] < 190
                and (p[1] - max(p[0], p[2])) > 25)
    white = sum(1 for p in data if min(p) > 240)
    print(f"{label}: 绿色像素={green*100//n}% 纯白={white*100//n}%")


BOX = (1040, 380, 1229, 520)
for lbl, p in (("baseline 140848", A), ("changed 140922", B), ("confirmed 140930", C)):
    stats(p, BOX, f"  {lbl}")

art(A, BOX, 60, "baseline 右侧 y380-520")
art(B, BOX, 60, "changed 右侧 y380-520")
art(C, BOX, 60, "confirmed 右侧 y380-520")

# 最右列宽度：微信滚动条到底在哪
img = Image.open(DEBUG / B).convert("RGB")
w, h = img.size
print(f"\n图宽={w}，检查最右 40 列的颜色（x={w-40}..{w}）")
px = img.load()
for x in range(w - 40, w, 4):
    col = [px[x, y] for y in range(380, 520, 4)]
    avg = tuple(sum(c[i] for c in col) // len(col) for i in range(3))
    print(f"  x={x}: avg={avg}")