#!/usr/bin/env python3
"""生成 FResearch 的 OG 分享预览图 (1200x630 PNG)。

可复现: 改完跑 `python3 scripts/make_og_image.py` 重新生成 ../og-image.png。
配色对齐站点暗色主题 (--text #f2f0ec / --gold #c9a45c / 深炭底)。
"""
import os

from PIL import Image, ImageDraw, ImageFont

W, H = 1200, 630
BG = (18, 15, 11)          # 深炭暖底
PANEL = (24, 20, 15)
TEXT = (242, 240, 236)     # --text
SUB = (154, 151, 145)      # --text-secondary
GOLD = (201, 164, 92)      # --gold
LINE = (60, 48, 30)        # --border

GEORGIA_B = "/System/Library/Fonts/Supplemental/Georgia Bold.ttf"
GEORGIA = "/System/Library/Fonts/Supplemental/Georgia.ttf"
SONGTI = "/System/Library/Fonts/Supplemental/Songti.ttc"
HIRAGINO = "/System/Library/Fonts/Hiragino Sans GB.ttc"

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "..", "og-image.png")


def font(path, size, index=0):
    return ImageFont.truetype(path, size, index=index)


def main():
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # 细金边框
    d.rectangle([28, 28, W - 28, H - 28], outline=LINE, width=2)

    # 顶部小标 + 金线
    d.text((72, 74), "F R E S E A R C H", font=font(GEORGIA, 30), fill=SUB)
    d.line([(72, 128), (W - 72, 128)], fill=LINE, width=2)

    # 主标题：与站内核心定位保持一致——目标价、公开观点、事后验证。
    d.text((72, 184), "投行目标价与观点验证", font=font(SONGTI, 82), fill=GOLD)
    d.text((72, 326), "What they said. What the market did.",
           font=font(GEORGIA_B, 43), fill=TEXT)

    # 副标题
    d.text((72, 422), "评级与目标价 · 机构观点 · 1 / 5 / 20 日市场反应",
           font=font(HIRAGINO, 32), fill=SUB)

    # 底部 ticker chips
    tickers = ["NVDA", "TSM", "AVGO", "MSFT", "MU", "ASML", "AMD", "ANET"]
    x = 72
    y = 512
    chip_font = font(GEORGIA, 26)
    for t in tickers:
        pad = 16
        tw = d.textlength(t, font=chip_font)
        bw = int(tw) + pad * 2
        bh = 48
        d.rounded_rectangle([x, y, x + bw, y + bh], radius=10,
                            fill=PANEL, outline=LINE, width=1)
        d.text((x + pad, y + 10), t, font=chip_font, fill=TEXT)
        x += bw + 14

    img.save(OUT, "PNG")
    print(f"written {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()
