"""S-51 人事の画面（取り込み：試し → 確定 → 前に戻す、一覧）と、休職・退職の扱い（K-054）のテスト。

- 列の対応づけ（名前を付けて保存して選べる）・在籍状態の対応（在職・休業・退社 → 在籍・休職・退職）
- 誤り（社員番号の重複・必要な列がない・知らない在籍状態）は行番号つき。誤りがあれば確定できない
- 部署が変わると、決まりで入るグループが入れ替わる（試しの結果にグループごとの人数）
- 退職した人は、手で入れたグループでも数えない。在籍に戻っても自動では戻さない。監査ログに残す
- 休職の設定（ログインできる・できない、権限：そのまま・社員の役割だけ）
- 前の取り込みに戻す（控えは3回分）・途中で失敗したら前のまま
- ほかの列（住所・給与など）は読まない
"""

import json
import sqlite3
import sys
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from documents_snapshot import build_settings  # noqa: E402
from test_api import H, FakeLLM, login  # noqa: E402

from yanua import access, hr, hrimport  # noqa: E402
from yanua.api import create_app  # noqa: E402

HEAD = "職員コード,漢字氏名,ふりがな,所属名,所属CD,在職区分,メール,住所,給与"
DEMO_ROWS = [
    ("10001", "山田 太郎", "ヤマダ タロウ", "経理部", "300", "在職", "yamada.t@example.co.jp"),
    ("10002", "佐藤 花子", "サトウ ハナコ", "総務部", "200", "在職", "sato.h@example.co.jp"),
    ("10003", "鈴木 一郎", "スズキ イチロウ", "営業部", "400", "在職", "suzuki.i@example.co.jp"),
    ("10004", "高橋 美咲", "タカハシ ミサキ", "営業部", "400", "在職", "takahashi.m@example.co.jp"),
    ("10005", "田中 健", "タナカ ケン", "情報システム部", "500", "在職", "tanaka.k@example.co.jp"),
    ("10006", "伊藤 直子", "イトウ ナオコ", "人事部", "100", "休業", "ito.n@example.co.jp"),
    ("10007", "渡辺 剛", "ワタナベ ツヨシ", "営業部", "400", "退社", "watanabe.t@example.co.jp"),
]
MAPPING = {"columns": {"employee_no": "職員コード", "name": "漢字氏名", "kana": "ふりがな", "dept": "所属名", "dept_code": "所属CD",
                       "status": "在職区分", "email": "メール"},
           "status_values": {"在職": "在籍", "休業": "休職", "退社": "退職"}}
SECRET = "東京都千代田区秘密町1-2-3"


def csv_text(rows, extra=True) -> bytes:
    lines = [HEAD]
    for r in rows:
        lines.append(",".join(r) + (f",{SECRET},9876543" if extra else ","))
    return ("\n".join(lines) + "\n").encode("utf-8")


def change(rows, no, **kw):
    out = []
    for r in rows:
        if r[0] == no:
            d = dict(zip(("no", "name", "kana", "dept", "code", "status", "email"), r))
            d.update(kw)
            r = (d["no"], d["name"], d["kana"], d["dept"], d["code"], d["status"], d["email"])
        out.append(r)
    return out


@pytest.fixture
def settings(tmp_path):
    return build_settings(tmp_path)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])) as c:
        login(c, "10006")                    # 人事（hr.view・hr.import）
        yield c


def wait_job(c, job_id) -> dict:
    end = time.monotonic() + 60
    while time.monotonic() < end:
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in ("待ち", "実行中"):
            return job
        time.sleep(0.05)
    raise AssertionError("ジョブが終わりません")


def upload(c, body: bytes, name="hr.csv") -> str:
    r = c.post(f"/api/uploads?kind=hr&name={name}", content=body, headers={**H, "Content-Type": "application/octet-stream"})
    assert r.status_code == 201, r.text
    return r.json()["file_id"]


def run(c, path: str, body: dict) -> dict:
    r = c.post(path, json=body, headers=H)
    assert r.status_code == 202, r.text
    job = wait_job(c, r.json()["job_id"])
    assert job["status"] == "完了", job
    return job["result"]


def preview(c, rows_or_body, mapping=MAPPING, **kw) -> dict:
    fid = upload(c, rows_or_body if isinstance(rows_or_body, bytes) else csv_text(rows_or_body))
    return run(c, "/api/hr/preview", {"file_id": fid, "mapping": mapping, **kw})


def commit(c, rows_or_body, mapping=MAPPING, **kw) -> dict:
    fid = upload(c, rows_or_body if isinstance(rows_or_body, bytes) else csv_text(rows_or_body))
    return run(c, "/api/hr/imports", {"file_id": fid, "mapping": mapping, **kw})


def audit(c, action) -> list[dict]:
    login(c, "10005")
    entries = c.get("/api/audit", params={"action": action}).json()["entries"]
    login(c, "10006")
    return entries


def engine(settings, leave="そのまま"):
    return access.Engine(lambda: sqlite3.connect(str(settings.db_path)), leave_policy=lambda: leave)


# ---------------------------------------------------------------- 列の対応づけ・在籍状態の対応


def test_mapping_is_saved_by_name_and_maps_columns_and_statuses(client):
    fid = upload(client, csv_text(DEMO_ROWS))
    r = client.post("/api/hr/inspect", json={"file_id": fid, "status_column": "在職区分"}, headers=H).json()
    assert r["headers"] == HEAD.split(",")
    assert r["draft"]["employee_no"] == "職員コード" and r["draft"]["kana"] == "ふりがな" and r["draft"]["status"] == "在職区分"
    assert {v["value"]: v["guess"] for v in r["status_values"]} == {"在職": "在籍", "休業": "休職", "退社": "退職"}
    # 名前を付けて保存すると、次から選べる（既定のデモの対応づけのほかに）
    assert client.put("/api/hr/mappings/うちの人事アプリ", json=MAPPING, headers=H).status_code == 200
    names = client.get("/api/hr/status").json()["mappings"]
    assert set(names) == {"デモの人事アプリ", "うちの人事アプリ"} and names["うちの人事アプリ"]["columns"]["name"] == "漢字氏名"
    assert "人事データの対応づけ「うちの人事アプリ」を足した" in audit(client, "hr.mappings.save")[0]["detail"]
    client.put("/api/hr/mappings/うちの人事アプリ", json=MAPPING, headers=H)
    assert "「うちの人事アプリ」を直した" in audit(client, "hr.mappings.save")[0]["detail"]
    # 誤りのある対応づけは保存しない
    bad = {"columns": {"employee_no": "職員コード", "name": "漢字氏名"}, "status_values": {"在職": "出向"}}
    r = client.put("/api/hr/mappings/だめ", json=bad, headers=H)
    assert r.status_code == 400 and {p["where"] for p in r.json()["error"]["details"]} >= {"columns.dept", "status_values.在職"}

    p = run(client, "/api/hr/preview", {"file_id": fid, "mapping": MAPPING})
    assert p["ok"] and p["counts"] == {"在籍": 5, "休職": 1, "退職": 1}
    assert p["ignored_columns"] == ["住所", "給与"]
    assert p["changed"]["count"] == 7 and p["changed"]["by_field"]["よみ"] == 7   # よみが入る（前の取り込みは よみなし）
    assert p["added"]["count"] == 0 and p["removed"]["count"] == 0


def test_errors_have_line_numbers_and_block_commit(client, settings):
    rows = DEMO_ROWS + [("10003", "重複 太郎", "", "営業部", "400", "在職", "")]
    rows = change(rows, "10004", status="出向")
    p = preview(client, rows)
    assert not p["ok"] and p["error_count"] == 2
    assert {(e["line"], e["message"]) for e in p["errors"]} == {
        (5, "在籍状態「出向」を、在籍・休職・退職のどれに当てるか決めていません"),
        (9, "社員番号 10003 が重複しています（4行目と同じ）")}
    r = commit(client, rows)
    assert r["committed"] is False and r["reason"] == "誤りがあるので確定しません"
    # 必要な列がない
    p = preview(client, b"ID,name\n1,a\n", {"columns": {"employee_no": "ID", "name": "name", "dept": "部署"}})
    assert not p["ok"] and p["errors"][0]["message"].startswith("対応づけた列がファイルにありません：部署（部署）")
    # 空の欄
    p = preview(client, change(DEMO_ROWS, "10002", dept=""))
    assert p["errors"] == [{"line": 3, "message": "部署が空です"}]
    db = sqlite3.connect(settings.db_path)
    assert db.execute("SELECT status FROM employees WHERE employee_no = '10004'").fetchone() == ("在籍",)   # 変えていない
    assert audit(client, "hr.import")[0]["result"] == "失敗"


def test_other_columns_are_not_read(client, settings):
    r = commit(client, DEMO_ROWS)
    assert r["committed"] and r["ignored_columns"] == ["住所", "給与"]
    blob = Path(settings.db_path).read_bytes()
    assert SECRET.encode() not in blob and "9876543".encode() not in blob
    # 読む関数も、割り当てた列の値しか返さない
    path = Path(settings.work_dir) / "x.csv"
    path.write_bytes(csv_text(DEMO_ROWS))
    got = list(hrimport._iter(path, ["職員コード", "漢字氏名"], None))
    assert got[0] == (2, {"職員コード": "10001", "漢字氏名": "山田 太郎"})
    assert all(SECRET not in json.dumps(v, ensure_ascii=False) for _, v in got)
    assert SECRET not in json.dumps(audit(client, "hr.import"), ensure_ascii=False)


# ---------------------------------------------------------------- 権限への効き方


def test_department_change_swaps_rule_groups_and_audit_has_numbers_only(client, settings):
    commit(client, DEMO_ROWS)
    assert "paper" not in engine(settings).permissions("10004", "営業部")
    moved = change(DEMO_ROWS, "10004", dept="総務部", code="200")
    p = preview(client, moved)
    assert p["changed"]["first"][0]["changes"] == ["部署 営業部→総務部", "部署コード"]
    assert {g["name"]: (g["join"], g["leave"]) for g in p["groups"]} == {"総務": (1, 0)}
    r = commit(client, moved)
    assert r["committed"]
    login(client, "10004")
    me = client.get("/api/me").json()
    assert "総務" in me["roles"] and "paper" in me["permissions"]
    entry = audit(client, "hr.import")[0]
    assert entry["detail"] == "一部のファイル：入る 0人・変わる 1人（部署 1・部署コード 1）・いなくなる 0人。社員番号：10004"
    assert "高橋" not in entry["detail"] and "@" not in entry["detail"]
    db = sqlite3.connect(settings.db_path)
    changed = dict(db.execute("SELECT employee_no, changed_at FROM employees"))
    assert changed["10004"] == settings.today.isoformat() if settings.today else changed["10004"]
    login(client, "10006")
    lst = client.get("/api/hr/employees", params={"q": "高橋"}).json()
    assert lst["employees"] == [{"employee_no": "10004", "name": "高橋 美咲", "dept": "総務部", "status": "在籍",
                                 "changed_at": changed["10004"]}]


def test_retired_people_are_not_counted_in_manual_groups_and_not_restored(client, settings):
    commit(client, DEMO_ROWS)
    login(client, "10005")
    assert client.post("/api/admin/access/groups/G-admin/members", json={"account_id": "10003"}, headers=H).status_code == 201
    assert "roles.view" in engine(settings).permissions("10003", "営業部")
    login(client, "10006")
    retired = change(DEMO_ROWS, "10003", status="退社")
    p = preview(client, retired)
    assert {g["name"]: (g["join"], g["leave"]) for g in p["groups"]} == {"社員": (0, 1), "管理者": (0, 1)}
    assert p["retired_members"] == 1
    assert commit(client, retired)["committed"]
    db = sqlite3.connect(settings.db_path)
    assert db.execute("SELECT COUNT(*) FROM group_members WHERE account_id = '10003'").fetchone()[0] == 0
    entry = audit(client, "roles.member.remove")[0]
    assert entry["target"] == "10003" and "「管理者」" in entry["detail"] and "自動では戻さない" in entry["detail"]
    # 在籍に戻っても、手で入れたグループには戻らない
    assert commit(client, DEMO_ROWS)["committed"]
    assert "roles.view" not in engine(settings).permissions("10003", "営業部")
    # 判定でも除く：退職の人の手で入れたメンバーの行が残っていても数えない
    with db:
        db.execute("UPDATE employees SET status = '退職', active = 0 WHERE employee_no = '10003'")
        db.execute("INSERT INTO group_members VALUES ('G-admin', '10003', NULL, 't', 't')")
        access._bump(db)
    assert engine(settings).permissions("10003", "営業部") == []


def test_cannot_retire_the_last_roles_assign_holder(client):
    commit(client, DEMO_ROWS)
    p = preview(client, change(DEMO_ROWS, "10005", status="退社"))
    assert p["lockout"] and not p["ok"]
    r = commit(client, change(DEMO_ROWS, "10005", status="退社"))
    assert not r["committed"] and "roles.assign" in r["reason"]


def test_on_leave_settings(client, settings):
    commit(client, DEMO_ROWS)                            # 10006 は休業 → 休職
    assert "hr.import" in engine(settings).permissions("10006", "人事部")
    login(client, "10005")
    r = client.put("/api/admin/settings/hr", json={"values": {"on_leave_login": True, "on_leave_permissions": "社員の役割だけ"}},
                   headers=H)
    assert r.status_code == 200, r.text
    login(client, "10006")
    me = client.get("/api/me").json()
    assert "hr.import" not in me["permissions"] and "ask" in me["permissions"]
    assert client.get("/api/hr/employees").status_code == 403
    login(client, "10005")
    client.put("/api/admin/settings/hr", json={"values": {"on_leave_login": False, "on_leave_permissions": "そのまま"}}, headers=H)
    r = client.post("/api/auth/dev-login", json={"employee_no": "10006"}, headers=H)
    assert r.status_code == 401
    login(client, "10002")                               # 在籍の人はそのまま
    # 設定は版と監査ログに残る
    login(client, "10005")
    assert "休職中の人のログイン できる→できない" in client.get("/api/audit", params={"action": "settings.hr"}).json()["entries"][0]["detail"]


def test_on_leave_session_is_ended_when_login_is_turned_off(client, settings):
    commit(client, DEMO_ROWS)
    other = TestClient(client.app)
    login(other, "10006")
    assert other.get("/api/me").status_code == 200
    login(client, "10005")
    client.put("/api/admin/settings/hr", json={"values": {"on_leave_login": False, "on_leave_permissions": "そのまま"}}, headers=H)
    assert other.get("/api/me").status_code == 401


# ---------------------------------------------------------------- 取り込みの種類・使わない列・ファイル名


SALES = [r for r in DEMO_ROWS if r[3] == "営業部"]


def test_partial_file_leaves_everyone_else_unchanged(client, settings):
    commit(client, DEMO_ROWS)
    db = sqlite3.connect(settings.db_path)
    before = db.execute("SELECT * FROM employees WHERE dept <> '営業部' ORDER BY employee_no").fetchall()
    p = preview(client, change(SALES, "10004", name="高橋 美咲子"))                # 種類を言わなければ「一部のファイル」
    assert p["scope"] == "partial" and p["scope_label"] == "一部のファイル"
    assert (p["added"]["count"], p["changed"]["count"], p["removed"]["count"]) == (0, 1, 0) and p["groups"] == []
    assert p["removal_warning"] is None
    r = commit(client, change(SALES, "10004", name="高橋 美咲子"))
    assert r["committed"]
    assert db.execute("SELECT * FROM employees WHERE dept <> '営業部' ORDER BY employee_no").fetchall() == before
    assert db.execute("SELECT name FROM employees WHERE employee_no = '10004'").fetchone() == ("高橋 美咲子",)
    assert audit(client, "hr.import")[0]["detail"].startswith("一部のファイル：入る 0人・変わる 1人（名前 1）・いなくなる 0人")
    assert client.get("/api/hr/status").json()["history"][0]["scope"] == "一部のファイル"


def test_whole_file_asks_again_when_more_than_ten_percent_leave(client, settings):
    commit(client, DEMO_ROWS)
    p = preview(client, SALES, scope="all")
    assert p["scope_label"] == "全員のファイル" and p["removed"]["count"] == 4
    assert p["removal_warning"] == {"count": 4, "percent": 57.1, "message": "いなくなる人が 4人（全体の 57.1%）います"}
    login(client, "10005")                                    # 締め出しの決まりにかからないよう、ここだけは管理者を残す
    login(client, "10006")
    rows = SALES + [r for r in DEMO_ROWS if r[0] in ("10005", "10006")]
    r = commit(client, rows, scope="all")                    # 確かめずに押すと、確定しない
    assert not r["committed"] and r["reason"] == "いなくなる人が 2人（全体の 28.6%）います。確かめてから、もう一度確定してください"
    db = sqlite3.connect(settings.db_path)
    assert db.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 7
    r = commit(client, rows, scope="all", confirm_removal=True)
    assert r["committed"] and db.execute("SELECT COUNT(*) FROM employees").fetchone()[0] == 5
    assert audit(client, "hr.import")[0]["detail"].startswith("全員のファイル：入る 0人・変わる 0人・いなくなる 2人")
    # 10% 以下なら、そのまま確定できる
    assert commit(client, rows[:-1] + [r for r in DEMO_ROWS if r[0] == "10006"], scope="all")["committed"]


def test_unused_optional_columns_keep_current_values(client, settings):
    commit(client, DEMO_ROWS)
    db = sqlite3.connect(settings.db_path)
    keep = dict(((r[0], r[1:]) for r in db.execute("SELECT employee_no, kana, dept_code, email, status FROM employees")))
    slim = {"columns": {"employee_no": "職員コード", "name": "漢字氏名", "dept": "所属名"}}   # よみ・部署コード・メール・在籍状態は使わない
    rows = change(DEMO_ROWS, "10002", name="佐藤 花") + [("20001", "新人 一郎", "シンジン", "営業部", "400", "在職", "new@example.co.jp")]
    p = preview(client, rows, slim)
    assert (p["added"]["count"], p["changed"]["count"]) == (1, 1)
    assert p["changed"]["first"][0]["changes"] == ["名前 佐藤 花子→佐藤 花"]
    assert commit(client, rows, slim)["committed"]
    now = dict(((r[0], r[1:]) for r in db.execute("SELECT employee_no, kana, dept_code, email, status FROM employees")))
    assert all(now[no] == v for no, v in keep.items())                     # 今いる人は、使わない列の値をそのまま
    assert now["20001"] == ("", "", "", "在籍")                            # 新しく入る人だけ空
    assert hr.lookup(db, "10007") is None                                  # 退職のまま（在籍状態を使わなくても変えない）


def test_upload_name_is_shown_not_the_server_name(client):
    fid = upload(client, csv_text(DEMO_ROWS), name="人事出力_11月.csv")
    run(client, "/api/hr/imports", {"file_id": fid, "mapping": MAPPING})
    st = client.get("/api/hr/status").json()
    assert st["last"]["source"] == "人事出力_11月.csv" and st["history"][0]["source"] == "人事出力_11月.csv"
    assert audit(client, "hr.import")[0]["target"] == "人事出力_11月.csv"
    commit(client, DEMO_ROWS)
    assert client.get("/api/hr/status").json()["backups"][0]["source"] == "人事出力_11月.csv"


def test_demo_data_from_before_marks_10006_on_leave_after_reimport(settings):
    """前の作りは在籍と休職を分けていなかった（10006 が「在籍」）。デモの CSV を取り込み直すと「休職」になる。"""
    db = sqlite3.connect(settings.db_path)
    with db:
        db.execute("UPDATE employees SET status = '在籍' WHERE employee_no = '10006'")   # 前の作りから移したときの形
    m = hr.load_mapping(ROOT / "demo" / "company" / "hr" / "mapping_sample.json")
    r = hr.import_employees(db, ROOT / "demo" / "company" / "hr" / "hr_export_sample.csv", m)
    assert r.errors == [] and hr.status_of(db, "10006") == "休職"
    assert r.result.preview.diff["changed"]["first"][-1]["changes"][0] == "在籍状態 在籍→休職"


# ---------------------------------------------------------------- 前に戻す・まとめて1回


def test_restore_previous_import_with_preview_and_keep_three(client, settings):
    commit(client, DEMO_ROWS)
    commit(client, change(DEMO_ROWS, "10001", name="山田 太一"))
    p = run(client, "/api/hr/restore/preview", {})
    assert p["changed"]["first"][0]["changes"] == ["名前 山田 太一→山田 太郎"]
    db = sqlite3.connect(settings.db_path)
    assert db.execute("SELECT name FROM employees WHERE employee_no = '10001'").fetchone() == ("山田 太一",)   # 試しは保存しない
    r = run(client, "/api/hr/restore", {})
    assert r["committed"] and r["kind"] == "戻す"
    assert db.execute("SELECT name FROM employees WHERE employee_no = '10001'").fetchone() == ("山田 太郎",)
    assert audit(client, "hr.restore")[0]["detail"].startswith("全員のファイル：入る 0人・変わる 1人（名前 1）")   # 控えは全員のデータ
    # 戻したことも、また戻せる
    run(client, "/api/hr/restore", {})
    assert db.execute("SELECT name FROM employees WHERE employee_no = '10001'").fetchone() == ("山田 太一",)
    for _ in range(3):
        commit(client, DEMO_ROWS)
    st = client.get("/api/hr/status").json()
    assert len(st["backups"]) == 3 and st["history"][0]["kind"] == "取り込み"
    tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type = 'table'") if r[0].startswith("hr_backup_")}
    assert tables == {f"hr_backup_{b['backup_id']}" for b in st["backups"]}


def test_commit_is_all_or_nothing(settings, tmp_path):
    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])):
        pass
    path = tmp_path / "x.csv"
    path.write_bytes(csv_text(change(DEMO_ROWS, "10001", dept="総務部")))
    db = sqlite3.connect(settings.db_path)
    before = db.execute("SELECT * FROM employees ORDER BY employee_no").fetchall()
    backups = hrimport.backups(db)
    for stage in ("backup", "replace", "finish"):
        def boom(at, stage=stage):
            if at == stage:
                raise RuntimeError("止めた")
        with pytest.raises(RuntimeError):
            hrimport.commit(db, path, MAPPING, "t", fail_hook=boom)
        assert db.execute("SELECT * FROM employees ORDER BY employee_no").fetchall() == before, stage
        assert hrimport.backups(db) == backups
    assert hrimport.commit(db, path, MAPPING, "t").committed


# ---------------------------------------------------------------- 一覧（hr.view の対象の部署だけ）


def test_list_shows_only_departments_in_hr_view_scope(client):
    commit(client, DEMO_ROWS)
    all_ = client.get("/api/hr/employees").json()
    assert all_["total"] == 7 and set(all_["employees"][0]) == {"employee_no", "name", "dept", "status", "changed_at"}
    assert [e["employee_no"] for e in client.get("/api/hr/employees", params={"dept": "営業部"}).json()["employees"]] == \
        ["10003", "10004", "10007"]
    assert client.get("/api/hr/employees", params={"q": "1000"}).json()["total"] == 7
    # 営業部だけを見られる人（hr.view × 営業部）
    login(client, "10005")
    roles = {r["name"]: r["role_id"] for r in client.get("/api/admin/access").json()["roles"]}
    r = client.post("/api/admin/access/roles", json={"name": "人事を見る", "permissions": ["hr.view"]}, headers=H)
    role = next(x["role_id"] for x in r.json()["roles"] if x["name"] == "人事を見る")
    r = client.post("/api/admin/access/groups", json={"name": "営業の人事"}, headers=H)
    gid = next(g["group_id"] for g in r.json()["groups"] if g["name"] == "営業の人事")
    client.post(f"/api/admin/access/groups/{gid}/members", json={"account_id": "10003"}, headers=H)
    assert client.post(f"/api/admin/access/groups/{gid}/grants", json={"role_id": role, "target_id": "dept:営業部"},
                       headers=H).status_code == 201
    assert roles
    login(client, "10003")
    v = client.get("/api/hr/employees").json()
    assert {e["dept"] for e in v["employees"]} == {"営業部"} and v["departments"] == ["営業部"]
    assert client.get("/api/hr/employees", params={"dept": "経理部"}).json()["employees"] == []
    assert client.post("/api/hr/preview", json={"file_id": "x", "mapping": MAPPING}, headers=H).status_code == 403
    login(client, "10004")
    assert client.get("/api/hr/employees").status_code == 403


# ---------------------------------------------------------------- 起動は止めない・コマンド


def test_broken_mapping_file_does_not_stop_startup(settings):
    (settings.settings_dir).mkdir(parents=True, exist_ok=True)
    (settings.settings_dir / "hr_mappings.json").write_text("{こわれた", encoding="utf-8")
    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])) as c:
        login(c, "10006")
        st = c.get("/api/hr/status").json()
        assert st["mappings"] == {} and "JSON として読めません" in st["problems"][0]["message"]
        assert c.get("/api/hr/employees").json()["total"] == 7          # 今の人事データのまま動く


def test_command_uses_the_same_process_and_audits(settings, tmp_path):
    from yanua.__main__ import main

    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])):
        pass
    src = tmp_path / "hr.csv"
    src.write_bytes(csv_text(change(DEMO_ROWS, "10002", status="退社")))
    mp = tmp_path / "m.json"
    mp.write_text(json.dumps(MAPPING, ensure_ascii=False), encoding="utf-8")
    audit_db = tmp_path / "audit.db"
    assert main(["--db", str(settings.db_path), "hr-import", str(src), "--mapping", str(mp), "--audit-db", str(audit_db)]) == 0
    db = sqlite3.connect(settings.db_path)
    assert db.execute("SELECT status FROM employees WHERE employee_no = '10002'").fetchone() == ("退職",)
    assert hrimport.history(db)[0]["by"].startswith("サーバーの操作")
    row = sqlite3.connect(audit_db).execute("SELECT actor_kind, action, detail FROM audit_log WHERE action = 'hr.import'").fetchone()
    assert row[0] == "サーバーの操作" and "社員番号：" in row[2] and "10002" in row[2]


def test_legacy_mapping_still_works(settings):
    db = sqlite3.connect(":memory:")
    m = hr.load_mapping(ROOT / "demo" / "company" / "hr" / "mapping_sample.json")    # active_values の前の書き方
    assert hr.import_employees(db, ROOT / "demo" / "company" / "hr" / "hr_export_sample.csv", m).errors == []
    assert dict(db.execute("SELECT employee_no, status FROM employees")) == {
        "10001": "在籍", "10002": "在籍", "10003": "在籍", "10004": "在籍", "10005": "在籍", "10006": "休職", "10007": "退職"}
