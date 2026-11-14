"""デモ用の架空の書類（PDF）を作る。社名・人名・金額はすべて架空。

    python demo/company/paper/make_samples.py

inbox/ に、文字の入ったPDF（請求書・見積書・議事録・種類のわからない書類）と、
スキャンしたような画像だけのPDF（納品書）を作る。
"""

from io import BytesIO
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.units import mm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.cidfonts import UnicodeCIDFont
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
from reportlab.lib.styles import ParagraphStyle

OUT = Path(__file__).resolve().parent / "inbox"
# 日本語のフォントをPDFに埋め込む（埋め込まないと、見る環境やOCRの前の画像化で文字が消えることがある）
FONT_FILES = ["/usr/share/fonts/truetype/fonts-japanese-gothic.ttf", "/usr/share/fonts/opentype/ipafont-gothic/ipag.ttf",
              "/usr/share/fonts/truetype/takao-gothic/TakaoGothic.ttf", "C:/Windows/Fonts/msgothic.ttc"]
FONT = "JP"
for f in FONT_FILES:
    if Path(f).exists():
        from reportlab.pdfbase.ttfonts import TTFont

        pdfmetrics.registerFont(TTFont(FONT, f))
        break
else:   # 埋め込めるフォントがなければ、埋め込まないフォントで作る
    pdfmetrics.registerFont(UnicodeCIDFont("HeiseiKakuGo-W5"))
    FONT = "HeiseiKakuGo-W5"
TITLE = ParagraphStyle("t", fontName=FONT, fontSize=18, leading=24, alignment=1)
BODY = ParagraphStyle("b", fontName=FONT, fontSize=10.5, leading=16)


def table(rows, widths):
    t = Table(rows, colWidths=widths)
    t.setStyle(TableStyle([("FONTNAME", (0, 0), (-1, -1), FONT), ("FONTSIZE", (0, 0), (-1, -1), 10),
                           ("GRID", (0, 0), (-1, -1), 0.5, colors.black),
                           ("BACKGROUND", (0, 0), (-1, 0), colors.lightgrey), ("ALIGN", (1, 1), (-1, -1), "RIGHT")]))
    return t


def build(path, story):
    SimpleDocTemplate(str(path), pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm).build(story)


def p(text, style=BODY):
    return Paragraph(text, style)


def scanned(path, story):
    """スキャンしたPDFのかわり：一度画像にして、画像だけのPDFにする（文字は入っていない）。"""
    from pdf2image import convert_from_bytes
    import img2pdf

    buf = BytesIO()
    SimpleDocTemplate(buf, pagesize=A4, leftMargin=20 * mm, rightMargin=20 * mm).build(story)
    img = convert_from_bytes(buf.getvalue(), dpi=200)[0].convert("L")
    out = BytesIO()
    img.save(out, format="PNG", dpi=(200, 200))
    path.write_bytes(img2pdf.convert(out.getvalue()))


def invoice(path):
    build(path, [p("請求書", TITLE), Spacer(1, 8 * mm),
                 p("デモ商事株式会社 御中"), p("発行日：2026年11月10日"), p("請求番号：INV-2026-1110"), Spacer(1, 4 * mm),
                 p("株式会社サンプル印刷"), p("東京都架空区見本町4-5-6"), Spacer(1, 6 * mm),
                 p("下記のとおりご請求申し上げます。"), p("ご請求金額　¥132,000（税込）"), Spacer(1, 4 * mm),
                 table([["品目", "数量", "単価", "金額"], ["会社案内パンフレット", "500", "200", "100,000"],
                        ["名刺印刷", "20", "1,000", "20,000"], ["消費税", "", "", "12,000"], ["合計", "", "", "132,000"]],
                       [70 * mm, 25 * mm, 30 * mm, 35 * mm]),
                 Spacer(1, 6 * mm), p("お支払い期限：2026年12月25日")])


def delivery(path):
    scanned(path, [p("納品書", TITLE), Spacer(1, 8 * mm),
                   p("デモ商事株式会社 御中"), p("納品日：2026年11月13日"), p("納品番号：DN-5521"), Spacer(1, 4 * mm),
                   p("合同会社ミホン事務用品"), Spacer(1, 6 * mm), p("下記のとおり納品いたしました。"), Spacer(1, 4 * mm),
                   table([["品名", "数量"], ["コピー用紙 A4（500枚）", "40"], ["トナー（黒）", "6"]], [110 * mm, 40 * mm])])


def estimate(path):
    build(path, [p("御見積書", TITLE), Spacer(1, 8 * mm), p("デモ商事株式会社 御中"), p("見積日：2026年11月5日"),
                 p("有限会社テスト空調"), Spacer(1, 6 * mm), p("お見積金額　¥385,000（税込）"), Spacer(1, 4 * mm),
                 table([["項目", "数量", "金額"], ["本社3階 空調機の点検", "1式", "150,000"],
                        ["フィルター交換", "12", "200,000"], ["消費税", "", "35,000"], ["合計", "", "385,000"]],
                       [90 * mm, 30 * mm, 40 * mm]),
                 Spacer(1, 6 * mm), p("有効期限：見積日から30日")])


def minutes(path):
    build(path, [p("議事録", TITLE), Spacer(1, 8 * mm), p("会議名：本社移転の定例会（第3回）"),
                 p("開催日：2026年11月12日　14:00〜15:00"), p("場所：5階 第1会議室"),
                 p("出席者：総務部 佐藤、情報システム部 田中、経理部 山田"), Spacer(1, 6 * mm),
                 p("1. 移転先のネットワーク工事は12月第2週に行う。"), p("2. 複合機の移設は、リース会社に見積を依頼する。"),
                 p("3. 次回は11月26日に開催する。"), Spacer(1, 4 * mm),
                 p("宿題：田中 ― 工事の当日の作業手順をまとめる。佐藤 ― リース会社へ連絡する。")])


def unknown(path):
    build(path, [p("ご案内", TITLE), Spacer(1, 8 * mm), p("年末年始の休業日について"), Spacer(1, 4 * mm),
                 p("12月29日から1月3日まで、休業いたします。ご不便をおかけしますが、よろしくお願いいたします。")])


if __name__ == "__main__":
    OUT.mkdir(parents=True, exist_ok=True)
    invoice(OUT / "scan_0001.pdf")
    estimate(OUT / "scan_0002.pdf")
    minutes(OUT / "scan_0003.pdf")
    delivery(OUT / "scan_0004.pdf")
    unknown(OUT / "scan_0005.pdf")
    print(f"{OUT} にデモの書類を作りました")
