"""会社の様式（テンプレート）への差し込み（フォームオーバーレイ）。

PDFから読み取った項目を、ルールに付けた様式（Word / Excel）の決まった場所に入れて、新しいファイルを作る。
様式のレイアウト（罫線・文字の大きさ・セルの結合など）はそのまま残るので、「この様式を使ってください」と
決めておけば、書類ごとに形が崩れない。どのルールにも当たらない書類は差し込まない（個別に作るか、作り直す）。

様式の中に書く差し込み位置:
    {{date_jp}}   2026年11月10日          {{date}}  20261110        {{yyyy}} {{mm}} {{dd}}
    {{company}}   相手先の会社名           {{to}}    「御中」の宛先
    {{amount}}    金額（数字だけ）         {{amount_comma}}  132,000
    {{rule}}      ルール名                 {{source}}  元のPDFのファイル名
    ルールの fields に書いた独自の項目（例：{{invoice_no}}）
    明細：表の1行に {{items.品目}} {{items.数量}} のように、PDFの表の見出しの名前で書く（{{items.1}} のように列の番号でもよい）。
          その行が、PDFの表の行の数だけ増える。表の「合計」「小計」「消費税」の行は明細に入れない。
読み取れなかった項目は空欄にする。値はすべて文字列で入れる（0 で始まるコードなどを崩さないため）。
"""

from __future__ import annotations

import copy
import re
import unicodedata
from pathlib import Path

from .extract import PdfText

PLACEHOLDER = re.compile(r"\{\{\s*([\w.]+)\s*\}\}")
SUMMARY_ROWS = ("合計", "小計", "消費税", "税", "総計", "税込合計", "値引")


class FormError(ValueError):
    pass


def _norm(text: str) -> str:
    return re.sub(r"\s", "", unicodedata.normalize("NFKC", text or ""))


def items_from(doc: PdfText) -> tuple[list[str], list[list[str]]]:
    """PDFのいちばん大きい表から、見出しと明細の行を取り出す（合計などの行は除く）。"""
    tables = [t for p in doc.pages for t in p.tables if len(t) >= 2]
    if not tables:
        return [], ocr_rows(doc)
    table = max(tables, key=len)
    header = [(c or "").strip() for c in table[0]]
    rows = [[(c or "").replace("\n", " ").strip() for c in r] for r in table[1:]]
    rows = [r for r in rows if r and _norm(r[0]) not in SUMMARY_ROWS and any(r)]
    return header, rows


NUMERIC_TAIL = re.compile(r"^[¥￥]?[\d,.\s]+$")


def ocr_rows(doc: PdfText) -> list[list[str]]:
    """スキャンしたページ（OCR）の表の行を拾う。OCRは表の形を返さないので、
    「大きな空白で区切られ、最後が数字」の行を明細とみなす（列は様式の明細の並び順で入れる）。"""
    rows = []
    for page in doc.pages:
        if not page.ocr:
            continue
        for line in page.text.splitlines():
            parts = [x.strip() for x in re.split(r"\s{2,}", line.strip()) if x.strip()]
            if len(parts) >= 2 and NUMERIC_TAIL.match(parts[-1]) and _norm(parts[0]) not in SUMMARY_ROWS:
                rows.append([parts[0]] + [re.sub(r"\s", "", x) for x in parts[1:]])
    return rows


def _header_or_template(header: list[str], texts: list[str]) -> list[str]:
    """見出しがない（OCRの）明細は、様式の明細の並び順を見出しの代わりにする。"""
    if header:
        return header
    return [k.split(".", 1)[1] for t in texts for k in PLACEHOLDER.findall(t or "") if k.startswith("items.")]


def values_for(fields: dict[str, str], source: Path) -> dict[str, str]:
    v = dict(fields)
    if v.get("yyyy"):
        v["date_jp"] = f"{v['yyyy']}年{int(v['mm'])}月{int(v['dd'])}日"
    if v.get("amount", "").isdigit():
        v["amount_comma"] = f"{int(v['amount']):,}"
    v["source"] = source.name
    return v


def _item_value(key: str, header: list[str], row: list[str]) -> str:
    col = key.split(".", 1)[1]
    if col.isdigit():
        i = int(col) - 1
    else:
        names = [_norm(h) for h in header]
        i = names.index(_norm(col)) if _norm(col) in names else -1
    return row[i] if 0 <= i < len(row) else ""


def _fill(text: str, values: dict[str, str], header=None, row=None) -> str:
    def sub(m: re.Match) -> str:
        key = m[1]
        if key.startswith("items."):
            return _item_value(key, header or [], row or []) if row is not None else ""
        return values.get(key, "")
    return PLACEHOLDER.sub(sub, text)


def _has_items(text: str) -> bool:
    return any(k.startswith("items.") for k in PLACEHOLDER.findall(text or ""))


# ------------------------------------------------------------------ Excel


def fill_xlsx(template: Path, dest: Path, values: dict[str, str], header: list[str], rows: list[list[str]]) -> Path:
    from openpyxl import load_workbook
    from openpyxl.worksheet.cell_range import CellRange

    wb = load_workbook(template)
    for ws in wb.worksheets:
        item_row = next((r[0].row for r in ws.iter_rows() if any(_has_items(str(c.value or "")) for c in r)), None)
        if item_row is not None:
            tmpl = [(c.column, c.value, copy.copy(c._style)) for c in ws[item_row]]
            header = _header_or_template(header, [str(v or "") for _, v, _ in tmpl])
            extra = max(len(rows) - 1, 0)
            if extra:
                # 明細の行を増やす。下にある結合セルも一緒にずらす（合計欄などの形を崩さない）
                below = [mr for mr in ws.merged_cells.ranges if mr.min_row > item_row]
                for mr in below:
                    ws.merged_cells.ranges.remove(mr)
                heights = {r: ws.row_dimensions[r].height for r in range(item_row + 1, ws.max_row + 1)}
                ws.insert_rows(item_row + 1, extra)
                for r, h in heights.items():
                    ws.row_dimensions[r + extra].height = h
                for mr in below:
                    cr = CellRange(mr.coord)
                    cr.shift(0, extra)
                    ws.merge_cells(cr.coord)
            for i, row in enumerate(rows or [[]]):
                r = item_row + i
                ws.row_dimensions[r].height = ws.row_dimensions[item_row].height
                for col, value, style in tmpl:
                    cell = ws.cell(r, col)
                    cell._style = copy.copy(style)
                    if isinstance(value, str):
                        cell.value = _fill(value, values, header, row if rows else None)
                        cell.number_format = "@"
        for r in ws.iter_rows():
            for cell in r:
                if isinstance(cell.value, str) and PLACEHOLDER.search(cell.value):
                    cell.value = _fill(cell.value, values)
                    cell.number_format = "@"
    dest.parent.mkdir(parents=True, exist_ok=True)
    wb.save(dest)
    return dest


# ------------------------------------------------------------------ Word


def _fill_paragraph(p, values: dict[str, str], header=None, row=None) -> None:
    # 差し込み位置が複数の run に分かれていることがあるので、段落の文字をまとめて置き換え、最初の run の書式で書く
    if "{{" not in p.text:
        return
    new = _fill(p.text, values, header, row)
    runs = p.runs
    if not runs:
        return
    runs[0].text = new
    for r in runs[1:]:
        r.text = ""


def _cells_paragraphs(row_cells):
    for cell in row_cells:
        yield from cell.paragraphs


def fill_docx(template: Path, dest: Path, values: dict[str, str], header: list[str], rows: list[list[str]]) -> Path:
    from docx import Document

    d = Document(template)
    for table in d.tables:
        for tr in list(table.rows):
            if not any(_has_items(c.text) for c in tr.cells):
                continue
            tmpl = copy.deepcopy(tr._tr)
            header = _header_or_template(header, [c.text for c in tr.cells])
            anchor = tr._tr
            for i, row in enumerate(rows):
                new_tr = copy.deepcopy(tmpl)
                anchor.addnext(new_tr)
                anchor = new_tr
            # 追加した行に値を入れ、もとの（差し込み位置の）行は消す
            added = [r for r in table.rows if r._tr in set(_siblings_after(tr._tr, len(rows)))]
            for r, row in zip(added, rows):
                for p in _cells_paragraphs(r.cells):
                    _fill_paragraph(p, values, header, row)
            tr._tr.getparent().remove(tr._tr)
    parts = [d] + [s.header for s in d.sections] + [s.footer for s in d.sections]
    for part in parts:
        for p in part.paragraphs:
            _fill_paragraph(p, values)
        for table in part.tables:
            for r in table.rows:
                for p in _cells_paragraphs(r.cells):
                    _fill_paragraph(p, values)
    dest.parent.mkdir(parents=True, exist_ok=True)
    d.save(dest)
    return dest


def _siblings_after(tr, n: int) -> list:
    out, cur = [], tr
    for _ in range(n):
        cur = cur.getnext()
        out.append(cur)
    return out


def fill(template: Path, dest: Path, doc: PdfText, fields: dict[str, str]) -> Path:
    """様式（.xlsx / .docx）に、PDFから読み取った項目と明細を差し込む。"""
    if not template.exists():
        raise FormError(f"様式のファイルがありません: {template}")
    header, rows = items_from(doc)
    values = values_for(fields, doc.path)
    if template.suffix.lower() == ".xlsx":
        return fill_xlsx(template, dest.with_suffix(".xlsx"), values, header, rows)
    if template.suffix.lower() == ".docx":
        return fill_docx(template, dest.with_suffix(".docx"), values, header, rows)
    raise FormError(f"様式は .xlsx か .docx にしてください: {template.name}")
