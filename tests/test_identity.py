"""本人確認（yanua/identity.py）のテスト。3つのうち2つが同じ1人を指したときだけ確定する。"""

import sqlite3
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yanua import hr, identity  # noqa: E402

HR_DIR = ROOT / "demo" / "company" / "hr"


@pytest.fixture
def db(tmp_path):
    db = sqlite3.connect(tmp_path / "hr.db")
    hr.import_employees(db, HR_DIR / "hr_export_sample.csv", hr.load_mapping(HR_DIR / "mapping_sample.json"))
    yield db
    db.close()


def check(db, *said):
    return identity.check(db, list(said))


# デモの人事データ：10001 山田 太郎（経理部）、10002 佐藤 花子（総務部）、10003 鈴木 一郎（営業部）、
# 10004 高橋 美咲（営業部）、10005 田中 健（情報システム部）、10007 渡辺 剛（退職）


@pytest.mark.parametrize("said, no, matched", [
    # 3つとも合う。言う順番や言い方は問わない
    ("営業部の高橋美咲です。番号は10004です。", "10004", ["従業員番号", "名前", "部署"]),
    ("10004、高橋 美咲、営業", "10004", ["従業員番号", "名前", "部署"]),
    ("えっと、高橋です。営業部で、従業員番号は１０００４です", "10004", ["従業員番号", "名前", "部署"]),
    # 番号を覚えていない：名前と部署で確定（「総務」は総務部の呼び方）
    ("総務の佐藤花子です。番号はわかりません", "10002", ["名前", "部署"]),
    # 番号を聞き間違えた（10044）：名前と部署で確定する
    ("営業部の高橋美咲、10044です", "10004", ["名前", "部署"]),
    # 名前を聞き間違えた（シモダ→？）：番号と部署で確定する
    ("経理部のヤマダです。10001", "10001", ["従業員番号", "部署"]),
    # 姓だけでも、番号と合えば確定
    ("鈴木です。10003", "10003", ["従業員番号", "名前"]),
])
def test_two_of_three_confirms(db, said, no, matched):
    r = check(db, said)
    assert r.person and r.person["employee_no"] == no
    assert r.matched == matched


def test_misheard_number_is_noted(db):
    r = check(db, "営業部の高橋美咲、10044です")
    assert "聞き間違い" in r.note and r.declared.employee_no == "10044"


@pytest.mark.parametrize("said", [
    "高橋美咲です",              # 名前だけでは確定しない
    "10004です",                 # 番号だけでも確定しない
    "営業部です",                # 部署だけでも確定しない
    "私ですが",                  # 声で分かるでしょう、という電話
    "営業部の佐藤花子です",      # 名前は総務部の人、部署は営業部：1つずつしか合わない
    "10001、総務部です",         # 番号は経理部の人、部署は総務部
])
def test_one_clue_is_not_enough(db, said):
    assert check(db, said).person is None


def test_missing_items_are_asked_once(db):
    r = check(db, "高橋美咲です")
    assert r.missing == ["dept", "employee_no"]
    q = identity.follow_up(r.missing)
    assert "所属部署と従業員番号" in q and "わからない" in q
    # 2回目の返事と合わせて照合する
    r = check(db, "高橋美咲です", "営業です。番号はわかりません")
    assert r.person["employee_no"] == "10004"


def test_same_surname_in_same_department_is_not_confirmed(db):
    """同じ部署に同じ姓の人が2人いれば、姓と部署だけでは決めない。"""
    db.execute("INSERT INTO employees (employee_no, name, dept, dept_code, active, email, name_key, surname_key, dept_key) "
               "VALUES ('10010', '高橋 誠', '営業部', '400', 1, '', '高橋誠', '高橋', '営業部')")
    r = check(db, "営業部の高橋です")
    assert r.person is None and "複数" in r.note and "employee_no" in r.missing
    assert check(db, "営業部の高橋です", "10010").person["name"] == "高橋 誠"


def test_new_hire_not_in_hr_is_recorded(db):
    r = check(db, "総務部の新井ゆいです。今週入社したので番号はまだありません")
    assert r.person is None and "未登録" in r.note
    req = r.requester({"account": "soumu-common", "account_type": "shared"})
    assert req["verified"] == "照合できず"
    assert req["declared"]["name"] == "新井ゆい" and req["declared"]["dept"] == "総務部"
    assert "今週入社" in req["declared"]["said"]


def test_retired_employee_is_not_confirmed(db):
    assert check(db, "営業部の渡辺剛です。10007").person is None


@pytest.mark.parametrize("said, name", [
    ("営業部の高橋美咲です", "高橋 美咲"),
    ("総務部の新井と申します", "新井"),
    ("総務部の新井 ゆいです", "新井 ゆい"),
    ("えっと、イシモダです。情報システムの", "イシモダ"),
    ("プリンターから印刷できません", ""),    # 問い合わせの中身を名前として受け取らない
    ("わかりません", ""),
])
def test_declared_name(db, said, name):
    assert check(db, said).declared.name == name


def test_department_alias_collision(db):
    """「営業部」と「営業課」があるとき、「営業」だけではどちらとも決めない。"""
    db.execute("INSERT INTO employees (employee_no, name, dept, dept_code, active, email, name_key, surname_key, dept_key) "
               "VALUES ('10011', '小林 優', '営業課', '410', 1, '', '小林優', '小林', '営業課')")
    hr.refresh_departments(db)
    assert hr.dept_in_text(db, "営業の者です")[0] == ""
    assert hr.dept_in_text(db, "営業部の者です")[0] == "営業部"


def test_old_database_gets_key_columns(tmp_path):
    """照合用の列がない古い保存先にも、列と索引を足して、今ある行を埋める。"""
    db = sqlite3.connect(tmp_path / "old.db")
    db.execute("CREATE TABLE employees (employee_no TEXT PRIMARY KEY, name TEXT NOT NULL, dept TEXT NOT NULL, "
               "dept_code TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL, email TEXT NOT NULL DEFAULT '')")
    db.execute("INSERT INTO employees VALUES ('1', '石母田 光明', '情報システム部', '', 1, '')")
    db.commit()
    assert hr.find_in_text(db, "石母田さんをお願いします")[0]["employee_no"] == "1"


def test_lookup_uses_indexes(db):
    """名前・部署の照合は索引で引く（在籍者を全部読まない）。"""
    for col in ("name_key", "surname_key"):
        plan = " ".join(r[-1] for r in db.execute(
            f"EXPLAIN QUERY PLAN SELECT employee_no FROM employees WHERE {col} IN (?, ?) AND active = 1", ("a", "b")))
        assert "USING INDEX" in plan or "USING COVERING INDEX" in plan, plan


def test_fast_with_many_employees(tmp_path):
    """在籍20万人でも、1回の照合が速い（100万人の計測は eval/bench_identity.py）。"""
    db = sqlite3.connect(tmp_path / "big.db")
    hr.ensure_schema(db)
    surnames = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤"]
    chars = "一二三四五六七八九十春夏秋冬花月雪風空海山川森林光明美咲健誠"
    given = [a + b for a in chars for b in chars]   # 900通り：同姓同名がたくさんいる
    rows = []
    for i in range(200_000):
        name = f"{surnames[i % 10]} {given[(i // 10) % 900]}"
        dept = f"第{i % 300}営業部"
        rows.append((str(1_000_000 + i), name, dept, "", 1, "", *hr._keys(name, dept)))
    with db:
        db.executemany("INSERT INTO employees VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", rows)
    hr.refresh_departments(db)
    target = rows[123_456]
    said = f"{target[2]}の{target[1].replace(' ', '')}です。番号は{target[0]}"
    started = time.perf_counter()
    r = identity.check(db, [said])
    elapsed = time.perf_counter() - started
    assert r.person and r.person["employee_no"] == target[0]
    assert elapsed < 1.0, elapsed
