"""監査ログ（K-009）のテスト。Ollama は使わない。

- 何を記録するか：ログイン・ログアウト・ログインの失敗、権限で断ったこと、ジョブ、受付端末、設定、仕分けの実行、ダウンロード
- 何を残さないか：トークン・入力したコード（どの設定でも）。本文は設定で on にしたときだけ
- 足すだけ（API にも DB にも、消す・書き換える道がない）。保存期間を過ぎたものだけ消え、消したことが残る
- 記録に失敗しても、元の操作は止まらない
"""

import json
import shutil
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api import DEMO, H, FakeLLM, login  # noqa: E402
from test_api import settings as base_settings  # noqa: E402,F401  （fixture を使う）
from test_api_stage3 import FakeTranscriber, make_meeting, upload, wait_job  # noqa: E402

from yanua import audit  # noqa: E402
from yanua.api import auth, create_app  # noqa: E402
from yanua.api.jobs import JobFailed, JobQueue  # noqa: E402

PAPER = DEMO / "paper"
ADMIN, GA, EMP = "10005", "10002", "10004"     # 田中（管理者）、佐藤（総務）、高橋（社員）


@pytest.fixture
def settings(base_settings, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in ("scan_0001.pdf", "scan_0002.pdf", "scan_0005.pdf"):
        shutil.copy2(PAPER / "inbox" / name, inbox / name)
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({"roles": {"総務": {"departments": ["総務部"]}, "管理者": {"employees": [ADMIN]}}},
                                ensure_ascii=False), encoding="utf-8")
    base_settings.roles_path = roles
    base_settings.work_dir = tmp_path / "work"
    base_settings.paper_inbox = inbox
    base_settings.paper_out = tmp_path / "sorted"
    base_settings.paper_rules = tmp_path / "paper" / "rules.json"
    shutil.copytree(PAPER / "templates", tmp_path / "paper" / "templates")
    shutil.copy2(PAPER / "rules.json", base_settings.paper_rules)
    base_settings.meeting_template = DEMO / "meeting" / "templates" / "議事録.docx"
    base_settings.today = date(2026, 11, 16)
    base_settings.kiosk_min_seconds = 0.0
    base_settings.kiosk_max_failures = 3
    return base_settings


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeLLM(), transcriber_factory=FakeTranscriber)) as c:
        yield c


def entries(client, **params) -> list[dict]:
    """監査ログを管理者として読む（読んだこと自体も1件増える）。"""
    login(client, ADMIN)
    r = client.get("/api/audit", params={"per_page": 200, **params})
    assert r.status_code == 200, r.text
    return r.json()["entries"]


def actions(rows) -> list[str]:
    return [r["action"] for r in rows]


def auditor(client) -> audit.Auditor:
    return client.app.state.audit


# ---------------------------------------------------------------- ログイン・権限


def test_login_logout_and_failed_login(client, settings):
    login(client, EMP)
    client.post("/api/auth/logout", headers=H)
    assert client.post("/api/auth/dev-login", json={"employee_no": "99999"}, headers=H).status_code == 401
    rows = entries(client, action="auth")
    by = {(r["action"], r["result"], r["actor"]): r for r in rows}
    ok = by[("auth.login", "成功", EMP)]
    assert ok["actor_name"] == "高橋 美咲" and ok["actor_kind"] == "社員" and ok["ip"] == "testclient"
    assert ok["detail"] == "開発用ログイン"
    assert by[("auth.logout", "成功", EMP)]["actor_name"] == "高橋 美咲"
    failed = by[("auth.login", "失敗", "なし")]       # ログイン前は「なし」
    assert failed["actor_name"] == "なし" and failed["target"] == "99999"
    assert settings.audit_path == settings.db_path.parent / "audit.db" and settings.audit_path.is_file()


def test_forbidden_is_recorded_with_the_operation(client):
    login(client, EMP)
    assert client.get("/api/audit").status_code == 403          # 管理者だけ（404 ではなく 403）
    assert client.get("/api/paper/inbox").status_code == 403
    assert client.get("/api/jobs?all=true").status_code == 403
    rows = [r for r in entries(client, action="access.denied") if r["actor"] == EMP]
    assert {r["result"] for r in rows} == {"拒否"}
    details = " ".join(r["detail"] for r in rows)
    assert "「audit.view」" in details and "「paper」" in details and "「jobs.all」" in details
    assert {r["target"] for r in rows} >= {"GET /api/audit", "GET /api/paper/inbox"}


def test_audit_view_filters_paging_and_is_recorded(client):
    for who in (EMP, GA, EMP):
        login(client, who)
    login(client, ADMIN)
    page1 = client.get("/api/audit", params={"action": "auth.login", "per_page": 2}).json()
    assert page1["total"] == 4 and len(page1["entries"]) == 2 and page1["keep_days"] == 365 and page1["keep_body"] is False
    page2 = client.get("/api/audit", params={"action": "auth.login", "per_page": 2, "page": 2}).json()
    newest_first = [e["id"] for e in page1["entries"] + page2["entries"]]
    assert newest_first == sorted(newest_first, reverse=True)
    assert page1["entries"][0]["actor"] == ADMIN
    # した人は番号か名前の一部
    assert {e["actor"] for e in client.get("/api/audit", params={"actor": "高橋"}).json()["entries"]} == {EMP}
    assert {e["actor"] for e in client.get("/api/audit", params={"actor": GA}).json()["entries"]} == {GA}
    # 操作は先頭の区切り（auth → auth.login・auth.logout）。「auth.log」のような途中までは当てない
    assert client.get("/api/audit", params={"action": "auth.log"}).json()["total"] == 0
    assert client.get("/api/audit", params={"result": "失敗"}).json()["total"] == 0
    assert client.get("/api/audit", params={"result": "だめ"}).status_code == 400
    # 期間（日付はその日の終わりまで）
    today = datetime.now().astimezone().date()
    assert client.get("/api/audit", params={"since": str(today), "until": str(today), "action": "auth"}).json()["total"] == 4
    assert client.get("/api/audit", params={"until": str(today - timedelta(days=1))}).json()["total"] == 0
    assert client.get("/api/audit", params={"since": "昨日"}).status_code == 400
    # 見たことも残る（どんな条件で見たか）
    views = [e for e in client.get("/api/audit", params={"action": "audit.view"}).json()["entries"]]
    assert len(views) >= 8 and all(v["actor"] == ADMIN for v in views)
    assert any("操作=auth.login" in v["detail"] and "2ページ目" in v["detail"] for v in views)
    assert "audit.view" in client.get("/api/audit").json()["actions"]


def test_audit_log_cannot_be_deleted_or_changed(client, settings):
    login(client, EMP)
    login(client, ADMIN)
    for method in ("DELETE", "PUT", "PATCH", "POST"):
        assert client.request(method, "/api/audit", headers=H).status_code in (404, 405)
    db = sqlite3.connect(settings.audit_path)
    with pytest.raises(sqlite3.DatabaseError, match="書き換えられません"):
        db.execute("UPDATE audit_log SET detail = 'x'")
    with pytest.raises(sqlite3.DatabaseError, match="消せません"):
        db.execute("DELETE FROM audit_log")
    db.close()


# ---------------------------------------------------------------- 残さないもの


def test_no_tokens_or_codes_in_the_log(client, settings):
    login(client, EMP)
    session = client.cookies.get(auth.COOKIE)
    visit = client.post("/api/visits", json={"date": "2026-11-16", "time": "15:00", "room": "5階 第2会議室",
                                             "visitor_company": "ABC商事", "visitor_name": "木村 誠"}, headers=H).json()
    login(client, ADMIN)
    admin_session = client.cookies.get(auth.COOKIE)
    device = client.post("/api/admin/kiosk-devices", json={"name": "受付1"}, headers=H).json()
    k = TestClient(client.app)
    kh = {**H, "Authorization": f"Bearer {device['token']}"}
    wrong = ["ZZZZ-ZZZZ", "QQQQ-1234", "WXYZ-9876"]
    for code in wrong:
        assert k.post("/api/kiosk/check-in", json={"code": code}, headers=kh).json()["ok"] is False
    # 止めたあと、正しいコードでも照合しない。止めたことは1回だけ残る
    assert k.post("/api/kiosk/check-in", json={"code": visit["code"]}, headers=kh).json()["ok"] is False
    # 端末トークンでほかの API を呼んでも断って残す
    assert k.get("/api/me", headers=kh).status_code == 403
    client.delete(f"/api/admin/kiosk-devices/{device['device_id']}", headers=H)

    text = auditor(client).all_text()
    for secret in (session, admin_session, device["token"], visit["code"], visit["code"].replace("-", ""), *wrong):
        assert secret and secret not in text, secret
    rows = entries(client)
    locked = [r for r in rows if r["action"] == "kiosk.locked"]
    assert len(locked) == 1 and locked[0]["actor"] == device["device_id"] and locked[0]["actor_name"] == "受付1"
    assert locked[0]["actor_kind"] == "受付端末" and locked[0]["result"] == "拒否" and "3回" in locked[0]["detail"]
    reg = next(r for r in rows if r["action"] == "kiosk.register")
    assert reg["actor"] == ADMIN and reg["target"] == device["device_id"] and "受付1" in reg["detail"]
    assert "kiosk.revoke" in actions(rows) and "api.bearer" in actions(rows)


def test_body_is_kept_only_when_enabled(tmp_path):
    off = audit.Auditor(tmp_path / "a.db")
    off.record("ask", actor="10004", actor_name="高橋 美咲", body="在宅勤務は何日まで？")
    on = audit.Auditor(tmp_path / "b.db", keep_body=True)
    on.record("ask", actor="10004", actor_name="高橋 美咲", body="在宅勤務は何日まで？")
    assert off.query()[0][0]["body"] is None and "在宅勤務" not in off.all_text()
    assert on.query()[0][0]["body"] == "在宅勤務は何日まで？"
    # 端末トークンの形の値は、どこに書いても伏せる（本文を残す設定でも）
    on.record("x", actor="10004", detail="yk_" + "a" * 40, body="yk_" + "b" * 40)
    assert "yk_" not in on.all_text()


# ---------------------------------------------------------------- ジョブ・設定・仕分け・ダウンロード


def test_jobs_settings_paper_and_downloads(client, settings):
    login(client, GA)
    plan = wait_job(client, client.post("/api/paper/plans", json={}, headers=H).json()["job_id"])
    plan_id = plan["result"]["plan_id"]
    rule = {"name": "案内", "keywords": {"any": ["案内"]}, "filename": "{original}.pdf", "folder": "総務/案内"}
    assert client.post("/api/paper/rules", json={"rule": {**rule, "folder": "../x"}}, headers=H).status_code == 400
    assert client.post("/api/paper/rules", json={"rule": rule}, headers=H).status_code == 201
    assert client.post("/api/paper/rules/history/1/restore", json={}, headers=H).status_code == 200
    assert client.post("/api/paper/rules/reset", json={}, headers=H).status_code == 200
    done = client.post(f"/api/paper/plans/{plan_id}/execute", json={"move": False}, headers=H).json()
    conv = wait_job(client, client.post("/api/paper/convert", json={"inbox": "scan_0001.pdf", "to": "docx"}, headers=H).json()["job_id"])
    f = conv["result"]["files"][0]
    assert client.get(f"/api/files/{f['file_id']}").status_code == 200
    meeting_id, _ = make_meeting(client, EMP)
    assert client.get(f"/api/meetings/{meeting_id}/minutes.docx").status_code == 200

    rows = entries(client)
    jobs = [r for r in rows if r["action"].startswith("job.") and r["target"] == plan["job_id"]]
    assert sorted(actions(jobs)) == ["job.done", "job.start"] and {r["actor"] for r in jobs} == {GA}
    assert all(r["detail"].startswith("仕分けの予定：") and r["ip"] == "" for r in jobs)

    rules = [r for r in rows if r["action"].startswith("paper.rules.")]
    assert [(r["action"], r["result"]) for r in reversed(rules)] == [
        ("paper.rules.save", "失敗"), ("paper.rules.save", "成功"), ("paper.rules.restore", "成功"), ("paper.rules.reset", "成功")]
    assert all(r["actor"] == GA and r["actor_name"] == "佐藤 花子" and r["target"] == "paper.rules" for r in rules)
    assert "ルール「案内」を足した（版 2）" in rules[2]["detail"] and "保存しませんでした：ルールに直すところがあります" in rules[3]["detail"]

    ex = next(r for r in rows if r["action"] == "paper.execute")
    assert ex["target"] == plan_id and ex["result"] == "成功"
    assert ex["detail"].startswith(f"{len(done['placed'])}件を写しました")
    folders = {str(Path(p).parent) for p in done["placed"]}
    assert all(d in ex["detail"] for d in folders)
    assert not any(Path(p).name in ex["detail"] for p in done["placed"])     # ファイル名（中身から作る）は残さない

    dl = next(r for r in rows if r["action"] == "file.download")
    assert dl["target"] == f["file_id"] and f["name"] in dl["detail"] and dl["actor"] == GA
    md = next(r for r in rows if r["action"] == "meeting.download")
    assert md["target"] == meeting_id and md["actor"] == EMP and ".docx" in md["detail"]
    # 議事録・PDF の中身は残さない
    text = auditor(client).all_text()
    assert "工事は土曜日" not in text and "132,000" not in text and "132000" not in text


def test_job_failed_cancelled_and_interrupted(tmp_path):
    events = []
    q = JobQueue(tmp_path / "jobs.db", listener=lambda e, j: events.append((e, j["job_id"], j["status"])))

    def boom(ctx, params):
        raise JobFailed("読めませんでした", "管理者向けの理由")

    q.register("boom", boom)
    q.register("ok", lambda ctx, params: {"n": 1})
    failed = q.submit("boom", "10004", {})["job_id"]
    q.run_one(failed)
    cancelled = q.submit("ok", "10004", {})["job_id"]
    q.cancel(cancelled)
    stuck = q.submit("ok", "10004", {})["job_id"]
    assert events == [("start", failed, "実行中"), ("failed", failed, "失敗"), ("cancelled", cancelled, "取り消し")]
    # 再起動で終わらなかったジョブは「中断」として残す
    events.clear()
    JobQueue(tmp_path / "jobs.db", listener=lambda e, j: events.append((e, j["job_id"], j["status"])))
    assert events == [("interrupted", stuck, "失敗")]


# ---------------------------------------------------------------- 保存期間・記録の失敗


def test_old_entries_are_purged_at_startup_and_the_purge_is_recorded(settings):
    a = audit.Auditor(settings.audit_path, keep_days=365)
    old = datetime.now(timezone.utc) - timedelta(days=400)
    a.record("auth.login", actor=EMP, actor_name="高橋 美咲", at=old)
    a.record("auth.login", actor=EMP, actor_name="高橋 美咲", at=datetime.now(timezone.utc) - timedelta(days=300))
    with TestClient(create_app(settings, FakeLLM())) as c:      # 起動したときに消す
        rows = entries(c)
    logins = [r for r in rows if r["action"] == "auth.login" and r["actor"] == EMP]
    assert len(logins) == 1                                      # 300日前のものは残る
    purge = [r for r in rows if r["action"] == "audit.purge"]
    assert len(purge) == 1 and purge[0]["actor"] == "システム" and "1件" in purge[0]["detail"] and "365日" in purge[0]["detail"]
    assert a.purge() == 0 and len([r for r in a.query()[0] if r["action"] == "audit.purge"]) == 1   # 消すものがなければ残さない


def test_recording_failure_does_not_stop_the_operation(client, caplog, monkeypatch):
    def broken():
        raise sqlite3.OperationalError("disk I/O error")

    monkeypatch.setattr(auditor(client), "_connect", broken)
    login(client, EMP)                                           # ログインはできる
    assert client.get("/api/me").status_code == 200
    assert client.get("/api/paper/inbox").status_code == 403     # 断るのも、ふだんどおり
    assert "監査ログに記録できませんでした（auth.login）" in caplog.text


# ---------------------------------------------------------------- 質問の記録（K-009 の続き）

QUESTIONS = {
    "answered": "在宅勤務は週に何日までできますか？",     # 全社の文書で答える
    "no_permission": "採用面接",                         # 人事部だけの文書（採用面接の進め方）にしか当たらない
    "not_found": "ｘｙｚｚｙ",                            # どの文書にも当たらない
    "stream": "在宅勤務の申請は？",
    "helpdesk": "パスワードを忘れました",
}


def ask_everything(client) -> dict:
    login(client, EMP)                                   # 高橋（営業部）
    out = {k: client.post("/api/ask", json={"question": QUESTIONS[k]}, headers=H).json()
           for k in ("answered", "no_permission", "not_found")}
    r = client.post("/api/ask/stream", json={"question": QUESTIONS["stream"]}, headers=H)
    assert r.status_code == 200 and "event: result" in r.text
    sid = client.post("/api/chat/sessions", json={}, headers=H).json()["session_id"]
    reply = client.post(f"/api/chat/sessions/{sid}/messages", json={"text": QUESTIONS["helpdesk"]}, headers=H).json()
    assert "解決しましたか" in reply["reply"]
    return out


def test_questions_are_recorded_without_their_text(client):
    replies = ask_everything(client)
    rows = entries(client, action="ask")
    chat_rows = [r for r in rows if r["action"] == "ask.chat"]
    assert len(chat_rows) == 4 and {r["actor"] for r in rows} == {EMP}
    answered = next(r for r in chat_rows if r["result"] == "成功" and r["ip"] == "testclient")
    assert answered["detail"].startswith("チャット：答えました（出典：A01「") and "A01" in answered["target"]
    denied = next(r for r in chat_rows if r["result"] == "拒否")
    assert denied["detail"] == "チャット：断りました（権限がない）。部署の権限で見せなかった文書 1件" and denied["target"] == ""
    missing = next(r for r in chat_rows if r["result"] == "失敗")
    assert missing["detail"] == "チャット：断りました（文書が見つからない）"
    assert all(r["ip"] == "testclient" for r in chat_rows)          # 逐次表示（別のスレッド）でも接続元が残る
    helpdesk = [r for r in rows if r["action"] == "ask.helpdesk"]
    assert len(helpdesk) == 1 and helpdesk[0]["detail"].startswith("ヘルプデスク：答えました（出典：")
    assert helpdesk[0]["actor_name"] == "高橋 美咲" and helpdesk[0]["ip"] == "testclient"
    assert all(r["body"] is None for r in rows)
    # 既定では、記録の全文に質問の文字列が入らない。見せなかった文書の題名も入らない
    text = auditor(client).all_text()
    for q in QUESTIONS.values():
        assert q not in text, q
    assert "採用面接の進め方" not in text and "H02" not in text
    # 見られない文書があることは、質問した人への返事には出さない
    for r in replies.values():
        assert set(r) == {"answer", "sources", "refused", "elapsed_sec"}
    assert replies["no_permission"] == {**replies["not_found"], "elapsed_sec": replies["no_permission"]["elapsed_sec"]}


def test_question_text_is_kept_only_with_audit_body(settings):
    settings.audit_keep_body = True
    with TestClient(create_app(settings, FakeLLM())) as c:
        ask_everything(c)
        rows = entries(c, action="ask")
    assert {r["body"] for r in rows} == set(QUESTIONS.values())


def test_reset_when_already_default_adds_no_version_and_no_record(client):
    login(client, GA)
    r = client.post("/api/paper/rules/reset", json={}, headers=H)
    assert r.status_code == 200 and r.json()["already_default"] is True and r.json()["message"] == "すでに既定です"
    assert r.json()["version"] is None
    assert client.get("/api/paper/rules/history").json()["versions"] == []
    rule = {"name": "案内", "keywords": {"any": ["案内"]}, "filename": "{original}.pdf", "folder": "総務/案内"}
    assert client.post("/api/paper/rules", json={"rule": rule}, headers=H).status_code == 201
    assert client.post("/api/paper/rules/reset", json={}, headers=H).json()["version"] == 3
    again = client.post("/api/paper/rules/reset", json={}, headers=H).json()
    assert again["already_default"] is True
    assert [v["version"] for v in client.get("/api/paper/rules/history").json()["versions"]] == [3, 2, 1]
    assert [r["action"] for r in entries(client, action="paper.rules")] == ["paper.rules.reset", "paper.rules.save"]


def test_refusal_with_hidden_documents_is_recorded_as_denied(client):
    """経理部が人事部の文書のことを聞く。見られる文書にも弱く当たるが答えられず、見られない文書に強く当たる → 拒否（権限がない）。"""
    login(client, "10001")                               # 山田（経理部）
    q = "採用面接の進め方を教えてください"
    reply = client.post("/api/ask", json={"question": q}, headers=H).json()
    plain = client.post("/api/ask", json={"question": "ｘｙｚｚｙ"}, headers=H).json()
    assert reply["refused"] and reply["answer"] == plain["answer"]      # 社員への返事は、見つからないときと同じ
    rows = [r for r in entries(client, action="ask.chat") if r["actor"] == "10001"]
    denied = next(r for r in rows if r["result"] == "拒否")
    assert denied["detail"].startswith("チャット：断りました（権限がない）。部署の権限で見せなかった文書 ")
    assert next(r for r in rows if r["result"] == "失敗")["detail"] == "チャット：断りました（文書が見つからない）"
    assert q not in auditor(client).all_text() and "採用面接の進め方" not in auditor(client).all_text()


def test_helpdesk_guided_by_procedure_is_a_success(client):
    """社内文書に答えがなく、決まった手順（プリンターなど）で案内したら「手順で案内」＝成功。"""
    login(client, EMP)
    sid = client.post("/api/chat/sessions", json={}, headers=H).json()["session_id"]
    for text in ("プリンターから印刷できません", "本社2階の営業部の複合機です。自分だけ印刷できません。", "一部できない"):
        r = client.post(f"/api/chat/sessions/{sid}/messages", json={"text": text}, headers=H).json()
    assert "解決しましたか" in r["reply"]
    rows = [r for r in entries(client, action="ask.helpdesk")]
    assert len(rows) == 1, rows
    assert rows[0]["result"] == "成功" and rows[0]["detail"].startswith("ヘルプデスク：手順で案内しました（手順：")
    assert "プリンターから印刷できません" not in auditor(client).all_text()
