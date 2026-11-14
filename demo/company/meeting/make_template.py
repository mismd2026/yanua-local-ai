"""デモ用の議事録の様式（Word）を作る。差し込み位置は {{...}}（書き方は yanua/meeting/minutes.py）。

    python demo/company/meeting/make_template.py
"""

from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt

OUT = Path(__file__).resolve().parent / "templates" / "議事録.docx"

d = Document()
d.styles["Normal"].font.name = "Yu Gothic"
d.styles["Normal"].font.size = Pt(10.5)
h = d.add_paragraph()
r = h.add_run("議　事　録")
r.bold, r.font.size = True, Pt(18)
h.alignment = WD_ALIGN_PARAGRAPH.CENTER
info = d.add_table(rows=5, cols=2)
info.style = "Table Grid"
for i, (k, v) in enumerate([("会議名", "{{title}}"), ("日時", "{{date_jp}}"), ("場所", "{{place}}"), ("出席者", "{{attendees}}"), ("次回", "{{next}}")]):
    info.cell(i, 0).text, info.cell(i, 1).text = k, v
d.add_paragraph("")
d.add_heading("要約", level=2)
d.add_paragraph("{{summary}}")
d.add_heading("決まったこと", level=2)
d.add_paragraph("{{decisions}}")
d.add_heading("宿題", level=2)
t = d.add_table(rows=2, cols=3)
t.style = "Table Grid"
for j, k in enumerate(["担当", "内容", "期限"]):
    t.cell(0, j).text = k
    t.cell(0, j).paragraphs[0].runs[0].bold = True
    t.cell(1, j).text = "{{items." + k + "}}"
d.add_paragraph("")
note = d.add_paragraph("{{note}}（元の録画・録音：{{source}}）")
note.runs[0].font.size = Pt(8)
d.add_page_break()
d.add_heading("発言の記録", level=2)
p = d.add_paragraph("{{transcript}}")
p.runs[0].font.size = Pt(9)
OUT.parent.mkdir(parents=True, exist_ok=True)
d.save(OUT)
print(f"{OUT} を作りました")
