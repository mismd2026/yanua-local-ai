"""PDFから文字を取り出す。文字の入っていないページは OCR（tesseract、社内で処理）で読む。"""

from __future__ import annotations

import re
import shutil
from dataclasses import dataclass, field
from pathlib import Path

import pdfplumber

OCR_LANG = "jpn+eng"
MIN_TEXT = 20   # これより文字が少ないページは、スキャンした画像とみなして OCR する


class OCRUnavailable(RuntimeError):
    pass


@dataclass
class Page:
    number: int
    text: str
    ocr: bool = False
    tables: list[list[list[str]]] = field(default_factory=list)


@dataclass
class PdfText:
    path: Path
    pages: list[Page]

    @property
    def text(self) -> str:
        return "\n".join(p.text for p in self.pages)

    @property
    def used_ocr(self) -> bool:
        return any(p.ocr for p in self.pages)


def ocr_available() -> bool:
    return shutil.which("tesseract") is not None and shutil.which("pdftoppm") is not None


def _ocr_page(path: Path, number: int, lang: str) -> str:
    if not ocr_available():
        raise OCRUnavailable(
            "文字のないページ（スキャンした画像）を読むには tesseract と poppler が必要です。"
            "WSL/Ubuntu では: sudo apt install tesseract-ocr tesseract-ocr-jpn poppler-utils"
        )
    import pytesseract
    from pdf2image import convert_from_path

    image = convert_from_path(str(path), dpi=300, first_page=number, last_page=number)[0]
    text = pytesseract.image_to_string(image, lang=lang)
    # 大きな文字の表題は、ページ全体の読み取りでは抜けやすい。上のほうだけを1つの塊として読み直し、抜けた行を足す
    w, h = image.size
    head = pytesseract.image_to_string(image.crop((0, 0, w, int(h * 0.15))), lang=lang, config="--psm 6")
    compact = re.sub(r"\s", "", text)
    missing = [ln.strip() for ln in head.splitlines() if len(ln.strip()) >= 2 and re.sub(r"\s", "", ln) not in compact]
    if missing:
        text = "\n".join(missing) + "\n" + text
    # tesseract は日本語の文字の間に空白を入れがちなので、日本語どうしの間の空白を詰める
    return re.sub(r"(?<=[^\x00-\x7F])[ \t]+(?=[^\x00-\x7F])", "", text)


def read_pdf(path: Path, ocr: bool = True, lang: str = OCR_LANG, tables: bool = False) -> PdfText:
    """PDFの各ページの文字（と、必要なら表）を取り出す。"""
    pages = []
    with pdfplumber.open(path) as pdf:
        for i, p in enumerate(pdf.pages, start=1):
            text = (p.extract_text() or "").strip()
            page = Page(i, text)
            if tables and len(text) >= MIN_TEXT:
                page.tables = [[[c or "" for c in row] for row in t] for t in p.extract_tables()]
            if len(text) < MIN_TEXT and ocr:
                page.text = _ocr_page(path, i, lang).strip()
                page.ocr = True
            pages.append(page)
    return PdfText(path, pages)
