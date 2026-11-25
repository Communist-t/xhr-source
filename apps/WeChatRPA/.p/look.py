# -*- coding: utf-8 -*-
"""看清两个被误判的变化块到底是什么内容。"""
from pathlib import Path

from PIL import Image

DEBUG = Path("runtime/screenshots/debug")


def art(path, box, cols=80, label=""):
    img = Image.open(DEBUG / path)
    region = img.crop(box)
    w, h = region.size
    rows = max(1, int(cols * h / w / 2))
    small = region.convert("L").resize((cols, rows), Image.BOX)
    px = small.load()
    print(f"\n=== {label} :: {box} ({w}x{h}) ===")
    for y in range(rows):
        print("".join("#" if px[x, y] < 150 else ("+" if px[x, y] < 215 else ".") for x in range(cols)))


def mean(path, box, label):
    img = Image.open(DEBUG / path).convert("RGB")
    r = img.crop(box)
    data = list(r.get_flattened_data()) if hasattr(r, "get_flattened_data") else list(r.getdata())
    n = len(data)
    R = sum(p[0] for p in data) // n
    G = sum(p[1] for p in data) // n
    B = sum(p[2] for p in data) // n
    green = sum(1 for p in data if p[1] > 165 and p[0] < 190 and p[2] < 190 and (p[1] - max(p[0], p[2])) > 25)
    print(f"{label}: mean=({R},{G},{B}) 绿点={green}/{n} ({green*100//n}%)")


# 墨：被当成噪声的 8 格块（y296-304, x48-72）
print("【墨 第11轮】8 格块 = 买房？")
mean("wechat_chat_cycle0011_墨_nochange_20261107_141134.png", (30, 280, 130, 320), "  该区域")
mean("wechat_chat_cycle0003_墨_nochange_20261107_140917.png", (30, 280, 130, 320), "  第3轮同区域(基准)")
art("wechat_chat_cycle0011_墨_nochange_20261107_141134.png", (0, 270, 260, 380), 70, "墨 第11轮 左侧 y270-380")
art("wechat_chat_cycle0003_墨_nochange_20261107_140917.png", (0, 270, 260, 380), 70, "墨 第3轮 左侧 y270-380")

# 水蜜桃：每轮都触发、复查又回退的 54 格块（y400-488, x1064-1152）
print("\n【水蜜桃 第6轮】54 格块在最右侧 x1064-1152")
mean("wechat_chat_cycle0006_水蜜桃_changed_20261107_140956.png", (1040, 380, 1209, 500), "  changed 帧")
mean("wechat_chat_cycle0004_水蜜桃_changed_20261107_140922.png", (1040, 380, 1209, 500), "  第4轮 changed")
art("wechat_chat_cycle0004_水蜜桃_changed_20261107_140922.png", (1000, 360, 1229, 520), 70, "水蜜桃 changed 右侧")