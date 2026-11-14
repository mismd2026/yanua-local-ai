"""PDFを Word（.docx）や Excel（.xlsx）に変換する。

- Word：ページごとに見出しを付け、本文の段落と表をそのまま入れる。スキャンしたページは OCR の文字を入れる
- Excel：PDFの中の表を、1つの表を1つのシートにして入れる。表の外の文字は「本文」のシートに、
  表が見つからないページは、行ごとにA列へ入れる
- Excel の値は、数字も含めてすべて文字列で入れる（0 で始まるコードや郵便番号を崩さないため）
レイアウト（文字の位置や大きさ、図）までは再現しない。編集や再利用のための「中身の取り出し」と考える。
会社の決まった様式で作り直したいときは、ルールに様式（テンプレート）を付けて form.py で差し込む。
スキャンしたPDFの表は、OCRでは表の形を取り出せないため、Excelでは行ごとの文字になる。
"""

from __future__ import annotations

import re
from pathlib import Path

from docx import Document
from openpyxl import Workbook
from openpyxl.utils import get_column_letter

from .extract import PdfText, read_pdf

TEXT_FORMAT = "@"   # Excel の「文字列」の表示形式


def _paragraphs(text: str) -> list[str]:
    """PDFの行を段落にまとめる。

    帳票は1行が1項目のことが多いので、基本は1行を1段落にする。
    前の行が長く（折り返しとみられ）、句点などで終わっていないときだけ、次の行とつなげる。
    """
    paras: list[str] = []
    for line in (ln.strip() for ln in text.splitlines()):
        if not line:
            continue
        prev = paras[-1] if paras else ""
        if prev and len(prev) >= 30 and not re.search(r"[。．.！？!?：:）)」]$", prev):
            sep = "" if re.search(r"[^\x00-\x7F]$", prev) else " "
            paras[-1] = prev + sep + line
        else:
            paras.append(line)
    return paras


def _table_text(table: list[list[str]]) -> set[str]:
    return {c.strip() for row in table for c in row if c and c.strip()}


def _lines_outside_tables(page) -> list[str]:
    in_tables = set().union(*[_table_text(t) for t in page.tables]) if page.tables else set()
    return [ln for ln in page.text.splitlines()
            if ln.strip() and not (in_tables and all(part in in_tables for part in ln.split()))]


def to_docx(src: Path, dest: Path, doc: PdfText | None = None) -> Path:
    doc = doc or read_pdf(src, tables=True)
    word = Document()
    word.add_heading(src.stem, level=1)
    for page in doc.pages:
        if len(doc.pages) > 1:
            word.add_heading(f"{page.number}ページ" + ("（OCR）" if page.ocr else ""), level=2)
        # 表の中身は表として入れるので、本文からは除く
        for para in _paragraphs("\n".join(_lines_outside_tables(page))):
            word.add_paragraph(para)
        for table in page.tables:
            if not table or not table[0]:
                continue
            t = word.add_table(rows=len(table), cols=max(len(r) for r in table))
            t.style = "Table Grid"
            for i, row in enumerate(table):
                for j, cell in enumerate(row):
                    t.cell(i, j).text = (cell or "").replace("\n", " ")
            word.add_paragraph("")
    dest.parent.mkdir(parents=True, exist_ok=True)
    word.save(dest)
    return dest


def _value(cell: str) -> str:
    # 数字も文字列のまま入れる。コードや郵便番号のように 0 で始まる値（0 の数が桁を表す）を、数値にして崩さないため
    return (cell or "").replace("\n", " ").strip()


def _put_row(ws, values: list[str]) -> None:
    ws.append(values)
    for cell in ws[ws.max_row]:
        cell.number_format = TEXT_FORMAT


def to_xlsx(src: Path, dest: Path, doc: PdfText | None = None) -> Path:
    doc = doc or read_pdf(src, tables=True)
    wb = Workbook()
    wb.remove(wb.active)
    for page in doc.pages:
        tables = [t for t in page.tables if t and t[0]]
        if not tables:
            ws = wb.create_sheet(f"p{page.number}" + ("_OCR" if page.ocr else ""))
            for line in page.text.splitlines():
                if line.strip():
                    _put_row(ws, [line.strip()])
            ws.column_dimensions["A"].width = 80
            continue
        # 表の外の文字（宛先・日付・合計など）も、本文のシートに残す
        body = wb.create_sheet(f"p{page.number}_本文")
        for line in _lines_outside_tables(page):
            _put_row(body, [line.strip()])
        body.column_dimensions["A"].width = 80
        for k, table in enumerate(tables, start=1):
            ws = wb.create_sheet(f"p{page.number}_表{k}")
            for row in table:
                _put_row(ws, [_value(c) for c in row])
            for j in range(1, max(len(r) for r in table) + 1):
                width = max(len(str(ws.cell(i, j).value or "")) for i in range(1, ws.max_row + 1))
                ws.column_dimensions[get_column_letter(j)].width = min(60, max(8, width * 2))
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)
    return dest
