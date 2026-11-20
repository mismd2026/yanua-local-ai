"""S-50 文書の公開先（公開先 × 見るのに要る権限）と、文書の画面の API のテスト。

- 前後で同じ：移し替える前に書き出した tests/golden/documents_before.json と、今のコードの結果が一致する（意図した違いはない）
- 前の保存先（doc_access：部署・全社・社外公開）からの移し替え
- 判定：見るのに要る権限を、公開先を含む対象で持っているときだけ。can（Engine.can_view_document）と検索の絞り込み（DocScope）が同じ答え
- グループ専用の文書・要る権限 infra.docs.view の文書。見られない文書の題名は返事に出さない
- 画面の API：一覧（扱える部署の文書だけ）・公開先を直す（本文は作り直さない）・外す・戻す・変える前の確かめ・取り込み
- 監査ログに中身つきで残す（本文は入れない）
"""

import json
import sqlite3
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from documents_snapshot import GOLDEN, build_settings, snapshot  # noqa: E402
from test_api import H, FakeLLM, login  # noqa: E402

from yanua import access  # noqa: E402
from yanua.api import create_app  # noqa: E402
from yanua.store import DocScope, DocSettings, Store  # noqa: E402

PEOPLE = {"10001": "経理部", "10002": "総務部", "10003": "営業部", "10004": "営業部", "10005": "情報システム部", "10006": "人事部"}


@pytest.fixture
def settings(tmp_path):
    return build_settings(tmp_path)


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])) as c:
        login(c, "10005")
        yield c


def logs(c, action) -> list[dict]:
    login(c, "10005")
    return c.get("/api/audit", params={"action": action}).json()["entries"]


def search_ids(c, q) -> list[str]:
    return [h["doc_id"] for h in c.get("/api/search", params={"q": q}).json()["hits"]]


# ---------------------------------------------------------------- 前後で同じ


def test_same_documents_before_and_after(tmp_path):
    before = json.loads(GOLDEN.read_text(encoding="utf-8"))
    after = json.loads(json.dumps(snapshot(tmp_path)))
    for no in before["people"]:
        for key in before["people"][no]:
            assert after["people"][no].get(key) == before["people"][no][key], f"{no} の {key} が変わりました"
    assert after["audit"] == before["audit"], "質問の監査ログ（権限で見せなかった文書の件数）が変わりました"
    assert after["by_department"] == before["by_department"], "部署で決める使い方（コマンド・ヘルプデスク）の検索が変わりました"
    assert after["public"] == before["public"], "社外向けの検索が変わりました"
    assert after == before


def test_old_department_table_is_migrated_once(tmp_path):
    db = tmp_path / "old.db"
    con = sqlite3.connect(db)
    con.executescript("""
        CREATE TABLE documents (doc_id TEXT PRIMARY KEY, source TEXT NOT NULL, path TEXT NOT NULL, title TEXT NOT NULL,
            owner TEXT NOT NULL DEFAULT '', updated TEXT NOT NULL DEFAULT '', content_hash TEXT NOT NULL, indexed_at TEXT NOT NULL);
        CREATE TABLE doc_access (doc_id TEXT NOT NULL, department TEXT NOT NULL, PRIMARY KEY (doc_id, department));
        CREATE TABLE chunks (chunk_id INTEGER PRIMARY KEY AUTOINCREMENT, doc_id TEXT NOT NULL, position INTEGER NOT NULL,
            section TEXT NOT NULL, text TEXT NOT NULL);
        INSERT INTO documents VALUES ('K01', '/x', 'k.md', '月次決算', '経理部', '', 'h1', 't'), ('P01', '/x', 'p.md', '会社案内', '総務部', '', 'h2', 't'),
                                     ('A01', '/x', 'a.md', '就業規則', '総務部', '', 'h3', 't');
        INSERT INTO doc_access VALUES ('K01', '経理部'), ('K01', '総務部'), ('P01', '社外公開'), ('A01', '全社');
        INSERT INTO chunks (doc_id, position, section, text) VALUES ('K01', 0, '', 'x'), ('P01', 0, '', 'y'), ('A01', 0, '', 'z');
    """)
    con.commit()
    con.close()
    s = Store(db)
    assert s.doc_settings("K01")["targets"] == ["dept:経理部", "dept:総務部"]
    assert s.doc_settings("P01")["targets"] == ["special:all"] and s.doc_settings("P01")["external"] is True
    assert s.doc_settings("A01")["targets"] == ["special:all"] and s.doc_settings("A01")["required"] == "documents.view"
    assert {c.doc_id for c in s.public_chunks()} == {"P01"}
    assert {c.doc_id for c in s.chunks_for("営業部")} == {"P01", "A01"}
    assert s.db.execute("SELECT 1 FROM sqlite_master WHERE name = 'doc_access'").fetchone() is None
    s.close()
    Store(db).close()                       # 2回目は何もしない


# ---------------------------------------------------------------- 判定


def test_can_and_the_search_filter_agree_for_everyone(client, settings):
    engine = access.Engine(lambda: sqlite3.connect(str(settings.db_path)))
    s = Store(settings.db_path)
    for no, dept in PEOPLE.items():
        scope = engine.doc_scope(no, dept)
        for d in s.documents():
            assert engine.can_view_document(no, dept, d["required"], d["targets"]).allowed == s.can_view(d["doc_id"], scope), (no, d["doc_id"])
    why = engine.can_view_document("10001", "経理部", "documents.view", ["dept:経理部"]).reasons
    assert why == ["グループ「社員」（決まり：全員）の役割「社員」× メンバー自身の所属（部署と、入っているグループ）"]


def test_group_only_document_that_needs_infra_docs_view(client):
    # 「インフラ」のグループ（10003 だけ）に、infra.docs.view × そのグループ を与える
    r = client.post("/api/admin/access/roles", json={"name": "構成書を読む", "permissions": ["infra.docs.view"]}, headers=H)
    role = next(x["role_id"] for x in r.json()["roles"] if x["name"] == "構成書を読む")
    r = client.post("/api/admin/access/groups", json={"name": "インフラ"}, headers=H)
    gid = next(g["group_id"] for g in r.json()["groups"] if g["name"] == "インフラ")
    assert client.post(f"/api/admin/access/groups/{gid}/members", json={"account_id": "10003"}, headers=H).status_code == 201
    assert client.post(f"/api/admin/access/groups/{gid}/grants", json={"role_id": role, "target_id": f"group:{gid}"}, headers=H).status_code == 201
    # S01（障害対応。情報システム部）をインフラのグループ専用・要る権限 infra.docs.view にする
    r = client.patch("/api/admin/documents/S01", json={"targets": [f"group:{gid}"], "required": "infra.docs.view"}, headers=H)
    assert r.status_code == 200, r.text
    assert r.json()["detail"] == "障害対応の手順の公開先 情報システム部→インフラ、要る権限 documents.view→infra.docs.view"
    login(client, "10003")
    assert "S01" in search_ids(client, "障害対応") and client.get("/api/documents/S01").status_code == 200
    login(client, "10004")                                 # 同じ営業部でも、グループにいない人は見えない
    assert "S01" not in search_ids(client, "障害対応") and client.get("/api/documents/S01").status_code == 404
    login(client, "10005")                                 # 情報システム部の管理者も、infra.docs.view がなければ見えない
    assert "S01" not in search_ids(client, "障害対応") and client.get("/api/documents/S01").status_code == 404
    ans = client.post("/api/ask", json={"question": "障害対応の手順を教えて"}, headers=H).json()
    assert "障害対応の手順" not in json.dumps(ans, ensure_ascii=False)       # 見られない文書の題名は返事に出さない
    entry = logs(client, "ask.chat")[0]
    assert "権限" in entry["detail"] and "障害対応の手順" not in entry["detail"]


def test_document_published_to_a_group_is_seen_by_its_members_only(client):
    # 社員の documents.view の対象は「メンバー自身の所属（部署と、入っているグループ）」。グループの公開先にも効く
    p = client.post("/api/admin/documents/preview", json={"doc_id": "H01", "targets": ["group:G-general"]}, headers=H).json()
    assert p["detail"] == "人事評価の運用手順の公開先 人事部→総務" and (p["gain"], p["lose"]) == (1, 1)
    assert client.patch("/api/admin/documents/H01", json={"targets": ["group:G-general"]}, headers=H).status_code == 200
    for no in PEOPLE:
        login(client, no)
        seen = no == "10002"                                  # 総務のメンバー（決まり：部署「総務部」）だけ
        assert ("H01" in search_ids(client, "人事評価")) is seen, no
        assert (client.get("/api/documents/H01").status_code == 200) is seen, no
        listed = "H01" in {d["doc_id"] for d in client.get("/api/documents").json()["documents"]}
        assert listed is (seen or no == "10005"), no          # 10005 は文書を扱える人なので、一覧には扱う文書として出る


def test_group_membership_counts_for_the_self_target(settings):
    import datetime as dt

    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])):
        pass
    db = sqlite3.connect(settings.db_path)
    with db:
        db.execute("INSERT INTO access_groups VALUES ('G-x', '読む会', '', NULL)")   # 役割を与えていないグループ
        db.execute("INSERT INTO group_members VALUES ('G-x', '10004', NULL, 't', 't')")
        db.execute("INSERT INTO group_members VALUES ('G-x', '10003', '2000-01-01', 't', 't')")   # 期限切れ
        access._bump(db)
    db.close()
    today = dt.date.today().isoformat()
    e = access.Engine(lambda: sqlite3.connect(str(settings.db_path)), today=lambda: today)
    assert e.can_view_document("10004", "営業部", "documents.view", ["group:G-x"]).allowed
    assert not e.can_view_document("10003", "営業部", "documents.view", ["group:G-x"]).allowed
    assert not e.can_view_document("10001", "経理部", "documents.view", ["group:G-x"]).allowed
    assert not e.can_view_document("10004", "営業部", "infra.docs.view", ["group:G-x"]).allowed   # 要る権限は別
    s = Store(settings.db_path)
    s.set_publication("H01", ["group:G-x"], "documents.view", False)
    assert s.can_view("H01", e.doc_scope("10004", "営業部")) and not s.can_view("H01", e.doc_scope("10003", "営業部"))
    s.close()


def test_upgrade_replaces_the_employee_target_once(settings):
    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])):
        pass
    db = sqlite3.connect(settings.db_path)
    with db:                                                  # 前の版のデータ：社員 × メンバー自身の部署、置き換えの印なし
        db.execute("UPDATE group_grants SET target_id = 'special:self_dept' WHERE target_id = 'special:self'")
        db.execute("DELETE FROM access_meta WHERE key = 'self_target'")
        db.execute("DELETE FROM targets WHERE target_id = 'special:self'")
    db.close()
    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])) as c:
        login(c, "10005")
        v = c.get("/api/admin/access").json()
        g = next(x for x in v["groups"] if x["name"] == "社員")
        assert [(x["role"], x["target"]) for x in g["grants"]] == [("社員", "メンバー自身の所属（部署と、入っているグループ）")]
        entry = c.get("/api/audit", params={"action": "roles.migrate"}).json()["entries"][0]
        assert entry["detail"] == "役割「社員」の対象を「メンバー自身の部署」から「メンバー自身の所属（部署と、入っているグループ）」に置き換えた（1件）"
    db = sqlite3.connect(settings.db_path)
    with db:                                                  # 画面で「メンバー自身の部署」に戻したら、起動し直しても戻したまま
        db.execute("UPDATE group_grants SET target_id = 'special:self_dept' WHERE target_id = 'special:self'")
    db.close()
    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])) as c:
        login(c, "10005")
        g = next(x for x in c.get("/api/admin/access").json()["groups"] if x["name"] == "社員")
        assert [x["target"] for x in g["grants"]] == ["メンバー自身の部署"]


# ---------------------------------------------------------------- 画面の API


def test_admin_list_shows_only_documents_you_can_manage(client):
    v = client.get("/api/admin/documents").json()
    assert len(v["documents"]) == 24 and {p["name"] for p in v["permissions"]} == {"documents.view", "infra.docs.view"}
    k01 = next(d for d in v["documents"] if d["doc_id"] == "K01")
    assert (k01["owner"], k01["target_names"], k01["required"]) == ("経理部", ["経理部"], "documents.view")
    p01 = next(d for d in v["documents"] if d["doc_id"] == "P01")
    assert p01["target_names"] == ["全社"] and p01["external"] is True
    assert {"全社", "部署", "グループ"} == {t["kind"] for t in v["publish_targets"]}
    # 経理部の文書担当（文書管理 × 経理部）は、経理部の文書だけを扱える
    roles = {r["name"]: r["role_id"] for r in client.get("/api/admin/access").json()["roles"]}
    r = client.post("/api/admin/access/groups", json={"name": "経理の文書担当"}, headers=H)
    gid = next(g["group_id"] for g in r.json()["groups"] if g["name"] == "経理の文書担当")
    client.post(f"/api/admin/access/groups/{gid}/members", json={"account_id": "10001"}, headers=H)
    client.post(f"/api/admin/access/groups/{gid}/grants", json={"role_id": roles["文書管理"], "target_id": "dept:経理部"}, headers=H)
    login(client, "10001")
    v = client.get("/api/admin/documents").json()
    assert {d["doc_id"] for d in v["documents"]} == {"A03", "K01", "K02"} and v["owners"] == ["経理部"]   # 持ち主が経理部の文書
    assert client.patch("/api/admin/documents/A01", json={"targets": ["dept:経理部"]}, headers=H).status_code == 404
    assert client.patch("/api/admin/documents/K01", json={"targets": ["dept:経理部", "dept:総務部"]}, headers=H).status_code == 200
    login(client, "10004")
    assert client.get("/api/admin/documents").status_code == 403


def test_publication_changes_without_rebuilding_and_are_audited(client, settings):
    db = sqlite3.connect(settings.db_path)
    chunks_before = db.execute("SELECT chunk_id, text FROM chunks WHERE doc_id = 'K01' ORDER BY chunk_id").fetchall()
    login(client, "10004")
    assert "K01" not in search_ids(client, "月次決算")
    login(client, "10005")
    r = client.patch("/api/admin/documents/K01", json={"targets": ["special:all"]}, headers=H)
    assert r.status_code == 200 and r.json()["detail"] == "月次決算の手順の公開先 経理部→全社"
    assert db.execute("SELECT chunk_id, text FROM chunks WHERE doc_id = 'K01' ORDER BY chunk_id").fetchall() == chunks_before   # 作り直さない
    login(client, "10004")
    assert "K01" in search_ids(client, "月次決算")                       # すぐに検索に効く
    entry = logs(client, "documents.publish")[0]
    assert entry["detail"] == "月次決算の手順の公開先 経理部→全社" and entry["target"] == "K01"
    body = chunks_before[0][1]
    assert body.split("\n", 1)[1][:15] not in json.dumps(logs(client, "documents"), ensure_ascii=False)   # 本文は残さない
    # 誤り：公開先なし・選べない公開先・選べない権限・変わったところなし
    for bad in ({"targets": []}, {"targets": ["system:x"]}, {"targets": ["special:all"], "required": "roles.assign"}, {"targets": ["special:all"]}):
        assert client.patch("/api/admin/documents/K01", json=bad, headers=H).status_code == 400, bad
    db.close()


def test_preview_counts_who_gains_and_loses_by_group_and_saves_nothing(client):
    r = client.post("/api/admin/documents/preview", json={"doc_id": "A01", "targets": ["dept:経理部"]}, headers=H)
    assert r.status_code == 200, r.text
    p = r.json()
    assert p["detail"] == "就業規則（抜粋）の公開先 全社→経理部" and (p["gain"], p["lose"]) == (0, 5)
    everyone = next(g for g in p["groups"] if g["name"] == "社員")
    assert (everyone["members"], everyone["lose"], everyone["can_see"]) == (6, 5, 1)
    p = client.post("/api/admin/documents/preview", json={"doc_id": "K01", "targets": ["special:all"]}, headers=H).json()
    assert (p["gain"], p["lose"]) == (5, 0)
    p = client.post("/api/admin/documents/preview", json={"doc_id": "K01", "action": "remove"}, headers=H).json()
    assert (p["gain"], p["lose"]) == (0, 1) and p["detail"].startswith("月次決算の手順を外した")
    assert logs(client, "documents") == []                                # 試しに当てただけ
    login(client, "10004")
    assert "A01" in search_ids(client, "就業規則")


def test_remove_and_restore(client):
    assert client.post("/api/admin/documents/K01/remove", headers=H).status_code == 200
    assert client.post("/api/admin/documents/K01/remove", headers=H).status_code == 409
    assert "K01" not in {d["doc_id"] for d in client.get("/api/admin/documents").json()["documents"]}
    removed = client.get("/api/admin/documents", params={"removed": True}).json()["documents"]
    assert [d["doc_id"] for d in removed] == ["K01"] and removed[0]["removed_by"] == "10005"
    login(client, "10001")
    assert "K01" not in search_ids(client, "月次決算") and client.get("/api/documents/K01").status_code == 404
    assert "K01" not in {d["doc_id"] for d in client.get("/api/documents").json()["documents"]}
    login(client, "10005")
    assert client.post("/api/admin/documents/K01/restore", headers=H).status_code == 200
    login(client, "10001")
    assert "K01" in search_ids(client, "月次決算")
    details = [e["detail"] for e in reversed(logs(client, "documents")) if e["result"] == "成功"]
    assert details == ["月次決算の手順を外した（検索に出さない。公開先 経理部）", "月次決算の手順を戻した（公開先 経理部）"]


def wait_job(c, job_id) -> dict:
    import time

    for _ in range(200):
        job = c.get(f"/api/jobs/{job_id}").json()
        if job["status"] in ("完了", "失敗", "取り消し"):
            return job
        time.sleep(0.05)
    raise AssertionError("ジョブが終わりません")


def test_ingest_with_chosen_owner_and_publication(settings, tmp_path):
    src = tmp_path / "inbox" / "infra"
    src.mkdir(parents=True)
    (src / "x.md").write_text("---\nid: X01\ntitle: 構成書\n---\n## ネットワーク\nVPN の機器は2台です\n", encoding="utf-8")
    settings.ingest_roots = [tmp_path / "inbox"]
    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])) as c:
        login(c, "10005")
        # ファイルの先頭に access がないので、ファイルの書き方では取り込めない（閉じる側に倒す）
        job = wait_job(c, c.post("/api/documents/ingest", json={"source": str(src)}, headers=H).json()["job_id"])
        assert job["result"]["added"] == [] and "access" in job["result"]["errors"][0]
        r = c.post("/api/documents/ingest", json={"source": str(src), "mode": "chosen", "owner": "情報システム部",
                                                  "targets": ["dept:情報システム部"], "required": "documents.view"}, headers=H)
        job = wait_job(c, r.json()["job_id"])
        assert job["result"]["added"] == ["X01"], job
        d = next(x for x in c.get("/api/admin/documents").json()["documents"] if x["doc_id"] == "X01")
        assert (d["owner"], d["targets"], d["decided_by"]) == ("情報システム部", ["dept:情報システム部"], "screen")
        assert "X01" in search_ids(c, "VPN")
        login(c, "10003")
        assert "X01" not in search_ids(c, "VPN")
        entries = logs(c, "documents.ingest")
        assert "足した 1件（X01）" in entries[0]["detail"] and "公開先 情報システム部" in entries[0]["detail"]
        assert "VPN" not in entries[0]["detail"]
        # 扱えない部署・選べない公開先は、受け付けない
        assert c.post("/api/documents/ingest", json={"source": str(src), "mode": "chosen", "owner": "", "targets": ["special:all"]},
                      headers=H).status_code == 400
        assert c.post("/api/documents/ingest", json={"source": str(src), "mode": "chosen", "owner": "経理部", "targets": ["nosuch"]},
                      headers=H).status_code == 400


def test_reingest_keeps_what_the_screen_decided(tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    f = src / "a.md"
    f.write_text("---\nid: A\ntitle: 文書\naccess: 経理部\nowner: 経理部\n---\n## 節\n合言葉はりんご\n", encoding="utf-8")
    s = Store(tmp_path / "i.db")
    s.ingest(src)
    s.set_publication("A", ["special:all"], "documents.view", False)       # 画面で直した
    f.write_text("---\nid: A\ntitle: 文書\naccess: 人事部\nowner: 経理部\n---\n## 節\n合言葉はみかん\n", encoding="utf-8")
    assert s.ingest(src).updated == ["A"]
    assert s.doc_settings("A")["targets"] == ["special:all"]                # 画面で決めたものを残す
    assert [c.text for c in s.chunks_for("営業部") if c.doc_id == "A"] == ["文書 / 節\n合言葉はみかん"]
    # 画面で選んで取り込んだ新しい文書も、画面で決めたもの
    (src / "b.md").write_text("---\nid: B\ntitle: B\n---\n## 節\nほげ\n", encoding="utf-8")
    s.ingest(src, DocSettings(owner="経理部", targets=["dept:経理部"]), owners={"経理部"})
    assert s.doc_settings("B")["owner"] == "経理部" and s.doc_settings("B")["decided_by"] == "screen"
    assert DocScope.departments("経理部") == DocScope.of({"documents.view": {"dept:経理部"}})
    s.close()
