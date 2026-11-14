"""紙の書類のPDF（仕分け・変換）のテスト。デモの書類（demo/company/paper/inbox）を使う。"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yanua.paper import convert, form, rules, sort  # noqa: E402
from yanua.paper.extract import ocr_available, read_pdf  # noqa: E402

PAPER = ROOT / "demo" / "company" / "paper"
INBOX = PAPER / "inbox"
RULES = PAPER / "rules.json"


def has_jpn_ocr() -> bool:
    if not ocr_available():
        return False
    out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
    return "jpn" in out.stdout


needs_ocr = pytest.mark.skipif(not has_jpn_ocr(), reason="tesseract（日本語）がない環境")


class PickLLM:
    def __init__(self, answer):
        self.answer = answer
        self.prompts = []

    def chat(self, messages):
        self.prompts.append(messages[-1]["content"])
        return self.answer


@pytest.fixture
def rs():
    return rules.load_rules(RULES)


def by_name(plans):
    return {p.source.name: p for p in plans}


# ---------------------------------------------------------------- 仕分け


def test_text_pdfs_are_sorted_by_rules(rs):
    plans = by_name(sort.plan([INBOX / f"scan_000{i}.pdf" for i in (1, 2, 3, 5)], rs))
    assert str(plans["scan_0001.pdf"].dest) == "経理/請求書/2026/11/20261110_請求書_株式会社サンプル印刷_132000円.pdf"
    assert str(plans["scan_0002.pdf"].dest) == "総務/見積書/2026/20261105_見積書_有限会社テスト空調_385000円.pdf"
    assert str(plans["scan_0003.pdf"].dest) == "総務/議事録/2026/20261112_議事録_本社移転の定例会.pdf"
    assert str(plans["scan_0005.pdf"].dest) == "未分類/scan_0005.pdf" and plans["scan_0005.pdf"].by == "none"
    # 自社（デモ商事）は相手先にしない。独自の項目（請求番号）も取れる
    assert plans["scan_0001.pdf"].fields["to"] == "デモ商事株式会社"
    assert plans["scan_0001.pdf"].fields["invoice_no"] == "INV-2026-1110"


@needs_ocr
def test_scanned_pdf_is_read_with_ocr(rs):
    p = sort.plan([INBOX / "scan_0004.pdf"], rs)[0]
    assert p.ocr and p.rule == "納品書"
    assert str(p.dest) == "総務/納品書/2026/11/20261113_納品書_合同会社ミホン事務用品_DN-5521.pdf"


def test_execute_copies_logs_and_never_overwrites(rs, tmp_path):
    plans = sort.plan([INBOX / "scan_0001.pdf"], rs)
    out = tmp_path / "out"
    first = sort.execute(plans, out, text=True)
    second = sort.execute(plans, out)
    assert first[0].exists() and second[0].name.endswith("_2.pdf")      # 上書きしない
    assert (INBOX / "scan_0001.pdf").exists()                           # 既定はコピー
    assert "ご請求金額" in first[0].with_suffix(".txt").read_text(encoding="utf-8")
    log = (out / sort.LOG_NAME).read_text(encoding="utf-8-sig").splitlines()
    assert len(log) == 3 and "請求書" in log[1]


def test_field_values_cannot_escape_output_folder(tmp_path):
    rs = rules.RuleSet([rules.Rule("危険", any=["請求書"], fields={"x": r"請求番号\s*[:：]\s*(\S+)"},
                                   folder="../../{x}", filename="{x}/../../evil.pdf")])
    pdf = tmp_path / "in.pdf"
    shutil.copy(INBOX / "scan_0001.pdf", pdf)
    plans = sort.plan([pdf], rs)
    placed = sort.execute(plans, tmp_path / "out")
    assert all((tmp_path / "out").resolve() in p.resolve().parents for p in placed)
    assert plans[0].dest.name == "INV-2026-1110_.._.._evil.pdf" and ".." not in plans[0].dest.parts
    assert len(placed) == 1


def test_forced_rule_and_llm_fallback(rs):
    unknown = [INBOX / "scan_0005.pdf"]
    assert sort.plan(unknown, rs, forced="議事録")[0].rule == "議事録"
    llm = PickLLM("議事録")
    p = sort.plan(unknown, rs, llm=llm)[0]
    assert p.rule == "議事録" and p.by == "llm"
    assert "請求書、見積書、納品書、議事録" in llm.prompts[0]          # ルール名の中から選ばせる
    assert sort.plan(unknown, rs, llm=PickLLM("年賀状"))[0].rule == ""   # 選択肢以外の答えは使わない
    with pytest.raises(rules.RuleError):
        sort.plan(unknown, rs, forced="ない種類")


def test_none_keywords_exclude_rule(rs):
    m = rules.choose(rs, "御見積書\nご請求の前にお見積をお送りします")
    assert m.rule.name == "見積書"   # 請求書のルールは「見積」があると外れる


# ---------------------------------------------------------------- 項目の読み取り


def test_dates():
    assert rules.find_date("発行日：令和8年4月1日") == ("2026", "04", "01")
    assert rules.find_date("作成 2026/1/5") == ("2026", "01", "05")
    assert rules.find_date("備考 2025年12月1日\n発行日：2026年3月3日") == ("2026", "03", "03")   # 見出しつきの日付を優先
    assert rules.find_date("日付なし") is None


def test_render_fills_unknown_and_strips_path_chars():
    assert rules.render("{date}_{company}.pdf", {"company": "A/B:C"}) == "不明_A_B_C.pdf"


# ---------------------------------------------------------------- ルールの読み込み・下書き


def test_rule_file_errors(tmp_path):
    bad = tmp_path / "r.json"
    bad.write_text(json.dumps({"rules": [{"name": "x", "fields": {"a": "番号\\d+"}}]}), encoding="utf-8")
    with pytest.raises(rules.RuleError, match="かっこ"):
        rules.load_rules(bad)
    bad.write_text(json.dumps({"rules": [{"name": "x"}, {"name": "x"}]}), encoding="utf-8")
    with pytest.raises(rules.RuleError, match="同じ名前"):
        rules.load_rules(bad)


def test_draft_rule_from_sample():
    text = read_pdf(INBOX / "scan_0005.pdf").text
    draft = rules.draft_rule(text)
    assert draft["name"] == "案内" and draft["keywords"]["any"] == ["案内"]
    # LLMが挙げた言葉でも、書類に出てこない言葉は採らない
    draft = rules.draft_rule(text, llm=PickLLM("1. 休業日\n2. 夏季休暇\n3. 年末年始"))
    assert draft["keywords"]["any"] == ["案内", "休業日", "年末年始"]


def test_draft_can_be_added_and_used(tmp_path, rs):
    from yanua.__main__ import main

    path = tmp_path / "rules.json"
    shutil.copy(RULES, path)
    assert main(["pdf-rule-draft", str(INBOX / "scan_0005.pdf"), "--add-to", str(path)]) == 0
    plans = sort.plan([INBOX / "scan_0005.pdf"], rules.load_rules(path))
    assert plans[0].rule == "案内"
    assert main(["pdf-rule-draft", str(INBOX / "scan_0005.pdf"), "--add-to", str(path)]) == 1   # 同じ名前は足さない


# ---------------------------------------------------------------- 変換


def test_convert_to_docx(tmp_path):
    from docx import Document

    out = convert.to_docx(INBOX / "scan_0001.pdf", tmp_path / "a.docx")
    d = Document(out)
    texts = [p.text for p in d.paragraphs]
    assert "発行日：2026年11月10日" in texts and "請求番号：INV-2026-1110" in texts   # 1行ずつの項目はつなげない
    assert [c.text for c in d.tables[0].rows[1].cells] == ["会社案内パンフレット", "500", "200", "100,000"]
    assert not any("会社案内パンフレット" in t for t in texts)                          # 表の中身は本文に重ねない


def test_convert_to_xlsx(tmp_path):
    from openpyxl import load_workbook

    wb = load_workbook(convert.to_xlsx(INBOX / "scan_0001.pdf", tmp_path / "a.xlsx"))
    assert wb.sheetnames == ["p1_本文", "p1_表1"]
    rows = [[c.value for c in r] for r in wb["p1_表1"].iter_rows()]
    # 数字も文字列のまま（0 で始まるコードや郵便番号を崩さない）
    assert rows[1] == ["会社案内パンフレット", "500", "200", "100,000"]
    assert wb["p1_表1"]["B2"].number_format == "@"
    assert "ご請求金額 ¥132,000（税込）" in [r[0].value for r in wb["p1_本文"].iter_rows()]


@needs_ocr
def test_convert_scanned_pdf(tmp_path):
    from docx import Document
    from openpyxl import load_workbook

    d = Document(convert.to_docx(INBOX / "scan_0004.pdf", tmp_path / "s.docx"))
    assert any("納品" in p.text for p in d.paragraphs)
    assert load_workbook(convert.to_xlsx(INBOX / "scan_0004.pdf", tmp_path / "s.xlsx")).sheetnames == ["p1_OCR"]


def test_leading_zeros_are_kept(tmp_path):
    from openpyxl import load_workbook

    from yanua.paper.extract import Page, PdfText

    doc = PdfText(tmp_path / "x.pdf", [Page(1, "コード表 一覧です（テスト用の行）", tables=[[["コード", "郵便番号"], ["00123", "0010012"]]])])
    wb = load_workbook(convert.to_xlsx(doc.path, tmp_path / "z.xlsx", doc))
    assert [c.value for c in wb["p1_表1"][2]] == ["00123", "0010012"]


# ---------------------------------------------------------------- 様式への差し込み（フォームオーバーレイ）


def fill_for(rs, pdf, rule_name, fmt, dest):
    doc = read_pdf(INBOX / pdf, tables=True)
    rule = rs.get(rule_name)
    return form.fill(rule.templates[fmt], dest, doc, rules.find_fields(doc.text, rule, rs.self_names))


def test_form_xlsx_keeps_layout_and_fills_items(rs, tmp_path):
    from openpyxl import load_workbook

    ws = load_workbook(fill_for(rs, "scan_0001.pdf", "請求書", "xlsx", tmp_path / "a")).active
    cells = {c.coordinate: c.value for r in ws.iter_rows() for c in r if c.value}
    assert cells["A3"] == "デモ商事株式会社 御中" and cells["D3"] == "2026年11月10日" and cells["D4"] == "INV-2026-1110"
    assert cells["B7"] == "¥132,000（税込）"
    assert [ws.cell(10, j).value for j in range(1, 5)] == ["会社案内パンフレット", "500", "200", "100,000"]
    assert [ws.cell(11, j).value for j in range(1, 5)] == ["名刺印刷", "20", "1,000", "20,000"]
    # 明細が増えた分だけ、下の合計欄（結合セル）もずれる。合計などの行は明細に入れない
    assert "A13:C13" in {str(m) for m in ws.merged_cells.ranges} and cells["D13"] == "132,000"
    assert not any("{{" in str(v) for v in cells.values())
    assert "消費税" not in cells.values()


def test_form_docx_fills_table_rows(rs, tmp_path):
    from docx import Document

    d = Document(fill_for(rs, "scan_0001.pdf", "請求書", "docx", tmp_path / "a"))
    tables = [[[c.text for c in r.cells] for r in t.rows] for t in d.tables]
    assert tables[0][1] == ["請求番号", "INV-2026-1110"]
    assert tables[1] == [["品目", "数量", "単価", "金額"], ["会社案内パンフレット", "500", "200", "100,000"],
                         ["名刺印刷", "20", "1,000", "20,000"]]


@needs_ocr
def test_form_from_scanned_pdf_uses_template_column_order(rs, tmp_path):
    from docx import Document

    d = Document(fill_for(rs, "scan_0004.pdf", "納品書", "docx", tmp_path / "a"))
    rows = [[c.text for c in r.cells] for r in d.tables[1].rows]
    assert rows[1:] == [["コピー用紙 A4 (500枚)", "40"], ["トナー (黒)", "6"]]


def test_convert_with_rules(tmp_path):
    from yanua.__main__ import main

    assert main(["pdf-convert", str(INBOX / "scan_0002.pdf"), "--to", "xlsx", "--rules", str(RULES),
                 "--out", str(tmp_path / "e.xlsx")]) == 0
    from openpyxl import load_workbook
    assert load_workbook(tmp_path / "e.xlsx").active.title == "見積書"
    # 様式のない書類・どのルールにも当たらない書類は、様式なしで変換する
    assert main(["pdf-convert", str(INBOX / "scan_0005.pdf"), "--to", "docx", "--rules", str(RULES),
                 "--out", str(tmp_path / "u.docx")]) == 0


def test_sort_with_form(rs, tmp_path):
    plans = sort.plan([INBOX / "scan_0001.pdf", INBOX / "scan_0003.pdf"], rs)
    placed = sort.execute(plans, tmp_path / "out", form=True, rules=rs)
    invoice = placed[0]
    assert invoice.with_suffix(".xlsx").exists() and invoice.with_suffix(".docx").exists()
    assert not placed[1].with_suffix(".docx").exists()   # 議事録には様式がない


def test_template_format_must_be_known(tmp_path):
    bad = tmp_path / "r.json"
    bad.write_text(json.dumps({"rules": [{"name": "x", "templates": {"pdf": "a.pdf"}}]}), encoding="utf-8")
    with pytest.raises(rules.RuleError, match="xlsx か docx"):
        rules.load_rules(bad)
