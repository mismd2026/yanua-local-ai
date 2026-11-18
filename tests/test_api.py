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
from yanua.api.jobs import CANCELLED, DONE, FAILED, QUEUED, JobFailed, JobQueue  # noqa: E402
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
                    roles_path=ROOT / "config" / "roles.json", dev_login=True, ingest_roots=[DEMO],
                    web_dist=None, settings_dir=tmp_path / "settings")   # 画面で変えた設定は作業用の場所に


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


def test_openapi_documents_client_header(client):
    schema = client.get("/api/openapi.json").json()
    assert schema["components"]["securitySchemes"]["client"]["name"] == "X-Yanua-Client"
    assert schema["security"] == [{"client": []}]


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
        raise JobFailed("読み込めません", "管理者向け：python -m yanua ingest をやり直す")

    def broken(ctx, params):
        raise RuntimeError("/srv/data/secret.db が開けません")

    def slow(ctx, params):
        gate.wait(5)
        ctx.progress(10, "続き")   # 取り消されていればここで止まる
        return {}

    for k, f in (("ok", ok), ("ng", ng), ("broken", broken), ("slow", slow)):
        q.register(k, f)

    a = q.submit("ok", "u1", {"n": 21})["job_id"]
    q.run_one(a)
    assert q.get(a)["status"] == DONE and q.get(a)["result"] == {"n": 42}

    b = q.submit("ng", "u1", {})["job_id"]
    q.run_one(b)
    assert q.get(b)["status"] == FAILED and q.get(b)["error"] == "読み込めません"
    # 利用者に見せる文とログに残す文を分ける：コマンドや例外の中身（置き場所など）は画面に出さない
    assert "python" not in q.get(b)["error"]
    e = q.submit("broken", "u1", {})["job_id"]
    q.run_one(e)
    assert q.get(e)["status"] == FAILED and "secret" not in q.get(e)["error"] and e in q.get(e)["error"]

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


def test_helpdesk_skips_identity_for_logged_in_user(client):
    """ログインしている人は本人確認を聞かれず、お困りごとから始まる。"""
    login(client, "10002")
    r = client.post("/api/chat/sessions", json={}, headers=H)
    assert r.status_code == 201
    first = r.json()
    assert first["reply"].startswith("佐藤 花子さん、ありがとうございます。どのようなことでお困りですか") and not first["finished"]
    sid = first["session_id"]
    last = chat(client, "パスワードを忘れました", sid)
    assert "解決しましたか" in last["reply"]
    end = chat(client, "はい", sid)
    assert end["finished"] and "解決してよかった" in end["reply"]
    r = client.post(f"/api/chat/sessions/{sid}/messages", json={"text": "もう一度"}, headers=H)
    assert r.status_code == 409


def test_helpdesk_ticket_records_login(client, settings):
    login(client, "10004")
    sid = client.post("/api/chat/sessions", json={}, headers=H).json()["session_id"]
    for text in ("プリンターから印刷できません",
                 "本社2階の営業部の複合機です。部署の全員が印刷できず、業務が止まっています。"):
        r = chat(client, text, sid)
    if "解決しましたか" in r["reply"]:
        r = chat(client, "いいえ", sid)
    end = chat(client, "内線4100、9時から18時", sid)
    assert end["finished"] and end["ticket_id"].startswith("HD-")
    s = Store(settings.db_path)
    t = json.loads(s.db.execute("SELECT payload FROM helpdesk_tickets").fetchone()[0])
    s.close()
    assert t["requester"]["verified"] == "ログイン" and t["requester"]["dept"] == "営業部"
    assert t["priority"] == "高"


def test_helpdesk_ticket_with_shared_account(client):
    """代表アカウント（開発用ログインのときだけ指定できる）からは、本人確認を聞いて票まで進む。"""
    login(client, "10004")
    first = client.post("/api/chat/sessions", json={"account": "eigyo-common"}, headers=H).json()
    assert "本人確認を行う都合上" in first["reply"]
    sid = first["session_id"]
    for text in ("営業部の高橋美咲です。10004です。", "プリンターから印刷できません",
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
        assert "解決しましたか" in chat(c, "パスワードを忘れました", sid)["reply"]


def test_many_expired_sessions_are_removed(settings):
    """期限切れの会話が2つ以上あっても、消すときに「database is locked」にならない（実機で起きた）。"""
    import sqlite3

    with TestClient(create_app(settings, FakeLLM())) as c:
        login(c, "10002")
        sids = [c.post("/api/chat/sessions", json={}, headers=H).json()["session_id"] for _ in range(3)]
    db = sqlite3.connect(settings.chat_db_path)
    with db:
        db.execute("UPDATE chat_sessions SET last_used = '2000-01-01T00:00:00+00:00'")
    db.close()
    with TestClient(create_app(settings, FakeLLM())) as c:
        login(c, "10002")
        r = c.post("/api/chat/sessions", json={}, headers=H)
        assert r.status_code == 201
        for sid in sids:   # 消えた会話には送れない
            assert c.post(f"/api/chat/sessions/{sid}/messages", json={"text": "はい"}, headers=H).status_code == 404


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


# ---------------------------------------------------------------- 出典を開く（S-10）


def test_document_sections_only_for_viewable(client):
    login(client, "10004")   # 営業部
    doc = client.get("/api/documents/A01").json()
    assert doc["doc_id"] == "A01" and doc["title"] and doc["sections"]
    assert all(set(s) == {"section", "text"} for s in doc["sections"])
    assert "path" not in doc
    # 検索のために付けた「文書名 / 見出し」の行は本文に入れない
    assert not any(s["text"].startswith(doc["title"]) for s in doc["sections"])
    # 経理部だけの文書は、ない文書と同じ返事にする
    hidden = client.get("/api/documents/K01")
    missing = client.get("/api/documents/ZZZ99")
    assert hidden.status_code == missing.status_code == 404
    assert hidden.json() == missing.json()
    login(client, "10001")   # 経理部
    assert client.get("/api/documents/K01").status_code == 200


def test_document_needs_login(client):
    assert client.get("/api/documents/A01").status_code == 401


# ---------------------------------------------------------------- Web画面の配信


@pytest.fixture
def web_client(settings, tmp_path):
    dist = tmp_path / "dist"
    (dist / "assets").mkdir(parents=True)
    (dist / "index.html").write_text("<!doctype html><title>Yanua</title>", encoding="utf-8")
    (dist / "assets" / "app-abc.js").write_text("console.log(1)", encoding="utf-8")
    (tmp_path / "secret.txt").write_text("外のファイル", encoding="utf-8")
    settings.web_dist = dist
    with TestClient(create_app(settings, FakeLLM())) as c:
        yield c


def test_web_serves_index_for_screens(web_client):
    for path in ("/", "/login", "/chat"):
        r = web_client.get(path)
        assert r.status_code == 200 and "<title>Yanua</title>" in r.text, path
        assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
    r = web_client.get("/assets/app-abc.js")
    assert r.status_code == 200 and "immutable" in r.headers["cache-control"]
    assert web_client.head("/").status_code == 200


def test_web_does_not_cover_api_kiosk_or_missing_files(web_client):
    r = web_client.get("/api/nothing")
    assert r.status_code == 404 and r.json()["error"]["code"] == "not_found"
    assert web_client.get("/kiosk").status_code == 404     # 受付端末の画面を組み立てていなければ、何も返さない
    assert web_client.get("/assets/missing.js").status_code == 404
    assert web_client.get("/health").json()["status"] == "ok"
    assert web_client.get("/api/me").status_code == 401


def test_web_does_not_serve_outside_dist(web_client):
    for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/assets/..%2f..%2fsecret.txt"):
        r = web_client.get(path)
        assert "外のファイル" not in r.text, path


def test_kiosk_gets_only_the_kiosk_page(settings, tmp_path):
    dist = tmp_path / "dist"
    dist.mkdir()
    (dist / "index.html").write_text("<title>社内</title>", encoding="utf-8")
    (dist / "kiosk.html").write_text("<title>受付</title>", encoding="utf-8")
    settings.web_dist = dist
    with TestClient(create_app(settings, FakeLLM())) as c:
        for path in ("/kiosk", "/kiosk/", "/kiosk/code"):
            r = c.get(path)
            assert r.status_code == 200 and "受付" in r.text and "社内" not in r.text, path
            assert "frame-ancestors 'none'" in r.headers["content-security-policy"]
        assert "社内" in c.get("/").text
        assert c.get("/kiosk/missing.js").status_code == 404


def test_default_ingest_roots_are_docs_and_public():
    from yanua.api import Settings

    assert Settings().ingest_roots == [Path("demo/company/docs"), Path("demo/company/public")]


def test_no_web_without_build(client):
    assert client.get("/").status_code == 404


# ---------------------------------------------------------------- 回答の逐次表示（SSE）


def read_sse(text: str) -> list[tuple[str, dict]]:
    out = []
    for block in text.strip().split("\n\n"):
        lines = [l for l in block.splitlines() if not l.startswith(":")]
        if not lines:
            continue
        event = next(l[7:] for l in lines if l.startswith("event: "))
        data = json.loads(next(l[6:] for l in lines if l.startswith("data: ")))
        out.append((event, data))
    return out


def test_ask_stream_sends_only_progress_until_checked(client):
    login(client, "10004")
    r = client.post("/api/ask/stream", json={"question": "在宅勤務は週に何日までできますか？"}, headers=H)
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    events = read_sse(r.text)
    assert [e for e, _ in events] == ["progress", "progress", "progress", "result"]
    assert [d["stage"] for _, d in events[:3]] == ["search", "answer", "check"]
    # 途中のイベントには段階と文言だけ。LLM の文は入らない
    assert all(set(d) == {"stage", "message"} for _, d in events[:3])
    assert "週3日" not in "".join(json.dumps(d, ensure_ascii=False) for _, d in events[:3])
    result = events[-1][1]
    assert result["answer"] == "在宅勤務は週3日までです[1]。" and result["sources"] and "elapsed_sec" in result


def test_ask_stream_refusal_never_shows_model_text(client):
    login(client, "10004")
    events = read_sse(client.post("/api/ask/stream", json={"question": "社員食堂の今日のメニューは？"}, headers=H).text)
    result = events[-1]
    assert result[0] == "result" and result[1]["refused"] is True and result[1]["sources"] == []
    assert all(d.get("answer") != "わかりません" for _, d in events)


def test_ask_stream_checks_before_streaming(client):
    assert client.post("/api/ask/stream", json={"question": "在宅勤務は？"}, headers=H).status_code == 401
    login(client, "10004")
    r = client.post("/api/ask/stream", json={"question": "在宅勤務は？"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"
    r = client.post("/api/ask/stream", json={"question": ""}, headers=H)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_request"


def test_ask_stream_llm_error_is_an_error_event(settings):
    from yanua.llm import LLMError

    class DownLLM(FakeLLM):
        def chat(self, messages):
            raise LLMError("接続できません")

    with TestClient(create_app(settings, DownLLM())) as c:
        login(c, "10004")
        events = read_sse(c.post("/api/ask/stream", json={"question": "在宅勤務は週に何日？"}, headers=H).text)
    assert events[-1] == ("error", {"code": "llm_unavailable",
                                    "message": "回答を作るAIに接続できません。しばらくしてからやり直してください"})
    assert [e for e, _ in events].count("result") == 0



# ---------------------------------------------------------------- ジョブの一覧（S-03）


def test_ingest_sources_only_admin(client):
    login(client, "10004")
    assert client.get("/api/documents/ingest/sources").status_code == 403
    login(client, "10005")
    sources = client.get("/api/documents/ingest/sources").json()["sources"]
    paths = [s["path"] for s in sources]
    assert str(DEMO.resolve()) in paths and str((DEMO / "docs").resolve()) in paths
    # 一覧のフォルダは、どれも取り込みで受け付けられる
    r = client.post("/api/documents/ingest", json={"source": str((DEMO / "public").resolve())}, headers=H)
    assert r.status_code == 202
    job = wait_job(client, r.json()["job_id"])
    assert job["params"] == {"source": str((DEMO / "public").resolve())}


def test_job_list_all_only_admin(client):
    login(client, "10005")
    job_id = client.post("/api/documents/ingest", json={"source": str(DEMO / "public")}, headers=H).json()["job_id"]
    wait_job(client, job_id)
    assert job_id in [j["job_id"] for j in client.get("/api/jobs?all=true").json()["jobs"]]
    login(client, "10004")
    assert client.get("/api/jobs?all=true").status_code == 403
    assert client.get("/api/jobs").json()["jobs"] == []
    assert client.delete(f"/api/jobs/{job_id}", headers=H).status_code == 404
