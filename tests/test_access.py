"""権限の仕組み（役割 × 対象 × グループ。docs/access-model.md）のテスト。

- 移し替え：roles.json から役割・グループ・対象を作る（情シスは「ヘルプデスク担当」、監査は別の役割、IaC は作るだけ）
- 判定の1か所 can()：はい／いいえと理由（どのグループのどの役割から）。期限の切れたメンバーは数えない。変更で作り直す
- 決まりで入るメンバーと、手で入れたメンバーを区別する
- 変更は roles.assign だけ。自分が入っているグループは変えられない。監査ログに中身つきで残す
- 権限の設定に誤りがあれば起動しない
"""

import json
import sqlite3
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api import H, FakeLLM, login  # noqa: E402
from test_api import settings as base_settings  # noqa: E402,F401  （fixture を使う）

from yanua import access  # noqa: E402
from yanua.api import create_app  # noqa: E402
from yanua.permissions import DEFAULT_ROLES, PERMISSIONS  # noqa: E402

# デモ：10001 山田（経理部）、10002 佐藤（総務部）、10003 鈴木（営業部）、10004 高橋（営業部）、
# 10005 田中（情報システム部・管理者）、10006 伊藤（人事部）


@pytest.fixture
def settings(base_settings):
    return base_settings


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeLLM())) as c:
        yield c


def engine_of(settings):
    return access.Engine(lambda: sqlite3.connect(str(settings.db_path)))


def view(client) -> dict:
    login(client, "10005")
    r = client.get("/api/admin/access")
    assert r.status_code == 200, r.text
    return r.json()


def group(v, name) -> dict:
    return next(g for g in v["groups"] if g["name"] == name)


# ---------------------------------------------------------------- 権限の一覧・移し替え


def test_permission_catalog_matches_the_design():
    doc = (ROOT / "docs" / "access-model.md").read_text(encoding="utf-8")
    for name in PERMISSIONS:
        assert name in doc or name.split(".")[0] + ".*" in doc or name.startswith("infra."), name
    assert len(PERMISSIONS) == 31
    for role, (_, perms) in DEFAULT_ROLES.items():
        assert all(p in PERMISSIONS for p in perms), role


def test_migration_builds_roles_groups_and_targets(client):
    v = view(client)
    assert [g["name"] for g in v["groups"]] == ["社員", "総務", "情シス・ヘルプデスク", "人事", "管理者", "監査"]
    assert group(v, "社員")["rule"] == {"all": True}
    assert [(x["role"], x["target"]) for x in group(v, "社員")["grants"]] == [("社員", "メンバー自身の部署")]
    assert group(v, "総務")["rule"] == {"departments": ["総務部"]}
    assert [(x["role"], x["target"]) for x in group(v, "情シス・ヘルプデスク")["grants"]] == [("ヘルプデスク担当", "全社")]
    admin = group(v, "管理者")
    assert admin["rule"] is None and [m["account_id"] for m in admin["members"]] == ["10005"]   # 手で入れたメンバー
    assert admin["members"][0]["added_by"] == "（移し替え）"
    # 今の動きを変えないため、管理者は「監査」と「文書管理」の役割も持つ
    assert [x["role"] for x in admin["grants"]] == ["管理者", "監査", "文書管理"]
    assert [x["role"] for x in group(v, "監査")["grants"]] == ["監査"] and group(v, "監査")["members"] == []
    # IaC の役割は、中身の権限ごと作るが、どのグループにも与えない
    roles = {r["name"]: r for r in v["roles"]}
    assert roles["インフラ承認者"]["permissions"] == ["infra.docs.view", "infra.approve"]
    used = {x["role"] for g in v["groups"] for x in g["grants"]}
    assert not used & {"インフラ技術者", "インフラ承認者", "インフラ実行者", "インフラ管理"}
    assert {t["target_id"] for t in v["targets"]} >= {"special:self_dept", "special:all"}
    assert {a["account_id"]: a["kind"] for a in v["accounts"]}["sys:kiosk"] == "system"
    logs = client.get("/api/audit", params={"action": "roles.migrate"}).json()["entries"]
    assert len(logs) == 1 and logs[0]["actor"] == "システム"


def test_migration_runs_only_once(settings):
    with TestClient(create_app(settings, FakeLLM())):
        pass
    db = sqlite3.connect(settings.db_path)
    with db:
        db.execute("DELETE FROM group_members WHERE group_id = 'G-admin'")    # 管理者を外した
    db.close()
    with TestClient(create_app(settings, FakeLLM())) as c:          # 起動し直しても、roles.json から作り直さない
        login(c, "10005")
        assert c.get("/api/admin/access").status_code == 403


# ---------------------------------------------------------------- 判定


def test_can_gives_reasons_and_respects_targets(client, settings):
    view(client)
    e = engine_of(settings)
    d = e.can("10002", "総務部", "paper")
    assert d.allowed and d.reasons == ["グループ「総務」（決まり：部署「総務部」）の役割「総務」× 全社"]
    assert not e.can("10004", "営業部", "paper").allowed and e.can("10004", "営業部", "paper").reasons == []
    # メンバー自身の部署：自分の部署には効き、ほかの部署には効かない
    assert e.can("10004", "営業部", "documents.view", ("department", "営業部")).allowed
    assert not e.can("10004", "営業部", "documents.view", ("department", "経理部")).allowed
    assert e.departments("10004", "営業部", "documents.view") == {"営業部"}
    assert e.departments("10005", "情報システム部", "documents.manage") == access.ALL
    why = e.can("10005", "情報システム部", "roles.view").reasons
    assert len(why) == 2 and any("役割「監査」" in r for r in why) and any("役割「管理者」" in r for r in why)


def test_expired_members_are_not_counted_and_changes_rebuild(client, settings):
    e = engine_of(settings)
    assert not e.can("10004", "営業部", "audit.view").allowed
    login(client, "10005")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    r = client.post("/api/admin/access/groups/G-audit/members", json={"account_id": "10004", "expires_at": tomorrow}, headers=H)
    assert r.status_code == 201, r.text
    assert e.can("10004", "営業部", "audit.view").allowed                # 変更があったので作り直した
    assert "手で入れた、期限 " + tomorrow in e.can("10004", "営業部", "audit.view").reasons[0]
    later = access.Engine(e._connect, today=lambda: (date.today() + timedelta(days=2)).isoformat())
    assert not later.can("10004", "営業部", "audit.view").allowed       # 期限が切れたら、判定の時点で数えない
    login(client, "10004")
    assert client.get("/api/audit").status_code == 200


def test_rule_groups_cannot_have_manual_members(client):
    login(client, "10005")
    for gid in ("G-employees", "G-general"):
        r = client.post(f"/api/admin/access/groups/{gid}/members", json={"account_id": "10004"}, headers=H)
        assert r.status_code == 409 and "決まり" in r.json()["error"]["message"] and "手で出し入れできません" in r.json()["error"]["message"]
    r = client.delete("/api/admin/access/groups/G-general/members/10002", headers=H)
    assert r.status_code == 409
    denied = client.get("/api/audit", params={"action": "roles.member", "result": "拒否"}).json()["entries"]
    assert len(denied) == 3 and all("手で出し入れできません" in e["detail"] for e in denied)


def test_migration_splits_rule_and_manual_members(settings, tmp_path):
    """roles.json で部署と社員番号の両方を書いた役割は、決まりのグループと「（個別）」のグループに分ける（動きは同じ）。"""
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({"roles": {"総務": {"departments": ["総務部"], "employees": ["10003"]},
                                           "管理者": {"employees": ["10005"]}}}, ensure_ascii=False), encoding="utf-8")
    settings.roles_path = roles
    with TestClient(create_app(settings, FakeLLM())) as c:
        v = view(c)
        assert group(v, "総務")["rule"] == {"departments": ["総務部"]} and group(v, "総務")["members"] == []
        manual = group(v, "総務（個別）")
        assert manual["rule"] is None and [m["account_id"] for m in manual["members"]] == ["10003"]
        assert [(x["role"], x["target"]) for x in manual["grants"]] == [("総務", "全社")]
        for who in ("10002", "10003"):
            login(c, who)
            assert "paper" in c.get("/api/me").json()["permissions"]


def test_explain_shows_where_each_permission_comes_from(client):
    """「この人は何ができるか」：画面の欄と同じ q（社員番号・名前の一部・システムアカウント）で引く。"""
    login(client, "10005")
    for q in ("10002", "佐藤", "花子"):
        r = client.get("/api/admin/access/explain", params={"q": q})
        assert r.status_code == 200, r.text
        res = r.json()["result"]
        assert res["account_id"] == "10002" and res["groups"] == ["社員", "総務"]
    perms = {p["permission"]: p for p in res["permissions"]}
    paper = perms["paper"]["from"]
    assert [(x["group"], x["role"], x["target"], x["expires_at"]) for x in paper] == [("総務", "総務", "全社", None)]
    assert paper[0]["reason"] == "グループ「総務」（決まり：部署「総務部」）の役割「総務」× 全社"
    assert perms["ask"]["from"][0]["target"] == "メンバー自身の部署"
    assert "audit.view" not in perms
    # 決まらないときは候補だけ（営業部は2人）・いない人は候補なし
    r = client.get("/api/admin/access/explain", params={"q": "1000"}).json()
    assert r["result"] is None and len(r["candidates"]) == 6           # 退職した 10007 は出さない
    assert client.get("/api/admin/access/explain", params={"q": "99999"}).json() == {"q": "99999", "candidates": [], "result": None}
    assert client.get("/api/admin/access/explain").json()["candidates"] == []          # 何も入れなくても 400 にしない
    # システムアカウントも選べる（受付端末・更新の確認）
    r = client.get("/api/admin/access/explain", params={"q": "受付端末"}).json()
    assert r["result"]["account_id"] == "sys:kiosk" and r["result"]["permissions"] == []
    assert r["result"]["note"] == "今は端末トークンで /api/kiosk/ だけを使えます（この仕組みへの移し替えはまだ）"
    assert client.get("/api/admin/access/explain", params={"q": "10002"}).json()["result"]["note"] == ""


# ---------------------------------------------------------------- 変更（roles.assign）


def test_only_roles_assign_can_change_and_viewing_needs_roles_view(client):
    for who in ("10002", "10004", "10006"):
        login(client, who)
        assert client.get("/api/admin/access").status_code == 403
        assert client.post("/api/admin/access/groups/G-general/members", json={"account_id": "10004"}, headers=H).status_code == 403


def denied_message(r) -> str:
    assert r.status_code == 409, r.text
    return r.json()["error"]["message"]


def test_changes_that_increase_your_own_permissions_are_refused(client):
    login(client, "10005")
    roles = {x["name"]: x["role_id"] for x in client.get("/api/admin/access").json()["roles"]}
    # 自分が入っているグループに役割を足す
    msg = denied_message(client.post("/api/admin/access/groups/G-admin/grants", json={"role_id": roles["総務"], "target_id": "special:all"}, headers=H))
    assert "自分の権限が増える変更はできません" in msg and "paper" in msg
    # 自分のグループが使っている役割に、権限を足す
    assert "自分の権限が増える" in denied_message(client.put(f"/api/admin/access/roles/{roles['監査']}",
                                                       json={"permissions": ["audit.view", "roles.view", "hr.view"]}, headers=H))
    # 自分に効かない変更・権限が減る変更はできる
    assert client.post("/api/admin/access/groups/G-hr/grants", json={"role_id": roles["総務"], "target_id": "special:all"}, headers=H).status_code == 201
    assert client.put(f"/api/admin/access/roles/{roles['監査']}", json={"permissions": ["audit.view"]}, headers=H).status_code == 200
    denied = [e for e in client.get("/api/audit", params={"action": "roles", "result": "拒否"}).json()["entries"]]
    assert len(denied) == 2 and all("変えませんでした：自分の権限が増える変更はできません" in e["detail"] for e in denied)


def test_cannot_add_yourself_or_extend_your_own_expiry(client):
    login(client, "10005")
    tomorrow = (date.today() + timedelta(days=1)).isoformat()
    assert client.post("/api/admin/access/groups/G-admin/members", json={"account_id": "10002", "expires_at": tomorrow}, headers=H).status_code == 201
    login(client, "10002")                       # 10002 は期限つきの管理者になった
    later = (date.today() + timedelta(days=30)).isoformat()
    assert "自分の権限が増える" in denied_message(client.post("/api/admin/access/groups/G-admin/members",
                                                       json={"account_id": "10002", "expires_at": later}, headers=H))
    assert "自分の権限が増える" in denied_message(client.post("/api/admin/access/groups/G-admin/members",
                                                       json={"account_id": "10002"}, headers=H))     # 期限なしにするのも延ばすこと
    # 期限を縮めるのはできる（減る変更）
    assert client.post("/api/admin/access/groups/G-admin/members", json={"account_id": "10002", "expires_at": date.today().isoformat()},
                       headers=H).status_code == 201
    # 自分を、権限が増えるグループに入れる
    login(client, "10005")
    assert client.post("/api/admin/access/groups/G-audit/members", json={"account_id": "10005"}, headers=H).status_code == 201   # 増えない
    roles = {x["name"]: x["role_id"] for x in client.get("/api/admin/access").json()["roles"]}
    assert client.post("/api/admin/access/groups/G-audit/grants", json={"role_id": roles["人事"], "target_id": "special:all"},
                       headers=H).status_code == 409      # 自分が入った監査のグループに人事を足すと、自分が増える


def test_roles_can_be_removed_from_the_everyone_group(client):
    login(client, "10005")
    grant = next(g for g in group(client.get("/api/admin/access").json(), "社員")["grants"] if g["role"] == "社員")
    r = client.delete(f"/api/admin/access/grants/{grant['grant_id']}", headers=H)
    assert r.status_code == 200 and r.json()["detail"] == "社員 から 役割「社員」× メンバー自身の部署 を外した"
    login(client, "10004")
    assert "ask" not in client.get("/api/me").json()["permissions"]
    # 戻すのは自分の権限が増える変更なので、自分（10005）ではできない
    login(client, "10005")
    r = client.post("/api/admin/access/groups/G-employees/grants", json={"role_id": grant["role_id"], "target_id": "special:self_dept"}, headers=H)
    assert "自分の権限が増える" in denied_message(r)


def test_cannot_remove_the_last_roles_assign_holder(client):
    login(client, "10005")
    msg = denied_message(client.delete("/api/admin/access/groups/G-admin/members/10005", headers=H))
    assert "roles.assign を持つ人が1人もいなくなる" in msg
    admin_grant = next(g for g in group(client.get("/api/admin/access").json(), "管理者")["grants"] if g["role"] == "管理者")
    assert "1人もいなくなる" in denied_message(client.delete(f"/api/admin/access/grants/{admin_grant['grant_id']}", headers=H))
    roles = {x["name"]: x["role_id"] for x in client.get("/api/admin/access").json()["roles"]}
    assert "1人もいなくなる" in denied_message(client.put(f"/api/admin/access/roles/{roles['管理者']}",
                                                     json={"permissions": ["settings.view", "roles.view"]}, headers=H))
    # ほかに roles.assign を持つ人がいれば、自分を外せる
    assert client.post("/api/admin/access/groups/G-admin/members", json={"account_id": "10002"}, headers=H).status_code == 201
    assert client.delete("/api/admin/access/groups/G-admin/members/10005", headers=H).status_code == 200
    login(client, "10002")                       # 10005 は管理者でなくなったので、10002 で監査ログを見る
    assert len(client.get("/api/audit", params={"action": "roles", "result": "拒否"}).json()["entries"]) == 3


def test_changes_are_recorded_with_their_content(client):
    login(client, "10005")
    roles = {x["name"]: x["role_id"] for x in client.get("/api/admin/access").json()["roles"]}
    assert client.post("/api/admin/access/groups/G-audit/members",
                       json={"account_id": "10001", "expires_at": "2026-12-31"}, headers=H).status_code == 201
    r = client.post("/api/admin/access/groups/G-hr/grants", json={"role_id": roles["総務"], "target_id": "special:all"}, headers=H)
    assert r.status_code == 201
    gid = next(x["grant_id"] for x in group(r.json(), "人事")["grants"] if x["role"] == "総務")
    assert client.delete(f"/api/admin/access/grants/{gid}", headers=H).status_code == 200
    assert client.put(f"/api/admin/access/roles/{roles['人事']}", json={"permissions": ["hr.view"]}, headers=H).status_code == 200
    assert client.put(f"/api/admin/access/roles/{roles['人事']}", json={"permissions": ["nosuch"]}, headers=H).status_code == 400
    assert client.post("/api/admin/access/groups/G-audit/members", json={"account_id": "99999"}, headers=H).status_code == 400
    assert client.post("/api/admin/access/groups/G-audit/members", json={"account_id": "10001", "expires_at": "来月"}, headers=H).status_code == 400
    details = [e["detail"] for e in reversed(client.get("/api/audit", params={"action": "roles", "result": "成功"}).json()["entries"])
               if e["action"] != "roles.migrate"]
    assert details == [
        "監査 に 山田 太郎 を追加（期限 2026-12-31）",
        "人事 に 役割「総務」× 全社 を与えた",
        "人事 から 役割「総務」× 全社 を外した",
        "役割「人事」の権限：hr.import を外した",
    ]
    login(client, "10001")
    assert "audit.view" in client.get("/api/me").json()["permissions"]   # すぐに効く（作り直した）


# ---------------------------------------------------------------- 起動のときの決まり


def test_broken_access_settings_stop_startup(settings):
    with TestClient(create_app(settings, FakeLLM())):
        pass
    db = sqlite3.connect(settings.db_path)
    with db:
        db.execute("UPDATE access_roles SET permissions = ? WHERE name = '総務'", (json.dumps(["paper", "nosuch.perm"]),))
        db.execute("INSERT INTO group_grants (group_id, role_id, target_id) VALUES ('G-hr', 'R99', 'special:all')")
    db.close()
    with pytest.raises(access.AccessError, match="起動しません") as e:
        create_app(settings, FakeLLM())
    where = {w for w, _ in e.value.problems}
    assert "roles.総務" in where and any(w.startswith("grants[") for w in where)


def test_document_search_uses_the_decision(client, settings):
    """文書の検索の絞り込み（LLM に渡す前）も、判定（documents.view が効く部署）を使う。"""
    login(client, "10005")
    # 営業部の高橋に、経理部の文書を見られるようにする：監査のグループに「社員」× 経理部 を与え、高橋を監査に入れる
    db = sqlite3.connect(settings.db_path)
    with db:
        db.execute("INSERT INTO group_grants (group_id, role_id, target_id) VALUES ('G-audit', 'R01', 'dept:経理部')")
        db.execute("UPDATE access_meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'generation'")
    db.close()
    assert client.post("/api/admin/access/groups/G-audit/members", json={"account_id": "10004"}, headers=H).status_code == 201
    login(client, "10004")
    hits = {h["doc_id"] for h in client.get("/api/search", params={"q": "月次決算"}).json()["hits"]}
    assert "K01" in hits                                              # 経理部の「月次決算の手順」
    assert client.get("/api/documents/K01").status_code == 200
    login(client, "10003")                                            # 同じ営業部でも、監査に入っていない人は見えない
    assert "K01" not in {h["doc_id"] for h in client.get("/api/search", params={"q": "月次決算"}).json()["hits"]}
    assert client.get("/api/documents/K01").status_code == 404
