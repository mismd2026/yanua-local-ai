"""話し手：Web会議の録画に出ている名前を、画面から読み取る（Zoom・Teams・Meet）。

声で人を聞き分けるのではなく、「その発言のとき、どの画面（名前）が話し手として映っていたか」を見る。
1つの画面に複数人が映っていても（会議室など）、名前の表示が1つなら、その名前を話し手にする。

発言ごとに、その時刻の画面を取り出して:
  1. ギャラリービュー：発言中の人の枠は色で強調されるので、その色の枠を探し、枠の左下の名前を読む
  2. スピーカービュー：話している人が大きく映るので、画面の左下（ツールごとの位置）の名前を読む
  読んだ名前は、人事データの氏名・出席者の名前と照らして表記をそろえる（OCRの小さな読み違いを直す）
  読めなければ「不明」（議事録で人が直す）

ツールごとの位置や色（PRESETS）は、録画の設定や版で変わることがある。--debug-frames で切り出した画像を見て、
合わないときは config で直す。OCR は社内（tesseract）で行う。
"""

from __future__ import annotations

import difflib
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

UNKNOWN = "不明"


@dataclass
class Preset:
    name: str
    label_regions: list[tuple[float, float, float, float]]   # スピーカービューの名前の位置（画面に対する割合 x0,y0,x1,y1）
    highlight: list[tuple[int, int, int]]                    # ギャラリービューの、発言中の枠の色
    tolerance: int = 40
    tile_label: tuple[float, float, float, float] = (0.0, 0.84, 0.65, 1.0)   # 枠の中の名前の位置（枠に対する割合）
    drop_words: list[str] = field(default_factory=list)   # 名前のあとに付く表示（ホストなど）


PRESETS = {
    "zoom": Preset("zoom", [(0.0, 0.88, 0.45, 1.0)], [(35, 217, 89), (0, 210, 0), (255, 210, 0)],
                   drop_words=["ホスト", "共同ホスト", "Host", "Co-host", "ゲスト", "Guest", "自分", "Me"]),
    "teams": Preset("teams", [(0.0, 0.87, 0.45, 1.0)], [(127, 133, 245), (91, 95, 199), (98, 100, 167)],
                    drop_words=["開催者", "Organizer", "ゲスト", "Guest", "外部", "External"]),
    "meet": Preset("meet", [(0.0, 0.88, 0.45, 1.0)], [(138, 180, 248), (26, 115, 232)],
                   drop_words=["主催者", "Host", "あなた", "You", "プレゼンテーション"]),
}


def _key(text: str) -> str:
    return re.sub(r"\s", "", unicodedata.normalize("NFKC", text or ""))


def clean_label(text: str, preset: Preset) -> str:
    """OCRの結果から名前らしい1行を取り出す（記号やアイコンの読み違い、「ホスト」などの表示を除く）。"""
    best = ""
    for line in (text or "").splitlines():
        line = unicodedata.normalize("NFKC", line)
        line = re.sub(r"[()（）\[\]【】]?\s*(" + "|".join(map(re.escape, preset.drop_words)) + r")\s*[()（）\[\]【】]?",
                      "", line) if preset.drop_words else line
        line = re.sub(r"[|｜_~=<>«»※*#@!?！？:;'\"`]", " ", line)
        line = re.sub(r"\s+", " ", line).strip(" .-、。・")
        if len(re.sub(r"[^\w]", "", line)) > len(re.sub(r"[^\w]", "", best)):
            best = line
    return best if 2 <= len(_key(best)) <= 30 else ""


def match_name(label: str, candidates: list[str], cutoff: float = 0.6) -> str:
    """読んだ名前を、人事データ・出席者の名前にそろえる。近い名前がなければ、読んだままにする。"""
    if not label:
        return UNKNOWN
    keys = {_key(c): c for c in candidates if c}
    k = _key(label)
    if k in keys:
        return keys[k]
    for ck, c in keys.items():   # 「高橋 美咲（営業部）」のように前後に何か付いている
        if ck and ck in k:
            return c
    close = difflib.get_close_matches(k, list(keys), n=1, cutoff=cutoff)
    return keys[close[0]] if close else label


def _ocr(image, debug: Path | None = None, tag: str = "") -> list[str]:
    """名前の部分を読む。そのままの画像と、白黒にした画像の2通りで読み、両方の結果を返す。"""
    import pytesseract
    from PIL import ImageOps, ImageStat

    img = image.convert("L")
    if ImageStat.Stat(img).mean[0] < 128:   # 暗い地に明るい文字は、反転して読む
        img = ImageOps.invert(img)
    img = img.resize((img.width * 3, img.height * 3))
    variants = [img, img.point(lambda v: 255 if v > 140 else 0)]   # 白黒にすると、地の模様が消える
    out = []
    for k, v in enumerate(variants):
        if debug:
            debug.mkdir(parents=True, exist_ok=True)
            v.save(debug / f"{tag}_{k}.png")
        text = pytesseract.image_to_string(v, lang="jpn+eng", config="--psm 7")
        out.append(re.sub(r"(?<=[^\x00-\x7F])[ \t]+(?=[^\x00-\x7F])", "", text))
    return out


def _pick(texts: list[str], preset: Preset, candidates: list[str]) -> str:
    """読んだ結果のうち、出席者の名前に合うものを優先し、なければ最初に読めたものを使う。"""
    labels = [lb for t in texts if (lb := clean_label(t, preset))]
    for lb in labels:
        m = match_name(lb, candidates)
        if m in candidates:
            return m
    return match_name(labels[0], candidates) if labels else UNKNOWN


def _crop(image, box, rel):
    x0, y0, x1, y1 = box
    w, h = x1 - x0, y1 - y0
    return image.crop((int(x0 + rel[0] * w), int(y0 + rel[1] * h), int(x0 + rel[2] * w), int(y0 + rel[3] * h)))


def highlighted_tile(image, preset: Preset) -> tuple[int, int, int, int] | None:
    """発言中の強調の枠（決まった色の線）を探し、その枠の範囲を返す。"""
    import numpy as np

    arr = np.asarray(image.convert("RGB")).astype(int)
    h, w, _ = arr.shape
    for color in preset.highlight:
        mask = (np.abs(arr - np.array(color)).max(axis=2) <= preset.tolerance)
        if mask.sum() < (w + h) * 0.2:   # 枠の線として少なすぎる
            continue
        ys, xs = np.nonzero(mask)
        x0, x1, y0, y1 = xs.min(), xs.max(), ys.min(), ys.max()
        if (x1 - x0) > w * 0.12 and (y1 - y0) > h * 0.12 and (x1 - x0) < w * 0.98:
            # 枠の線そのものは読み取りの邪魔になるので、内側だけにする
            inset = max(8, int(min(x1 - x0, y1 - y0) * 0.02))
            return int(x0) + inset, int(y0) + inset, int(x1) - inset, int(y1) - inset
    return None


def speaker_from_frame(image, preset: Preset, candidates: list[str], debug: Path | None = None, tag: str = "") -> str:
    tile = highlighted_tile(image, preset)
    if tile:
        return _pick(_ocr(_crop(image, tile, preset.tile_label), debug, f"{tag}_tile"), preset, candidates)
    w, h = image.size
    for i, region in enumerate(preset.label_regions):
        name = _pick(_ocr(_crop(image, (0, 0, w, h), region), debug, f"{tag}_view{i}"), preset, candidates)
        if name != UNKNOWN:
            return name
    return UNKNOWN


def frames_at(media: Path, times: list[float]):
    """動画の、指定した時刻の画面を順に返す。動画のない（音声だけの）ファイルなら None を返す。"""
    import av

    with av.open(str(media)) as container:
        if not container.streams.video:
            yield from (None for _ in times)
            return
        stream = container.streams.video[0]
        for t in times:
            container.seek(max(0, int(t / stream.time_base)), stream=stream, backward=True)
            image = None
            for frame in container.decode(stream):
                image = frame.to_image()
                if frame.time is not None and frame.time >= t - 0.05:
                    break
            yield image


def label_segments(media: Path, segments, tool: str, candidates: list[str], debug: Path | None = None) -> int:
    """発言ごとに話し手を入れる。発言の中ほどの画面で読み、読めなければ始めと終わりの近くも見る。返り値は読めた数。"""
    preset = PRESETS[tool]
    found = 0
    for i, s in enumerate(segments):
        mid = (s.start + s.end) / 2
        times = [mid, s.start + min(0.5, (s.end - s.start) / 4), s.end - min(0.5, (s.end - s.start) / 4)]
        name = UNKNOWN
        for j, image in enumerate(frames_at(media, times)):
            if image is None:
                break
            name = speaker_from_frame(image, preset, candidates, debug, f"{i:04d}_{j}")
            if name != UNKNOWN:
                break
        s.speaker = name
        found += name != UNKNOWN
    return found
