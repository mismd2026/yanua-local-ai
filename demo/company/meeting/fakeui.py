"""デモ・テスト用の、Web会議の録画「風」の画面を作る（Zoom・Teams・Meet）。

本物の画面ではなく、名前の表示の位置と、発言中の枠の強調の色だけを似せたもの。
話し手の読み取り（yanua/meeting/speakers.py）を、権利の心配なく、毎回同じ結果で確かめるために使う。
"""

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

FONT_FILES = ["/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf", "/usr/share/fonts/truetype/fonts-japanese-gothic.ttf",
              "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc", "C:/Windows/Fonts/meiryo.ttc"]
LOOK = {   # 背景、枠の地、発言中の枠の色、名前の地（None なら地なし）
    "zoom": ((26, 26, 26), (52, 52, 52), (35, 217, 89), (0, 0, 0)),
    "teams": ((41, 41, 41), (62, 62, 62), (127, 133, 245), (30, 30, 30)),
    "meet": ((32, 33, 36), (60, 64, 67), (138, 180, 248), None),
}


def font(size: int):
    for f in FONT_FILES:
        if Path(f).exists():
            return ImageFont.truetype(f, size)
    raise FileNotFoundError("日本語のフォントがありません（Ubuntu: sudo apt install fonts-ipafont-gothic）")


def _tile(d: ImageDraw.ImageDraw, box, name: str, look, active: bool, fsize: int):
    bg, tile, accent, label_bg = look
    x0, y0, x1, y1 = box
    d.rectangle(box, fill=tile)
    cx, cy, r = (x0 + x1) // 2, (y0 + y1) // 2 - (y1 - y0) // 12, min(x1 - x0, y1 - y0) // 5
    d.ellipse((cx - r, cy - r, cx + r, cy + r), fill=(110, 110, 110))            # 人の代わりの丸
    d.rectangle((cx - r * 1.4, cy + r * 1.1, cx + r * 1.4, cy + r * 2.2), fill=(110, 110, 110))
    f = font(fsize)
    tw = d.textlength(name, font=f)
    lx, ly = x0 + 12, y1 - fsize - 18
    if label_bg:
        d.rectangle((lx - 6, ly - 6, lx + tw + 8, ly + fsize + 8), fill=label_bg)
    d.text((lx, ly), name, font=f, fill=(255, 255, 255))
    if active:
        d.rectangle(box, outline=accent, width=6)


def render(tool: str, layout: str, names: list[str], active: int, size=(1280, 720)) -> Image.Image:
    look = LOOK[tool]
    img = Image.new("RGB", size, look[0])
    d = ImageDraw.Draw(img)
    w, h = size
    if layout == "speaker":
        _tile(d, (0, 0, w - 1, h - 1), names[active], look, False, 26)
    else:
        gap, cols = 10, 2
        rows = (len(names) + 1) // 2
        tw, th = (w - gap * (cols + 1)) // cols, (h - gap * (rows + 1)) // rows
        for i, n in enumerate(names):
            r, c = divmod(i, cols)
            x0, y0 = gap + c * (tw + gap), gap + r * (th + gap)
            _tile(d, (x0, y0, x0 + tw, y0 + th), n, look, i == active, 22)
    return img
