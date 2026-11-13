"""人事データの取り込みと照合のテスト。"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yanua import hr  # noqa: E402

HR_DIR = ROOT / "demo" / "company" / "hr"
SAMPLE = HR_DIR / "hr_export_sample.csv"
MAPPING = HR_DIR / "mapping_sample.json"


@pytest.fixture
def db():
    conn = sqlite3.connect(":memory:")
    yield conn
    conn.close()


def test_import_keeps_only_needed_columns(db):
    report = hr.import_employees(db, SAMPLE, hr.load_mapping(MAPPING))
    assert report.errors == []
    assert report.active == 6 and report.inactive == 1
    # 本人確認に使わない列は、読み込んだ時点で捨てる
    assert set(report.dropped_columns) == {"氏名カナ", "生年月日", "入社日"}
    cols = [r[1] for r in db.execute("PRAGMA table_info(employees)")]
    assert cols == ["employee_no", "name", "dept", "dept_code", "active"]


def test_verify_needs_both_number_and_name(db):
    hr.import_employees(db, SAMPLE, hr.load_mapping(MAPPING))
    assert hr.verify(db, "10001", "山田太郎")["dept"] == "経理部"      # 空白の有無は問わない
    assert hr.verify(db, "１０００１", "山田 太郎") is not None           # 全角数字も同じとみなす
    assert hr.verify(db, "10001", "佐藤 花子") is None                  # 番号は合っていても氏名が違う
    assert hr.verify(db, "99999", "山田 太郎") is None


def test_retired_and_on_leave(db):
    hr.import_employees(db, SAMPLE, hr.load_mapping(MAPPING))
    assert hr.verify(db, "10007", "渡辺 剛") is None          # 退職者は一致しない
    assert hr.verify(db, "10006", "伊藤 直子") is not None     # 休職は在籍扱い（対応表の active_values）
    assert hr.lookup(db, "10007") is None


def test_import_replaces_everything(db, tmp_path):
    hr.import_employees(db, SAMPLE, hr.load_mapping(MAPPING))
    small = tmp_path / "small.csv"
    small.write_text("社員番号,氏名,所属,所属コード,在籍区分\n20001,新人 一郎,営業部,400,在籍\n", encoding="utf-8")
    hr.import_employees(db, small, hr.load_mapping(MAPPING))
    assert hr.verify(db, "10001", "山田 太郎") is None   # 前回の名簿は残らない
    assert hr.verify(db, "20001", "新人 一郎") is not None


def test_bad_rows_do_not_replace(db, tmp_path):
    hr.import_employees(db, SAMPLE, hr.load_mapping(MAPPING))
    bad = tmp_path / "bad.csv"
    bad.write_text("社員番号,氏名,所属,所属コード,在籍区分\n1,a,営業部,4,在籍\n1,b,営業部,4,在籍\n", encoding="utf-8")
    report = hr.import_employees(db, bad, hr.load_mapping(MAPPING))
    assert report.errors   # 番号の重複
    assert hr.verify(db, "10001", "山田 太郎") is not None   # 不備があれば入れ替えない


def test_missing_column_is_reported(db, tmp_path):
    other = tmp_path / "other.csv"
    other.write_text("ID,名前,部署\n1,a,営業部\n", encoding="utf-8")
    report = hr.import_employees(db, other, hr.load_mapping(MAPPING))
    assert report.errors and "社員番号" in report.errors[0]


def test_excel_and_json(db, tmp_path):
    from openpyxl import Workbook

    wb = Workbook()
    ws = wb.active
    ws.title = "社員一覧"
    ws.append(["社員番号", "氏名", "所属", "所属コード", "在籍区分", "住所"])
    ws.append([30001, "表計 花子", "総務部", 200, "在籍", "東京都"])
    xlsx = tmp_path / "hr.xlsx"
    wb.save(xlsx)
    mapping = {**hr.load_mapping(MAPPING), "sheet": "社員一覧"}
    report = hr.import_employees(db, xlsx, mapping)
    assert report.errors == [] and report.dropped_columns == ["住所"]
    assert hr.verify(db, "30001", "表計 花子")["dept"] == "総務部"

    js = tmp_path / "hr.json"
    js.write_text(json.dumps({"employees": [{"社員番号": "40001", "氏名": "情報 次郎", "所属": "人事部",
                                             "所属コード": "100", "在籍区分": "在籍"}]}, ensure_ascii=False), encoding="utf-8")
    assert hr.import_employees(db, js, hr.load_mapping(MAPPING)).errors == []
    assert hr.verify(db, "40001", "情報 次郎") is not None


def test_mapping_draft_from_headers():
    draft = hr.suggest_mapping(["社員番号", "氏名", "氏名カナ", "所属", "所属コード", "在籍区分", "生年月日"])
    assert draft["columns"] == {"employee_no": "社員番号", "name": "氏名", "dept": "所属",
                                "dept_code": "所属コード", "status": "在籍区分"}


def test_mapping_draft_asks_llm_only_for_unknown_and_only_headers():
    class FakeLLM:
        name = "fake"
        prompts = []

        def chat(self, messages):
            self.prompts.append(messages[-1]["content"])
            return '{"employee_no": "職番", "status": "存在しない列"}'

    llm = FakeLLM()
    draft = hr.suggest_mapping(["職番", "氏名", "部署"], llm)
    assert draft["columns"]["employee_no"] == "職番"
    assert "status" not in draft["columns"]          # 一覧にない列名は採らない
    assert len(llm.prompts) == 1 and "職番" in llm.prompts[0]
