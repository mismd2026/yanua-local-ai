"""S-54 の画面（/admin/access）のための API と、復旧用のコマンド（python -m yanua access）のテスト。

- 変える前の確かめ：試しに当てるだけの API（保存しない）。だれのどの権限が増える・減るか。自分の権限が減るか。断られる理由
- グループ・役割・対象・対象のまとまりを作る・直す・消す（消せるのは使っていないものだけ）
- 対象のうち 部署・グループ・機能 は自動でそろえる。システム・サーバーは手で作る（宛先は S-53 から、接続情報は参照名だけ）
- 人を探す（社員番号・名前の一部・システムアカウント）
- 復旧用のコマンド：自分の権限の決まりは当てないが、実行したことは必ず監査ログに残す（した人は「サーバーの操作」と OS ユーザー）
"""

import getpass
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api import H, FakeLLM, login  # noqa: E402
from test_api import settings as base_settings  # noqa: E402,F401  （fixture を使う）

from yanua import access  # noqa: E402
from yanua.__main__ import main  # noqa: E402
from yanua.api import create_app  # noqa: E402


@pytest.fixture
def settings(base_settings):
    return base_settings


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeLLM())) as c:
        login(c, "10005")
        yield c


def view(client) -> dict:
    r = client.get("/api/admin/access")
    assert r.status_code == 200, r.text
    return r.json()


def group(v, name) -> dict:
    return next(g for g in v["groups"] if g["name"] == name)


def role_ids(client) -> dict:
    return {r["name"]: r["role_id"] for r in view(client)["roles"]}


def preview(client, op, **args) -> dict:
    r = client.post("/api/admin/access/preview", json={"op": op, "args": args}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def logs(client, **params) -> list[dict]:
    return client.get("/api/audit", params=params).json()["entries"]


# ---------------------------------------------------------------- 変える前の確かめ（試しに当てるだけ）


def test_preview_shows_who_gains_what_and_saves_nothing(client):
    p = preview(client, "member.add", group_id="G-audit", account_id="10004", expires_at="2099-12-31")
    assert p["refused"] is None and p["self_loss"] is False and p["self"] == {"gained": [], "lost": []}
    assert p["detail"] == "監査 に 高橋 美咲 を追加（期限 2099-12-31）"
    assert p["people"] == [{"account_id": "10004", "name": "高橋 美咲", "dept": "営業部",
                            "gained": ["audit.view × 全社（期限 2099-12-31）", "roles.view × 全社（期限 2099-12-31）"], "lost": []}]
    # 保存していない（メンバーは増えず、監査ログにも残らない）
    assert group(view(client), "監査")["members"] == []
    assert [e for e in logs(client, action="roles.member") if e["result"] == "成功"] == []


def test_preview_warns_when_you_lose_your_own_permissions(client):
    grant = next(g for g in group(view(client), "社員")["grants"] if g["role"] == "社員")
    p = preview(client, "grant.remove", grant_id=grant["grant_id"])
    assert p["refused"] is None and p["self_loss"] is True
    assert "ask × メンバー自身の部署" in p["self"]["lost"]
    assert {x["account_id"] for x in p["people"]} == {"10001", "10002", "10003", "10004", "10005", "10006"}
    assert all("ask × メンバー自身の部署" in x["lost"] for x in p["people"])
    # 自分の期限を縮めるのも「減る」
    client.post("/api/admin/access/groups/G-admin/members", json={"account_id": "10002", "expires_at": "2099-12-31"}, headers=H)
    login(client, "10002")
    p = preview(client, "member.add", group_id="G-admin", account_id="10002", expires_at="2099-01-31")
    assert p["refused"] is None and p["self_loss"] is True
    assert "roles.assign × 全社（期限 2099-12-31 → 2099-01-31）" in p["self"]["lost"]


def test_preview_gives_the_reason_when_the_change_would_be_refused(client):
    roles = role_ids(client)
    p = preview(client, "grant.add", group_id="G-admin", role_id=roles["総務"], target_id="special:all")
    assert p["refused"].startswith("自分の権限が増える変更はできません") and p["self_loss"] is False
    assert "paper × 全社" in p["self"]["gained"]
    p = preview(client, "member.remove", group_id="G-admin", account_id="10005")
    assert "roles.assign を持つ人が1人もいなくなる" in p["refused"]
    p = preview(client, "member.add", group_id="G-general", account_id="10004")
    assert "決まり（部署「総務部」）で入るグループなので" in p["refused"] and p["problems"][0]["where"] == "account"
    p = preview(client, "member.add", group_id="G-audit", account_id="10004", expires_at="来月")
    assert p["refused"] == "期限は YYYY-MM-DD で書いてください" and p["problems"][0]["where"] == "expires_at"
    assert client.post("/api/admin/access/preview", json={"op": "nosuch", "args": {}}, headers=H).status_code == 400
    assert client.post("/api/admin/access/preview", json={"op": "member.add", "args": {"group_id": "G-none", "account_id": "10004"}},
                       headers=H).status_code == 404
    login(client, "10002")
    assert client.post("/api/admin/access/preview", json={"op": "member.add", "args": {}}, headers=H).status_code == 403


def test_preview_summarises_by_group_when_many_people_are_affected(client, monkeypatch):
    monkeypatch.setattr(access, "IMPACT_LIMIT", 3)
    grant = next(g for g in group(view(client), "社員")["grants"] if g["role"] == "社員")
    p = preview(client, "grant.remove", grant_id=grant["grant_id"])
    assert p["too_many"] is True and p["checked"] == 6 and p["people"] == []
    assert p["groups"][0]["name"] == "社員" and p["groups"][0]["members"] == 6
    assert "ask × メンバー自身の部署" in p["groups"][0]["lost"]
    assert p["self_loss"] is True                                      # 自分のことは、多くても必ず出す


# ---------------------------------------------------------------- グループ・役割


def test_groups_can_be_created_described_and_deleted_only_when_empty(client):
    r = client.post("/api/admin/access/groups", json={"name": "経理の文書担当", "description": "経理部の文書を取り込む"}, headers=H)
    assert r.status_code == 201, r.text
    g = group(r.json(), "経理の文書担当")
    assert g["how"] == "manual" and g["member_count"] == 0
    assert any(t["target_id"] == f"group:{g['group_id']}" for t in r.json()["targets"])      # グループの対象も自動でできる
    assert client.post("/api/admin/access/groups", json={"name": "経理の文書担当"}, headers=H).status_code == 400   # 同じ名前
    r = client.patch(f"/api/admin/access/groups/{g['group_id']}", json={"description": "経理部の文書の担当"}, headers=H)
    assert group(r.json(), "経理の文書担当")["description"] == "経理部の文書の担当"
    # メンバーがいると消せない。外せば消せる
    client.post(f"/api/admin/access/groups/{g['group_id']}/members", json={"account_id": "10001"}, headers=H)
    r = client.delete(f"/api/admin/access/groups/{g['group_id']}", headers=H)
    assert r.status_code == 409 and "手で入れたメンバーがいます" in r.json()["error"]["message"]
    client.delete(f"/api/admin/access/groups/{g['group_id']}/members/10001", headers=H)
    r = client.delete(f"/api/admin/access/groups/{g['group_id']}", headers=H)
    assert r.status_code == 200 and all(x["name"] != "経理の文書担当" for x in r.json()["groups"])
    assert all(t["target_id"] != f"group:{g['group_id']}" for t in r.json()["targets"])
    # 役割を与えているグループ・決まりで入る人がいるグループは消せない
    r = client.delete("/api/admin/access/groups/G-audit", headers=H)
    assert r.status_code == 409 and "役割を与えています" in r.json()["error"]["message"]
    r = client.post("/api/admin/access/groups", json={"name": "営業部の全員", "rule": {"departments": ["営業部"]}}, headers=H)
    sales = group(r.json(), "営業部の全員")
    assert sales["how"] == "rule" and sales["rule_count"] == 2 and sales["rule_text"] == "部署「営業部」"
    assert "決まり（部署「営業部」）で入る人がいます" in client.delete(f"/api/admin/access/groups/{sales['group_id']}", headers=H).json()["error"]["message"]
    assert client.post("/api/admin/access/groups", json={"name": "x", "rule": {"departments": ["ない部"]}}, headers=H).status_code == 400
    actions = [e["action"] for e in logs(client, action="roles.group", result="成功")]
    assert actions.count("roles.group.create") == 2 and "roles.group.update" in actions and "roles.group.delete" in actions


def test_roles_can_be_created_and_deleted_only_when_unused(client):
    r = client.post("/api/admin/access/roles", json={"name": "受付の確認", "description": "受付の記録を見る"}, headers=H)
    assert r.status_code == 201, r.text
    new = next(x for x in r.json()["roles"] if x["name"] == "受付の確認")
    assert new["permissions"] == [] and new["used_by"] == []
    r = client.put(f"/api/admin/access/roles/{new['role_id']}", json={"permissions": ["reception.records"]}, headers=H)
    assert r.status_code == 200 and r.json()["detail"] == "役割「受付の確認」の権限：reception.records を足した"
    used = next(x for x in view(client)["roles"] if x["name"] == "総務")
    assert used["used_by"] == ["総務"]
    r = client.delete(f"/api/admin/access/roles/{used['role_id']}", headers=H)
    assert r.status_code == 409 and "グループ（総務）に与えているので消せません" in r.json()["error"]["message"]
    assert client.delete(f"/api/admin/access/roles/{new['role_id']}", headers=H).status_code == 200
    assert client.post("/api/admin/access/roles", json={"name": "x", "permissions": ["nosuch"]}, headers=H).status_code == 400


# ---------------------------------------------------------------- 対象・対象のまとまり


def test_departments_groups_and_features_are_targets_automatically(client):
    targets = {t["target_id"]: t for t in view(client)["targets"]}
    for tid in ("dept:営業部", "dept:人事部", "group:G-hr", "feature:チャット", "feature:IaC（将来）"):
        assert targets[tid]["auto"] is True, tid
    assert targets["dept:営業部"]["kind_name"] == "部署" and targets["special:all"]["kind_name"] == "特別"
    # 手では作れない
    r = client.post("/api/admin/access/targets", json={"kind": "department", "name": "新しい部"}, headers=H)
    assert r.status_code == 400 and "システムとサーバーだけ" in r.json()["error"]["message"]


def test_systems_and_servers_keep_only_the_name_of_the_secret(client, monkeypatch):
    monkeypatch.setenv("YANUA_TEST_AWS_KEY", "the-real-secret-value")
    v = view(client)
    host = v["hosts"][0]
    r = client.post("/api/admin/access/targets", json={"kind": "system", "name": "本番の AWS", "description": "読み取りだけ",
                                                       "host": host, "secret_ref": "YANUA_TEST_AWS_KEY"}, headers=H)
    assert r.status_code == 201, r.text
    assert "the-real-secret-value" not in r.text
    t = next(x for x in r.json()["targets"] if x["name"] == "本番の AWS")
    assert (t["kind"], t["host"], t["secret_ref"], t["auto"]) == ("system", host, "YANUA_TEST_AWS_KEY", False)
    entry = logs(client, action="roles.target.create")[0]
    assert "接続情報の参照名：YANUA_TEST_AWS_KEY" in entry["detail"] and "the-real-secret-value" not in entry["detail"]
    # 宛先は S-53 の宛先から。参照名は環境変数の名前だけ（値のようなものは断る）
    bad = client.post("/api/admin/access/targets", json={"kind": "server", "name": "x", "host": "evil.example.com"}, headers=H)
    assert bad.status_code == 400 and "通信の設定（S-53）の宛先から" in bad.json()["error"]["message"]
    bad = client.post("/api/admin/access/targets", json={"kind": "server", "name": "x", "secret_ref": "AKIA-xxxx/yyyy"}, headers=H)
    assert bad.status_code == 400 and bad.json()["error"]["details"][0]["where"] == "secret_ref"
    r = client.patch(f"/api/admin/access/targets/{t['target_id']}", json={"description": "plan まで", "host": "", "secret_ref": "AWS_RO"},
                     headers=H)
    assert r.status_code == 200 and next(x for x in r.json()["targets"] if x["target_id"] == t["target_id"])["secret_ref"] == "AWS_RO"
    # まとまりに使っていると消せない
    r = client.post("/api/admin/access/target-sets", json={"name": "クラウド", "members": [t["target_id"]]}, headers=H)
    assert r.status_code == 201
    assert client.delete(f"/api/admin/access/targets/{t['target_id']}", headers=H).status_code == 409


def test_target_sets_can_be_granted_and_changes_go_through_the_rules(client):
    roles = role_ids(client)
    r = client.post("/api/admin/access/target-sets", json={"name": "営業と経理", "members": ["dept:営業部", "dept:経理部"]}, headers=H)
    assert r.status_code == 201, r.text
    sid = next(s["set_id"] for s in r.json()["target_sets"] if s["name"] == "営業と経理")
    r = client.post("/api/admin/access/groups/G-hr/grants", json={"role_id": roles["社員"], "set_id": sid}, headers=H)
    assert r.status_code == 201 and r.json()["detail"] == "人事 に 役割「社員」× まとまり「営業と経理」 を与えた"
    # まとまりの中身を変えると、そのまとまりを使うグループの人の権限が変わる（試しに当てて分かる）
    p = preview(client, "set.update", set_id=sid, members=["dept:営業部", "dept:経理部", "dept:総務部"])
    assert p["people"][0]["account_id"] == "10006" and "documents.view × 総務部" in p["people"][0]["gained"]
    assert client.put(f"/api/admin/access/target-sets/{sid}", json={"members": ["dept:営業部"]}, headers=H).status_code == 200
    assert client.delete(f"/api/admin/access/target-sets/{sid}", headers=H).status_code == 409    # グループに与えている
    assert client.post("/api/admin/access/target-sets", json={"name": "空", "members": []}, headers=H).status_code == 400


def test_people_search_by_number_name_and_system_accounts(client):
    find = lambda q: [p["account_id"] for p in client.get("/api/admin/access/people", params={"q": q}).json()["people"]]   # noqa: E731
    assert find("10004") == ["10004"] and find("高橋") == ["10004"] and find("美咲") == ["10004"]
    assert find("渡辺") == []                                          # 退職した人は出さない
    assert set(find("sys:")) == {"sys:kiosk", "sys:update-check"} and find("受付端末") == ["sys:kiosk"]
    assert find("") == [] and find("%") == []
    login(client, "10004")
    assert client.get("/api/admin/access/people", params={"q": "1"}).status_code == 403


# ---------------------------------------------------------------- 復旧用のコマンド


def test_recovery_command_restores_an_admin_and_is_audited(client, settings, capsys):
    # 管理者が自分を外して、だれも戻せなくなった（画面と API では断るので、DB で外した形を作る）
    db = sqlite3.connect(settings.db_path)
    with db:
        db.execute("DELETE FROM group_members WHERE group_id = 'G-admin'")
        db.execute("UPDATE access_meta SET value = CAST(value AS INTEGER) + 1 WHERE key = 'generation'")
    db.close()
    assert client.get("/api/admin/access").status_code == 403
    args = ["--db", str(settings.db_path), "access"]
    assert main([*args, "show"]) == 0
    out = capsys.readouterr().out
    assert "G-admin  管理者  （入り方：手で入れる）" in out and "役割 管理者 × 全社" in out
    assert main([*args, "add-member", "管理者", "10005", "--expires", "2099-12-31"]) == 0
    assert "管理者 に 田中 健 を追加（期限 2099-12-31）" in capsys.readouterr().out
    assert client.get("/api/admin/access").status_code == 200          # 動いているサーバーにも、すぐ効く
    # add-grant は自分の権限の決まりを当てない（「自分」がいない）
    assert main([*args, "add-grant", "監査", "管理者", "全社"]) == 0
    assert any(x["role"] == "管理者" for x in group(view(client), "監査")["grants"])
    # 決まりで入るグループ・ない社員番号は断り、断ったことも残す
    assert main([*args, "add-member", "総務", "10004"]) == 1
    assert main([*args, "add-member", "管理者", "99999"]) == 1
    assert main([*args, "add-member", "ないグループ", "10004"]) == 1
    entries = [e for e in logs(client, action="roles") if e["actor"] == "サーバーの操作"]
    who = f"OS ユーザー {getpass.getuser()}"
    assert [(e["action"], e["result"]) for e in reversed(entries)] == [
        ("roles.member.add", "成功"), ("roles.grant.add", "成功"), ("roles.member.add", "失敗"), ("roles.member.add", "失敗")]
    assert all(e["actor_kind"] == "サーバーの操作" and e["actor_name"] == who for e in entries)
    assert who in entries[-1]["detail"] and "管理者 に 田中 健 を追加" in entries[-1]["detail"]
    assert next(m for m in group(view(client), "管理者")["members"])["added_by"] == f"サーバーの操作（{who}）"


def test_recovery_command_does_not_change_anything_if_it_cannot_audit(settings, tmp_path, capsys):
    with TestClient(create_app(settings, FakeLLM())):
        pass
    blocked = tmp_path / "audit-dir-is-a-file"
    blocked.write_text("x")
    code = main(["--db", str(settings.db_path), "access", "--audit-db", str(blocked / "audit.db"), "add-member", "監査", "10004"])
    assert code == 1 and "監査ログ" in capsys.readouterr().out
    db = sqlite3.connect(settings.db_path)
    assert db.execute("SELECT COUNT(*) FROM group_members WHERE group_id = 'G-audit'").fetchone()[0] == 0
    db.close()
