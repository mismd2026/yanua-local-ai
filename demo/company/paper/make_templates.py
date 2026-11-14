"""デモ用の会社の様式（テンプレート）を作る。差し込み位置は {{...}} で書く（書き方は yanua/paper/form.py）。

    python demo/company/paper/make_templates.py

実際の会社では、ふだん使っている Word / Excel の様式に {{...}} を書き込めばよい。
"""

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side

OUT = Path(__file__).resolve().parent / "templates"
THIN = Side(style="thin")
BOX = Border(left=THIN, right=THIN, top=THIN, bottom=THIN)
GRAY = PatternFill("solid", fgColor="DDDDDD")


def xlsx(path: Path, title: str, number_label: str, number_key: str, amount_label: str, columns: list[str]):
    wb = Workbook()
    ws = wb.active
    ws.title = title
    last = chr(ord("A") + len(columns) - 1)
    ws.merge_cells(f"A1:{last}1")
    ws["A1"] = "　".join(title)
    ws["A1"].font = Font(size=18, bold=True)
    ws["A1"].alignment = Alignment(horizontal="center")
    ws["A3"] = "{{to}} 御中"
    ws["A3"].font = Font(size=12, underline="single")
    prev = chr(ord(last) - 1)
    for r, (label, value) in enumerate([("日付", "{{date_jp}}"), (number_label, "{{" + number_key + "}}"),
                                        ("発行元", "{{company}}")], start=3):
        ws[f"{prev}{r}"] = label
        ws[f"{last}{r}"] = value
        ws[f"{prev}{r}"].fill = GRAY
        for c in (f"{prev}{r}", f"{last}{r}"):
            ws[c].border = BOX
    ws["A7"] = amount_label
    ws["A7"].font = Font(size=12, bold=True)
    ws.merge_cells(f"B7:{last}7")
    ws["B7"] = "¥{{amount_comma}}（税込）"
    ws["B7"].font = Font(size=14, bold=True)
    for j, name in enumerate(columns, start=1):
        h = ws.cell(9, j, name)
        h.fill, h.border, h.font = GRAY, BOX, Font(bold=True)
        h.alignment = Alignment(horizontal="center")
        cell = ws.cell(10, j, "{{items." + name + "}}")
        cell.border = BOX
        if j > 1:
            cell.alignment = Alignment(horizontal="right")
    ws.merge_cells(f"A12:{prev}12")
    ws["A12"] = "合計（税込）"
    ws["A12"].alignment = Alignment(horizontal="right")
    ws[f"{last}12"] = "{{amount_comma}}"
    ws[f"{last}12"].alignment = Alignment(horizontal="right")
    for c in [f"A12", f"{last}12"]:
        ws[c].border = BOX
        ws[c].font = Font(bold=True)
    ws["A14"] = "備考"
    ws.merge_cells(f"A15:{last}15")
    ws["A15"] = "元の書類：{{source}}（Yanuaで読み取り。内容は元の書類と照らして確認してください）"
    ws["A15"].font = Font(size=9, color="666666")
    ws.column_dimensions["A"].width = 34
    for j in range(2, len(columns) + 1):
        ws.column_dimensions[chr(ord("A") + j - 1)].width = 16
    ws.column_dimensions[last].width = 24   # 日付・番号・発行元の欄
    ws.page_setup.orientation = "portrait"
    ws.page_setup.fitToWidth = 1
    ws.sheet_properties.pageSetUpPr.fitToPage = True
    wb.save(path)


def docx(path: Path, title: str, number_label: str, number_key: str, columns: list[str], closing: str):
    d = Document()
    style = d.styles["Normal"]
    style.font.name = "Yu Gothic"
    style.font.size = Pt(10.5)
    h = d.add_paragraph()
    run = h.add_run("　".join(title))
    run.bold = True
    run.font.size = Pt(18)
    h.alignment = WD_ALIGN_PARAGRAPH.CENTER
    d.add_paragraph("{{to}} 御中")
    info = d.add_table(rows=3, cols=2)
    info.style = "Table Grid"
    for i, (label, value) in enumerate([("日付", "{{date_jp}}"), (number_label, "{{" + number_key + "}}"),
                                        ("発行元", "{{company}}")]):
        info.cell(i, 0).text = label
        info.cell(i, 1).text = value
    d.add_paragraph("")
    d.add_paragraph(closing)
    items = d.add_table(rows=2, cols=len(columns))
    items.style = "Table Grid"
    for j, name in enumerate(columns):
        items.cell(0, j).text = name
        items.cell(0, j).paragraphs[0].runs[0].bold = True
        items.cell(1, j).text = "{{items." + name + "}}"
    d.add_paragraph("")
    p = d.add_paragraph("元の書類：{{source}}（Yanuaで読み取り。内容は元の書類と照らして確認してください）")
    p.runs[0].font.size = Pt(8)
    d.save(path)


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    xlsx(OUT / "請求書.xlsx", "請求書", "請求番号", "invoice_no", "ご請求金額", ["品目", "数量", "単価", "金額"])
    xlsx(OUT / "見積書.xlsx", "見積書", "見積番号", "estimate_no", "お見積金額", ["項目", "数量", "金額"])
    docx(OUT / "請求書.docx", "請求書", "請求番号", "invoice_no", ["品目", "数量", "単価", "金額"], "下記のとおりご請求申し上げます。")
    docx(OUT / "納品書.docx", "納品書", "納品番号", "delivery_no", ["品名", "数量"], "下記のとおり納品いたしました。")
    print(f"{OUT} に様式を作りました")
