"""アクセスの考え方の広げ（2026-11-22。docs/access-model.md）のデータの土台のテスト。

- 人事データの雇用区分・期限・役職・役職の段（取り込みで割り当てる。雇用区分の値の対応は在籍状態と同じ作り）
- 今いる人の既定（社員・期限なし・役職は空）。前の版の保存先・控えにも同じ既定を入れる
- 役職の段で決まるグループ（昇格で入る・退任で外れる）。既定の「経営」のグループと、段の境目の設定
- 経営のロールに管理の権限が入っていない
- can() の条件（入口・場所・端末）：「管理の権限は確かな入口が要る」を ON にすると、社員番号の入口では管理の権限が効かない
"""

import sqlite3
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api import H, login  # noqa: E402
from test_hr_screen import DEMO_ROWS, MAPPING, client, commit, engine, preview, run, settings, upload  # noqa: E402,F401

from yanua import access, hr, hrimport  # noqa: E402
from yanua.permissions import ADMIN_PERMISSIONS, DEFAULT_ROLES  # noqa: E402

HEAD2 = "職員コード,漢字氏名,ふりがな,所属名,所属CD,在職区分,メール,雇用形態,契約終了日,役職名,役職ランク"
MAPPING2 = {"columns": {**MAPPING["columns"], "employment": "雇用形態", "expires_on": "契約終了日", "title": "役職名",
                        "title_level": "役職ランク"},
            "status_values": MAPPING["status_values"],
            "employment_values": {"正社員": "社員", "契約社員": "契約", "派遣社員": "派遣", "業務委託": "外部委託"}}
EXTRA = {"10001": ("正社員", "", "部長", "4"), "10002": ("契約社員", "2027/03/31", "", ""),
         "10003": ("派遣社員", "2026-12-31", "", "1"), "10004": ("正社員", "", "主任", "2"),
         "10005": ("正社員", "", "課長", "3"), "10006": ("正社員", "", "", ""), "10007": ("業務委託", "", "", "")}


def csv2(rows, extra=None) -> bytes:
    extra = {**EXTRA, **(extra or {})}
    lines = [HEAD2] + [",".join([*r, *extra[r[0]]]) for r in rows]
    return ("\n".join(lines) + "\n").encode("utf-8")


def person(settings, no) -> dict:
    db = sqlite3.connect(settings.db_path)
    row = db.execute("SELECT employment, expires_on, title, title_level FROM employees WHERE employee_no = ?", (no,)).fetchone()
    return dict(zip(("employment", "expires_on", "title", "title_level"), row))


# ---------------------------------------------------------------- 取り込み


def test_import_employment_expiry_title_and_level(client, settings):
    r = commit(client, csv2(DEMO_ROWS), MAPPING2)
    assert r["committed"], r
    assert person(settings, "10001") == {"employment": "社員", "expires_on": "", "title": "部長", "title_level": 4}
    assert person(settings, "10002") == {"employment": "契約", "expires_on": "2027-03-31", "title": "", "title_level": None}
    assert person(settings, "10003")["employment"] == "派遣" and person(settings, "10007")["employment"] == "外部委託"
    # 変わったところは、試しの「変わるところ」に項目の名前で出る
    p = preview(client, csv2(DEMO_ROWS, {"10004": ("正社員", "", "課長", "3")}), MAPPING2)
    assert p["changed"]["first"][0]["changes"] == ["役職", "役職の段"]
    assert p["changed"]["by_field"]["役職の段"] == 1


def test_unknown_employment_bad_date_and_bad_level_are_errors_with_line_numbers(client):
    bad = csv2(DEMO_ROWS, {"10002": ("アルバイト", "", "", ""), "10003": ("派遣社員", "来年の春", "", ""),
                           "10004": ("正社員", "", "主任", "二")})
    p = preview(client, bad, MAPPING2)
    assert not p["ok"]
    assert [(e["line"], e["message"]) for e in p["errors"]] == [
        (3, "雇用区分「アルバイト」を、社員・契約・派遣・短期・外部委託のどれに当てるか決めていません"),
        (4, "期限「来年の春」を日付として読めません（例 2026-03-31）"),
        (5, "役職の段「二」は 1〜99 の整数にしてください"),
    ]
    # 雇用区分の列を使うのに、値の対応がないときは、対応づけの誤り
    fid = upload(client, csv2(DEMO_ROWS))
    r = client.post("/api/hr/preview", json={"file_id": fid, "mapping": {**MAPPING2, "employment_values": {}}}, headers=H)
    assert r.status_code == 400 and "雇用区分の値" in r.text


def test_inspect_drafts_employment_values(client):
    fid = upload(client, csv2(DEMO_ROWS))
    r = client.post("/api/hr/inspect", json={"file_id": fid, "employment_column": "雇用形態"}, headers=H).json()
    assert r["draft"]["employment"] == "雇用形態" and r["draft"]["title_level"] == "役職ランク"
    assert {v["value"]: v["guess"] for v in r["employment_values"]} == {"正社員": "社員", "契約社員": "契約", "派遣社員": "派遣",
                                                                       "業務委託": "外部委託"}
    st = client.get("/api/hr/status").json()
    assert st["employments"] == ["社員", "契約", "派遣", "短期", "外部委託"]
    assert st["fields"]["title_level"] == "役職の段"


# ---------------------------------------------------------------- 今いる人の既定


def test_people_already_there_are_employees_without_expiry_or_title(client, settings):
    commit(client, DEMO_ROWS)                                  # 雇用区分などの列を使わない対応づけ
    assert person(settings, "10001") == {"employment": "社員", "expires_on": "", "title": "", "title_level": None}
    commit(client, csv2(DEMO_ROWS), MAPPING2)
    # 列を「使わない」にした取り込みでは、今の値を残す（変わる人にも数えない）。新しく入る人は「社員」
    newcomer = ("10008", "小林 新", "コバヤシ アラタ", "総務部", "200", "在職", "")
    p = preview(client, [*DEMO_ROWS, newcomer])
    assert p["changed"]["count"] == 0
    assert commit(client, [*DEMO_ROWS, newcomer])["committed"]
    assert person(settings, "10002")["employment"] == "契約" and person(settings, "10001")["title_level"] == 4
    assert person(settings, "10008") == {"employment": "社員", "expires_on": "", "title": "", "title_level": None}


def test_old_store_gets_defaults_and_old_backup_can_be_restored(tmp_path):
    db = sqlite3.connect(tmp_path / "old.db")
    db.executescript("""
        CREATE TABLE employees (employee_no TEXT PRIMARY KEY, name TEXT NOT NULL, dept TEXT NOT NULL,
            dept_code TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL);
        INSERT INTO employees VALUES ('1', '山田 太郎', '経理部', '', 1);
    """)
    hrimport.ensure_schema(db)
    assert db.execute("SELECT employment, expires_on, title, title_level FROM employees").fetchone() == ("社員", "", "", None)
    # 前の版の控え（雇用区分などの列がない）から戻しても、既定で入る
    with db:
        db.execute("INSERT INTO hr_backups (saved_at, source, total, active) VALUES ('2026-01-01', 'old.csv', 1, 1)")
        db.execute("CREATE TABLE hr_backup_1 AS SELECT employee_no, name, kana, dept, dept_code, status, email, active, "
                   "name_key, surname_key, dept_key, changed_at FROM employees")
        db.execute("UPDATE employees SET employment = '派遣', title_level = 3")
    access.ensure_schema(db)
    p = hrimport.preview_restore(db)
    assert p.diff["changed"]["by_field"]["雇用区分"] == 1
    assert hrimport.restore(db, "t").committed
    assert db.execute("SELECT employment, title_level FROM employees").fetchone() == ("社員", None)


# ---------------------------------------------------------------- 役職の段で決まるグループ


def test_executive_group_follows_title_level_promotion_and_retirement(client, settings):
    commit(client, csv2(DEMO_ROWS), MAPPING2)
    e = engine(settings)
    assert "reports.view" not in e.permissions("10001", "経理部")           # 部長（段 4）は経営ではない
    promoted = csv2(DEMO_ROWS, {"10001": ("正社員", "", "取締役", "5")})
    p = preview(client, promoted, MAPPING2)
    assert {g["name"]: (g["join"], g["leave"]) for g in p["groups"]} == {"経営": (1, 0)}   # 昇格で入る
    assert commit(client, promoted, MAPPING2)["committed"]
    assert {"reports.view", "documents.view"} <= set(e.permissions("10001", "経理部"))
    assert e.can("10001", "経理部", "documents.view", ("department", "人事部")).allowed      # 全社の文書
    assert "グループ「経営」（決まり：役職の段 5 以上）" in e.can("10001", "経理部", "reports.view").reasons[0]
    back = csv2(DEMO_ROWS, {"10001": ("正社員", "", "顧問", "")})
    p = preview(client, back, MAPPING2)
    assert {g["name"]: (g["join"], g["leave"]) for g in p["groups"]} == {"経営": (0, 1)}     # 退任で外れる
    assert commit(client, back, MAPPING2)["committed"]
    assert "reports.view" not in e.permissions("10001", "経理部")


def test_executive_level_comes_from_the_setting(client, settings):
    commit(client, csv2(DEMO_ROWS), MAPPING2)
    e = engine(settings)
    assert "reports.view" not in e.permissions("10001", "経理部")
    login(client, "10005")
    values = {"admin_needs_strong_entry": False, "title_levels": {"1": "一般", "4": "部長", "5": "役員"}, "executive_level": 4}
    r = client.put("/api/admin/settings/access", json={"values": values}, headers=H)
    assert r.status_code == 200, r.text
    assert "reports.view" in e.permissions("10001", "経理部")              # 段 4 以上 になったので、部長も入る
    entries = client.get("/api/audit", params={"action": "roles.group.update"}).json()["entries"]
    assert entries[0]["detail"] == "グループ「経営」の決まりを、役職の段 5 以上 → 4 以上 にした（設定に合わせた）"


def test_group_by_title_level_can_be_created_and_counts_people(client, settings):
    commit(client, csv2(DEMO_ROWS), MAPPING2)
    login(client, "10005")
    r = client.post("/api/admin/access/groups", json={"name": "課長以上", "rule": {"min_level": 3}}, headers=H)
    assert r.status_code in (200, 201), r.text
    g = next(x for x in r.json()["groups"] if x["name"] == "課長以上")
    assert g["rule_text"] == "役職の段 3 以上" and g["rule_count"] == 2                 # 10001（4）と 10005（3）
    assert r.json()["title_levels"]["3"] == "課長"
    bad = client.post("/api/admin/access/groups", json={"name": "だめ", "rule": {"min_level": 0}}, headers=H)
    assert bad.status_code == 400 and "1〜99" in bad.text
    # 決まりで入るグループなので、手では入れない
    add = client.post(f"/api/admin/access/groups/{g['group_id']}/members", json={"account_id": "10004"}, headers=H)
    assert add.status_code == 409


# ---------------------------------------------------------------- 経営のロール


def test_executive_role_has_no_admin_permissions():
    assert not set(DEFAULT_ROLES["経営"][1]) & set(ADMIN_PERMISSIONS)
    assert set(DEFAULT_ROLES["経営"][1]) == {"documents.view", "reports.view"}
    assert "roles.assign" in ADMIN_PERMISSIONS and "settings.edit" in ADMIN_PERMISSIONS and "audit.view" in ADMIN_PERMISSIONS
    # ほかの人が見られる範囲を変える権限（人事データの取り込み・1つ前に戻す、文書の公開先の変更）も管理の権限。roles.view は入れない
    assert {"hr.import", "documents.manage"} <= set(ADMIN_PERMISSIONS) and "roles.view" not in ADMIN_PERMISSIONS


def test_executive_sees_company_documents_but_not_infra_documents(client, settings):
    commit(client, csv2(DEMO_ROWS, {"10001": ("正社員", "", "取締役", "5")}), MAPPING2)
    e = engine(settings)
    assert e.can_view_document("10001", "経理部", "documents.view", ["dept:人事部"]).allowed       # 全社の文書は見える
    assert e.can_view_document("10001", "経理部", "documents.view", ["group:G-hr"]).allowed
    # 要る権限が infra.docs.view の文書（構成書）は、今までどおり経営でも見えない
    for targets in (["dept:経理部"], ["dept:人事部"], ["group:G-exec"], [access.ALL]):
        assert not e.can_view_document("10001", "経理部", "infra.docs.view", targets).allowed, targets
    scope = dict(e.doc_scope("10001", "経理部").perms)
    assert scope["documents.view"] is None and "infra.docs.view" not in scope       # 検索・出典の絞り込みでも同じ


def test_preview_counts_newcomers_entering_without_employment_type(client):
    commit(client, csv2(DEMO_ROWS), MAPPING2)
    newcomers = [("10008", "小林 新", "コバヤシ アラタ", "総務部", "200", "在職", ""),
                 ("10009", "森 光", "モリ ヒカル", "営業部", "400", "在職", "")]
    p = preview(client, [*DEMO_ROWS, *newcomers])                     # 雇用区分の列を使わない対応づけ
    assert p["added"]["count"] == 2 and p["default_employment"] == 2
    extra = {"10008": ("契約社員", "", "", ""), "10009": ("正社員", "", "", "")}
    assert preview(client, csv2([*DEMO_ROWS, *newcomers], extra), MAPPING2)["default_employment"] == 0   # 列を使えば 0
    assert preview(client, DEMO_ROWS)["default_employment"] == 0                                         # 新しく入る人がいなければ 0


def test_old_data_gets_job_roles_and_executive_group_once(settings, client):
    db = sqlite3.connect(settings.db_path)
    names = {r[0] for r in db.execute("SELECT name FROM access_roles")}
    assert {"マネージャー", "ディレクター", "経営"} <= names
    rule, = db.execute("SELECT rule FROM access_groups WHERE group_id = 'G-exec'").fetchone()
    assert rule == '{"min_level": 5}'
    # 画面で消したら、また足さない
    with db:
        db.execute("DELETE FROM group_grants WHERE group_id = 'G-exec'")
        db.execute("DELETE FROM access_groups WHERE group_id = 'G-exec'")
    assert access.upgrade(db, 5) == []
    assert db.execute("SELECT 1 FROM access_groups WHERE group_id = 'G-exec'").fetchone() is None


# ---------------------------------------------------------------- 条件（入口・場所・端末）


@pytest.mark.parametrize("on", [False, True])
def test_admin_permissions_need_a_strong_entry_only_when_the_rule_is_on(client, settings, on):
    commit(client, DEMO_ROWS)
    e = access.Engine(lambda: sqlite3.connect(str(settings.db_path)), admin_needs_strong=lambda: on)
    plain = access.DEFAULT_CONTEXT
    assert (plain.entry, plain.location, plain.device) == ("社員番号", "社内", "登録していない端末")
    d = e.can("10005", "情報システム部", "roles.assign", ("group", "G-hr"), plain)
    assert d.allowed is (not on)
    if on:
        assert d.reasons == [access.NEEDS_STRONG]
        assert "roles.assign" not in e.permissions("10005", "情報システム部", plain)
        assert "settings.edit" not in e.permissions("10005", "情報システム部")          # 条件を渡さないときも既定の条件
        # ほかの人が見られる範囲を変える権限も止まる（人事データの取り込み・文書の公開先の変更）。読むだけの権限は効く
        assert not e.can("10006", "人事部", "hr.import", None, plain).allowed
        assert not e.can("10005", "情報システム部", "documents.manage", ("department", "経理部"), plain).allowed
        assert e.can("10005", "情報システム部", "roles.view", None, plain).allowed
    # 確かな入口（SSO・多要素つきの Yanua アカウント）なら、ON でも効く
    for entry in (access.ENTRY_SSO, access.ENTRY_ACCOUNT_MFA):
        assert e.can("10005", "情報システム部", "roles.assign", ("group", "G-hr"), access.Context(entry=entry)).allowed
    # 管理でない権限は、入口に関係なく効く
    assert e.can("10005", "情報システム部", "ask", None, plain).allowed


def test_rule_cannot_be_turned_on_from_a_weak_entry_and_me_shows_the_context(client):
    login(client, "10005")
    me = client.get("/api/me").json()
    assert me["context"] == {"entry": "社員番号", "location": "社内", "device": "登録していない端末", "strong": False}
    values = {"admin_needs_strong_entry": True, "title_levels": {"1": "一般", "5": "役員"}, "executive_level": 5}
    r = client.put("/api/admin/settings/access", json={"values": values}, headers=H)
    assert r.status_code == 409 and "ON にできません" in r.text
    # 断ったことは、監査ログに settings.access.save を「拒否」で、理由つきで残す
    e = client.get("/api/audit", params={"action": "settings.access.save"}).json()["entries"][0]
    assert e["result"] == "拒否" and e["actor"] == "10005"
    assert "「管理の権限は確かな入口が要る」を ON にしようとしたので断った（今の入口：社員番号" in e["detail"]
    # 断ったので、まだ管理できる
    assert client.get("/api/admin/settings/access").status_code == 200
    assert client.get("/api/admin/settings/access").json()["values"]["admin_needs_strong_entry"] is False


def test_turning_the_rule_on_takes_admin_permissions_away_from_employee_number_logins(client, settings):
    # 設定のファイルで ON にした（画面からは ON にできない）。社員番号の入口では、管理の画面に入れなくなる
    settings.admin_needs_strong_entry = True
    login(client, "10005")
    assert client.get("/api/admin/settings").status_code == 403
    assert "settings.view" not in client.get("/api/me").json()["permissions"]
    assert "ask" in client.get("/api/me").json()["permissions"]
    settings.admin_needs_strong_entry = False
    assert client.get("/api/admin/settings").status_code == 200


def test_hr_db_lookup_returns_status_and_level(client, settings):
    commit(client, csv2(DEMO_ROWS), MAPPING2)
    db = sqlite3.connect(settings.db_path)
    assert hr.attrs_of(db, "10001") == ("在籍", 4)
    assert hr.attrs_of(db, "99999") == (None, None)
