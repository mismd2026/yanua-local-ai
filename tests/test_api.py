"""Web画面向けの API（yanua/api）のテスト。Ollama は使わず、決まった返事をする偽のモデルで確かめる。"""

import json
import re
import sys
import threading
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yanua import hr  # noqa: E402
from yanua.api import Settings, create_app  # noqa: E402
from yanua.api.jobs import CANCELLED, DONE, FAILED, QUEUED, JobQueue  # noqa: E402
from yanua.api.roles import RoleConfigError, load_roles  # noqa: E402
from yanua.store import Store  # noqa: E402

DEMO = ROOT / "demo" / "company"
H = {"X-Yanua-Client": "test"}

# デモの人事データ：10001 山田（経理部）、10002 佐藤（総務部）、10004 高橋（営業部）、
# 10005 田中（情報システム部・管理者）、10007 渡辺（退職）


class FakeLLM:
    """社内文書の回答には資料の番号つきの文を、ヘルプデスクの項目の取り出しにはJSONを返す。"""

    name = "fake"

    def chat(self, messages):
        text = messages[-1]["content"]
        if "JSONだけを返して" in messages[0]["content"]:
            reply = text.split("返事:")[-1]
            loc = re.search(r"本社\d階", reply)
            return json.dumps({
                "device": "複合機" if "複合機" in reply else "不明",
                "location": loc.group(0) if loc else "不明",
                "scope": "部署・複数人" if "全員" in reply else "不明",
                "business": "止まっている" if "止まって" in reply else "不明",
            }, ensure_ascii=False)
        if "在宅勤務" in text:
            return "在宅勤務は週3日までです[1]。"
        if "パスワードを忘れた" in text:
            return "パスワード再設定の手順に従って再設定してください[1]。"
        return "わかりません"


@pytest.fixture
def settings(tmp_path):
    db = tmp_path / "index.db"
    s = Store(db)
    assert s.ingest(DEMO / "docs").errors == []
    hr.import_employees(s.db, DEMO / "hr" / "hr_export_sample.csv", hr.load_mapping(DEMO / "hr" / "mapping_sample.json"))
    s.close()
    return Settings(db_path=db, chat_db_path=tmp_path / "chat.db", helpdesk_data=DEMO / "flows" / "helpdesk",
                    roles_path=ROOT / "config" / "roles.json", dev_login=True, ingest_roots=[DEMO])


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeLLM())) as c:
        yield c


def login(c: TestClient, employee_no: str) -> None:
    r = c.post("/api/auth/dev-login", json={"employee_no": employee_no}, headers=H)
    assert r.status_code == 200, r.text


# ---------------------------------------------------------------- ログイン


def test_login_me_logout(client):
    assert client.get("/api/me").status_code == 401
    login(client, "10004")
    me = client.get("/api/me").json()
    assert me["name"] == "高橋 美咲" and me["dept"] == "営業部"
    assert me["roles"] == ["社員"]
    assert "documents.ingest" not in me["permissions"]
    client.post("/api/auth/logout", headers=H)
    assert client.get("/api/me").status_code == 401


def test_roles_from_config(client):
    login(client, "10005")
    assert client.get("/api/me").json()["roles"] == ["社員", "情シス", "管理者"]
    login(client, "10002")
    assert client.get("/api/me").json()["roles"] == ["社員", "総務"]


def test_retired_or_unknown_cannot_log_in(client):
    for no in ("10007", "99999"):
        r = client.post("/api/auth/dev-login", json={"employee_no": no}, headers=H)
        assert r.status_code == 401
        assert r.json()["error"]["code"] == "login_failed"


def test_session_ends_when_employee_leaves(client, settings):
    login(client, "10004")
    s = Store(settings.db_path)
    with s.db:
        s.db.execute("UPDATE employees SET active = 0 WHERE employee_no = '10004'")
    s.close()
    assert client.get("/api/me").status_code == 401


def test_cookie_is_httponly(client):
    r = client.post("/api/auth/dev-login", json={"employee_no": "10004"}, headers=H)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "SameSite=lax" in cookie


def test_dev_login_only_on_localhost(settings):
    settings.host = "0.0.0.0"
    with pytest.raises(ValueError):
        create_app(settings, FakeLLM())


def test_without_dev_login(settings):
    settings.dev_login = False
    with TestClient(create_app(settings, FakeLLM())) as c:
        assert c.post("/api/auth/dev-login", json={"employee_no": "10004"}, headers=H).status_code == 404
        r = c.get("/api/auth/login")
        assert r.status_code == 501 and r.json()["error"]["code"] == "sso_not_configured"
        assert c.get("/api/docs").status_code == 404


def test_cookie_secure_outside_dev(settings):
    assert not settings.cookie_secure
    settings.dev_login = False
    assert settings.cookie_secure


# ---------------------------------------------------------------- 共通の決まり


def test_csrf_header_required(client):
    r = client.post("/api/auth/dev-login", json={"employee_no": "10004"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"


def test_error_shape(client):
    login(client, "10004")
    for r in (client.get("/api/nothing"), client.post("/api/ask", json={}, headers=H),
              client.post("/api/documents/ingest", json={"source": "demo/company/docs"}, headers=H)):
        err = r.json()["error"]
        assert set(err) == {"code", "message"} and err["message"]


def test_health(client):
    r = client.get("/health").json()
    assert r["status"] == "ok" and r["documents"] > 0


# ---------------------------------------------------------------- 質問・検索・文書


def test_ask_uses_department_of_logged_in_user(client):
    assert client.post("/api/ask", json={"question": "在宅勤務は週に何日？"}, headers=H).status_code == 401
    login(client, "10004")
    r = client.post("/api/ask", json={"question": "在宅勤務は週に何日までできますか？"}, headers=H).json()
    assert not r["refused"] and r["sources"][0]["doc_id"] == "A01"


def test_ask_ignores_department_in_body(client):
    """本文で部署を渡しても使わない（ログインした人の部署だけを使う）。"""
    login(client, "10004")
    r = client.post("/api/ask", json={"question": "人事評価の入力期限は？", "department": "人事部"}, headers=H).json()
    assert not {s["doc_id"] for s in r["sources"]} & {"H01", "H02"}


def test_search_respects_department(client, settings):
    s = Store(settings.db_path)
    hr_docs = {d["doc_id"] for d in s.documents() if d["access"] == ["人事部"]}
    s.close()
    assert hr_docs
    login(client, "10004")
    hits = client.get("/api/search", params={"q": "人事評価 入力期限"}).json()["hits"]
    assert not {h["doc_id"] for h in hits} & hr_docs
    docs = client.get("/api/documents").json()["documents"]
    assert not {d["doc_id"] for d in docs} & hr_docs
    assert all("path" not in d for d in docs)


def test_admin_sees_all_documents_with_path(client):
    login(client, "10005")
    docs = client.get("/api/documents").json()["documents"]
    assert len(docs) >= 20 and all("path" in d for d in docs)


# ---------------------------------------------------------------- 文書の取り込み（ジョブ）


def test_ingest_only_admin(client):
    login(client, "10004")
    r = client.post("/api/documents/ingest", json={"source": str(DEMO / "public")}, headers=H)
    assert r.status_code == 403


def test_ingest_job(client):
    login(client, "10005")
    r = client.post("/api/documents/ingest", json={"source": str(DEMO / "public")}, headers=H)
    assert r.status_code == 202
    job = wait_job(client, r.json()["job_id"])
    assert job["status"] == DONE and job["progress"] == 100
    assert len(job["result"]["added"]) == 4


def test_ingest_outside_roots_is_rejected(client, tmp_path):
    login(client, "10005")
    for src in (str(tmp_path), "/etc", str(DEMO / ".." / ".." / "yanua")):
        r = client.post("/api/documents/ingest", json={"source": src}, headers=H)
        assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_source"


def wait_job(client, job_id: str, timeout: float = 10) -> dict:
    import time

    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in (QUEUED, "実行中"):
            return job
        time.sleep(0.05)
    raise AssertionError("ジョブが終わりません")


def test_jobs_of_others_are_hidden(client):
    login(client, "10005")
    job_id = client.post("/api/documents/ingest", json={"source": str(DEMO / "public")}, headers=H).json()["job_id"]
    wait_job(client, job_id)
    login(client, "10004")
    r = client.get(f"/api/jobs/{job_id}")
    assert r.status_code == 404
    assert client.get("/api/jobs").json()["jobs"] == []
    assert client.get("/api/jobs", params={"all": True}).status_code == 403


# ---------------------------------------------------------------- ジョブの仕組み


def test_job_queue_success_failure_cancel(tmp_path):
    q = JobQueue(tmp_path / "jobs.db")
    gate = threading.Event()

    def ok(ctx, params):
        ctx.progress(50, "半分")
        return {"n": params["n"] * 2}

    def ng(ctx, params):
        raise RuntimeError("読み込めません")

    def slow(ctx, params):
        gate.wait(5)
        ctx.progress(10, "続き")   # 取り消されていればここで止まる
        return {}

    for k, f in (("ok", ok), ("ng", ng), ("slow", slow)):
        q.register(k, f)

    a = q.submit("ok", "u1", {"n": 21})["job_id"]
    q.run_one(a)
    assert q.get(a)["status"] == DONE and q.get(a)["result"] == {"n": 42}

    b = q.submit("ng", "u1", {})["job_id"]
    q.run_one(b)
    assert q.get(b)["status"] == FAILED and "読み込めません" in q.get(b)["error"]

    c = q.submit("ok", "u1", {"n": 1})["job_id"]
    q.cancel(c)
    q.run_one(c)
    assert q.get(c)["status"] == CANCELLED and q.get(c)["result"] is None

    d = q.submit("slow", "u1", {})["job_id"]
    t = threading.Thread(target=q.run_one, args=(d,))
    t.start()
    q.cancel(d)
    gate.set()
    t.join(5)
    assert q.get(d)["status"] == CANCELLED

    with pytest.raises(ValueError):
        q.submit("unknown", "u1", {})
    assert [j["job_id"] for j in q.list("u1")][0] == d


def test_unfinished_jobs_fail_after_restart(tmp_path):
    q = JobQueue(tmp_path / "jobs.db")
    q.register("ok", lambda ctx, p: {})
    job_id = q.submit("ok", "u1", {})["job_id"]
    q2 = JobQueue(tmp_path / "jobs.db")
    job = q2.get(job_id)
    assert job["status"] == FAILED and "再起動" in job["error"]


# ---------------------------------------------------------------- 役割の設定


def test_role_config_errors(tmp_path):
    p = tmp_path / "roles.json"
    p.write_text(json.dumps({"roles": {"社長": {"employees": ["1"]}}}), encoding="utf-8")
    with pytest.raises(RoleConfigError):
        load_roles(p)
    p.write_text(json.dumps({"roles": {"総務": {"departments": "総務部"}}}), encoding="utf-8")
    with pytest.raises(RoleConfigError):
        load_roles(p)
    assert load_roles(tmp_path / "none.json").roles_for("1", "営業部") == ["社員"]


# ---------------------------------------------------------------- ヘルプデスク


def chat(client, text, sid):
    r = client.post(f"/api/chat/sessions/{sid}/messages", json={"text": text}, headers=H)
    assert r.status_code == 200, r.text
    return r.json()


def test_helpdesk_starts_from_logged_in_account(client):
    """個人アカウントのある人（佐藤・総務部）は、名前の確認から始まる。"""
    login(client, "10002")
    r = client.post("/api/chat/sessions", json={}, headers=H)
    assert r.status_code == 201
    first = r.json()
    assert "総務部の佐藤 花子さんでよろしいですか" in first["reply"] and not first["finished"]
    sid = first["session_id"]
    assert "お困り" in chat(client, "はい", sid)["reply"]
    last = chat(client, "パスワードを忘れました", sid)
    assert "解決しましたか" in last["reply"]
    end = chat(client, "はい", sid)
    assert end["finished"] and "解決してよかった" in end["reply"]
    r = client.post(f"/api/chat/sessions/{sid}/messages", json={"text": "もう一度"}, headers=H)
    assert r.status_code == 409


def test_helpdesk_ticket_with_shared_account(client):
    """代表アカウント（開発用ログインのときだけ指定できる）からは、従業員番号を聞いて票まで進む。"""
    login(client, "10004")
    sid = client.post("/api/chat/sessions", json={"account": "eigyo-common"}, headers=H).json()["session_id"]
    for text in ("10004", "高橋 美咲", "プリンターから印刷できません",
                 "本社2階の営業部の複合機です。部署の全員が印刷できず、業務が止まっています。"):
        r = chat(client, text, sid)
    if "解決しましたか" in r["reply"]:
        r = chat(client, "いいえ", sid)
    end = chat(client, "内線4100、9時から18時", sid)
    assert end["finished"] and end["ticket_id"].startswith("HD-")


def test_helpdesk_session_belongs_to_its_owner(client):
    login(client, "10002")
    sid = client.post("/api/chat/sessions", json={}, headers=H).json()["session_id"]
    login(client, "10004")
    r = client.post(f"/api/chat/sessions/{sid}/messages", json={"text": "はい"}, headers=H)
    assert r.status_code == 404
    assert client.delete(f"/api/chat/sessions/{sid}", headers=H).status_code == 404
    unknown = client.post("/api/chat/sessions/xxxx/messages", json={"text": "はい"}, headers=H)
    assert unknown.json() == r.json()


def test_helpdesk_continues_after_restart(settings):
    """会話の状態は SQLite にあるので、サーバーを作り直しても続けられる。"""
    with TestClient(create_app(settings, FakeLLM())) as c:
        login(c, "10002")
        sid = c.post("/api/chat/sessions", json={}, headers=H).json()["session_id"]
        cookies = dict(c.cookies)
    with TestClient(create_app(settings, FakeLLM()), cookies=cookies) as c:
        assert "お困り" in chat(c, "はい", sid)["reply"]


def test_account_override_needs_dev_login(settings):
    settings.dev_login = False
    app = create_app(settings, FakeLLM())
    with TestClient(app) as c:
        # SSO がまだないので、セッションを直接作ってログインした状態にする
        import sqlite3

        from yanua.api import auth

        db = sqlite3.connect(settings.db_path)
        token = auth.create_session(db, "10004", 1)
        db.close()
        c.cookies.set(auth.COOKIE, token)
        r = c.post("/api/chat/sessions", json={"account": "eigyo-common"}, headers=H)
        assert r.status_code == 403
