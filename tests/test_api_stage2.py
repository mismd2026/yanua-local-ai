"""段階2の API（お知らせ・来訪予定・受付の記録・問い合わせ票・受付端末）のテスト。Ollama は使わない。

各 API について、権限で拒否されること・ほかの人のものが見えないことを確かめる。
受付端末は、端末トークンの範囲と、違う日・間違い・取り消しのコードに同じ返事をすることを確かめる。
"""

import json
import sys
import time
from datetime import date
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api import DEMO, H, FakeLLM, login  # noqa: E402
from test_api import settings as base_settings  # noqa: E402,F401  （fixture を使う）

from yanua import visits  # noqa: E402
from yanua.api import create_app  # noqa: E402
from yanua.api import desk  # noqa: E402
from yanua.store import Store  # noqa: E402

TODAY = date(2026, 11, 16)
RECEPTION = DEMO / "flows" / "reception"

# デモの人事データ：10001 山田（経理部）、10002 佐藤（総務部・総務）、10004 高橋（営業部）、
# 10005 田中（情報システム部・情シス・管理者）


@pytest.fixture
def settings(base_settings):
    base_settings.today = TODAY
    base_settings.reception_data = RECEPTION
    base_settings.kiosk_min_seconds = 0.0
    return base_settings


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeLLM())) as c:
        yield c


def db_of(settings):
    return Store(settings.db_path)


def new_visit(client, who: str, day: str = "2026-11-16", **over) -> dict:
    login(client, who)
    body = {"date": day, "time": "15:00", "room": "5階 第2会議室", "visitor_company": "ABC商事", "visitor_name": "木村 誠",
            **over}
    r = client.post("/api/visits", json=body, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def register_device(client, name: str = "受付1") -> dict:
    login(client, "10005")
    r = client.post("/api/kiosk/devices", json={"name": name}, headers=H)
    assert r.status_code == 201, r.text
    return r.json()


def kiosk_headers(device: dict) -> dict:
    return {**H, "Authorization": f"Bearer {device['token']}"}


# ---------------------------------------------------------------- 権限で拒否される


@pytest.mark.parametrize("method,path,who,body", [
    ("GET", "/api/tickets", "10004", None),
    ("GET", "/api/tickets", "10002", None),
    ("GET", "/api/tickets/HD-1", "10004", None),
    ("PATCH", "/api/tickets/HD-1", "10002", {"status": "対応済み"}),
    ("GET", "/api/reception/records", "10004", None),
    ("GET", "/api/reception/records", "10005", None),
    ("POST", "/api/reception/records/RC-1/done", "10004", None),
    ("GET", "/api/visits?date=2026-11-16", "10004", None),
    ("GET", "/api/visits?host=10002", "10004", None),
    ("POST", "/api/visits/import", "10004", None),
    ("GET", "/api/kiosk/devices", "10002", None),
    ("POST", "/api/kiosk/devices", "10004", {"name": "x"}),
    ("DELETE", "/api/kiosk/devices/KD-1", "10002", None),
])
def test_forbidden_by_role(client, method, path, who, body):
    login(client, who)
    r = client.request(method, path, json=body, headers=H)
    assert r.status_code == 403, (path, who, r.text)
    assert r.json() == {"error": {"code": "forbidden", "message": "この操作をする権限がありません"}}


@pytest.mark.parametrize("method,path", [
    ("GET", "/api/notifications"), ("POST", "/api/notifications/read"), ("GET", "/api/visits"),
    ("POST", "/api/visits"), ("GET", "/api/tickets"), ("GET", "/api/reception/records"), ("GET", "/api/kiosk/devices"),
])
def test_needs_login(client, method, path):
    assert client.request(method, path, json={}, headers=H).status_code == 401


def test_me_lists_stage2_permissions(client):
    login(client, "10002")
    p = client.get("/api/me").json()["permissions"]
    assert {"notifications", "visits.own", "visits.all", "reception.records"} <= set(p) and "tickets" not in p
    login(client, "10005")
    p = client.get("/api/me").json()["permissions"]
    assert {"tickets", "kiosk.devices"} <= set(p) and "visits.all" not in p


# ---------------------------------------------------------------- お知らせ


def test_notifications_only_mine_and_my_department(client, settings):
    s = db_of(settings)
    visits.notify(s.db, "10004", "営業部", "来訪", "高橋さんあて")
    visits.notify(s.db, "", "営業部", "取次", "営業部あて")
    visits.notify(s.db, "10001", "経理部", "来訪", "山田さんあて")
    visits.notify(s.db, "", "経理部", "取次", "経理部あて")
    others = [n["notification_id"] for n in visits.notifications(s.db, "10001", "経理部")]
    s.close()

    login(client, "10004")
    r = client.get("/api/notifications?unread=1").json()
    assert r["unread"] == 2
    assert sorted(n["message"] for n in r["notifications"]) == ["営業部あて", "高橋さんあて"]
    assert {n["to"] for n in r["notifications"]} == {"あなた", "営業部"}

    # ほかの人のお知らせの番号を送っても、既読にならない（エラーにもしない）
    r = client.post("/api/notifications/read", json={"ids": others}, headers=H).json()
    assert r == {"read": 0, "unread": 2}
    login(client, "10001")
    assert client.get("/api/notifications?unread=1").json()["unread"] == 2

    login(client, "10004")
    mine = [n["notification_id"] for n in client.get("/api/notifications").json()["notifications"]]
    assert client.post("/api/notifications/read", json={"ids": mine[:1]}, headers=H).json() == {"read": 1, "unread": 1}
    assert client.post("/api/notifications/read", json={"all": True}, headers=H).json() == {"read": 1, "unread": 0}
    all_items = client.get("/api/notifications").json()
    assert all_items["unread"] == 0 and len(all_items["notifications"]) == 2 and all(n["read"] for n in all_items["notifications"])


# ---------------------------------------------------------------- 来訪予定


def test_visit_code_is_shown_once_and_others_cannot_see_it(client):
    created = new_visit(client, "10004", purpose="新商品の打ち合わせ")
    assert visits.looks_like_code(created["code"]) and created["code"] in created["invitation"]
    vid = created["visit"]["visit_id"]
    mine = client.get("/api/visits").json()["visits"]
    assert [v["visit_id"] for v in mine] == [vid]
    # コードは一覧には出ない（ハッシュだけを保存している）
    assert created["code"] not in json.dumps(mine, ensure_ascii=False)

    login(client, "10001")
    assert client.get("/api/visits").json()["visits"] == []
    hidden = client.delete(f"/api/visits/{vid}", headers=H)
    missing = client.delete("/api/visits/999999", headers=H)
    assert hidden.status_code == missing.status_code == 404 and hidden.json() == missing.json()


def test_employee_cannot_register_visit_for_someone_else(client):
    login(client, "10004")
    body = {"date": "2026-11-16", "time": "10:00", "room": "A", "visitor_company": "X", "visitor_name": "Y",
            "host_employee_no": "10001"}
    assert client.post("/api/visits", json=body, headers=H).status_code == 403
    login(client, "10002")   # 総務はほかの人の予定を登録できる
    r = client.post("/api/visits", json=body, headers=H)
    assert r.status_code == 201 and r.json()["visit"]["host_employee_no"] == "10001"


def test_visit_validation_error(client):
    login(client, "10004")
    r = client.post("/api/visits", json={"date": "2026/11/16", "time": "10:00", "room": "A", "visitor_company": "X",
                                         "visitor_name": "Y"}, headers=H)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_visit"


def test_general_affairs_sees_and_cancels_all_visits_of_a_day(client):
    a = new_visit(client, "10004")["visit"]["visit_id"]
    new_visit(client, "10001", time="11:00")
    new_visit(client, "10001", day="2026-11-17")
    login(client, "10002")
    day = client.get("/api/visits?date=2026-11-16").json()["visits"]
    assert len(day) == 2 and {v["host_name"] for v in day} == {"高橋 美咲", "山田 太郎"}
    assert {v["host_dept"] for v in day} == {"営業部", "経理部"}
    assert client.delete(f"/api/visits/{a}", headers=H).status_code == 200
    day = client.get("/api/visits?date=2026-11-16").json()["visits"]
    assert [v["cancelled"] for v in day if v["visit_id"] == a] == [True]
    login(client, "10004")
    assert client.get("/api/visits").json()["visits"][0]["cancelled"] is True


def test_visit_import_csv_and_calendar(client):
    login(client, "10002")
    r = client.post("/api/visits/import", content=(RECEPTION / "visits.csv").read_bytes(),
                    headers={**H, "Content-Type": "text/csv"})
    assert r.status_code == 200, r.text
    assert r.json()["kind"] == "visits" and len(r.json()["added"]) >= 2
    # 同じものをもう一度取り込んでも重ならない
    again = client.post("/api/visits/import", content=(RECEPTION / "visits.csv").read_bytes(), headers=H).json()
    assert again["added"] == []

    cal = client.post("/api/visits/import", content=(RECEPTION / "calendar_export_sample.csv").read_bytes(), headers=H).json()
    assert cal["kind"] == "calendar" and cal["added"] + cal["updated"] >= 1 and cal["cancelled"] >= 1

    bad = client.post("/api/visits/import", content="foo,bar\n1,2\n".encode(), headers=H)
    assert bad.status_code == 400 and bad.json()["error"]["code"] == "invalid_csv"
    empty = client.post("/api/visits/import", content=b"", headers=H)
    assert empty.status_code == 400


# ---------------------------------------------------------------- 問い合わせ票


def add_ticket(settings, tid="HD-20261116-0001", priority="高"):
    s = db_of(settings)
    desk.ensure_schema(s.db)
    payload = {"ticket_id": tid, "problem": "複合機が止まっている", "priority": priority, "category": "印刷",
               "requester": {"employee_no": "10004", "name": "高橋 美咲", "dept": "営業部"}, "contact": "内線4100"}
    with s.db:
        s.db.execute("INSERT INTO helpdesk_tickets VALUES (?, ?, ?, ?, ?)",
                     (tid, "2026-11-16T10:00:00+09:00", "印刷", priority, json.dumps(payload, ensure_ascii=False)))
    s.close()


def test_tickets_list_filter_and_update(client, settings):
    add_ticket(settings)
    add_ticket(settings, "HD-20261116-0002", priority="低")
    login(client, "10005")
    items = client.get("/api/tickets").json()["tickets"]
    assert [t["ticket_id"] for t in items] == ["HD-20261116-0002", "HD-20261116-0001"]
    assert all(t["status"] == "未対応" for t in items)
    assert [t["ticket_id"] for t in client.get("/api/tickets?priority=高").json()["tickets"]] == ["HD-20261116-0001"]
    t = client.get("/api/tickets/HD-20261116-0001").json()
    assert t["detail"]["contact"] == "内線4100"

    r = client.patch("/api/tickets/HD-20261116-0001", json={"status": "対応中", "note": "現地確認"}, headers=H).json()
    assert r["status"] == "対応中" and r["note"] == "現地確認" and r["status_updated_by"] == "10005"
    r = client.patch("/api/tickets/HD-20261116-0001", json={"status": "対応済み"}, headers=H).json()
    assert r["status"] == "対応済み" and r["note"] == "現地確認"
    assert [t["ticket_id"] for t in client.get("/api/tickets?status=未対応").json()["tickets"]] == ["HD-20261116-0002"]

    assert client.patch("/api/tickets/HD-20261116-0001", json={"status": "なし"}, headers=H).status_code == 400
    assert client.patch("/api/tickets/HD-0", json={"status": "対応中"}, headers=H).status_code == 404
    assert client.get("/api/tickets?status=x").status_code == 400


# ---------------------------------------------------------------- 受付の記録


def test_reception_records_and_done(client, settings):
    s = db_of(settings)
    rid = desk.add_record(s.db, channel="電話", category="取次", purpose="山田さんにお約束", visitor={"company": "A社", "name": "B"},
                          host=None, route="総務部", priority="中")
    s.close()
    login(client, "10002")
    items = client.get("/api/reception/records").json()["records"]
    assert [r["record_id"] for r in items] == [rid] and items[0]["status"] == "未対応"
    done = client.post(f"/api/reception/records/{rid}/done", headers=H).json()
    assert done["status"] == "対応済み" and done["status_updated_by"] == "10002"
    assert client.get("/api/reception/records?status=未対応").json()["records"] == []
    assert client.post("/api/reception/records/RC-none/done", headers=H).status_code == 404


# ---------------------------------------------------------------- 受付端末：トークンの範囲


def test_device_token_only_for_kiosk_api(client):
    device = register_device(client)
    assert device["token"].startswith("yk_") and device["device_id"].startswith("KD-")
    listed = client.get("/api/kiosk/devices").json()["devices"]
    assert [d["device_id"] for d in listed] == [device["device_id"]] and "token" not in listed[0]
    assert "token_hash" not in listed[0]

    k = TestClient(client.app)   # 受付端末（クッキーなし）
    kh = kiosk_headers(device)
    for method, path in [("GET", "/api/me"), ("GET", "/api/tickets"), ("POST", "/api/ask"), ("GET", "/api/kiosk/devices"),
                         ("POST", "/api/kiosk/devices"), ("GET", "/api/notifications"), ("GET", "/api/jobs")]:
        r = k.request(method, path, json={"question": "x", "name": "x"}, headers=kh)
        assert r.status_code == 403, (method, path, r.text)
    assert k.post("/api/kiosk/sessions", headers=kh).status_code == 201


def test_login_cookie_cannot_use_kiosk_api(client):
    register_device(client)              # 管理者でログインしたまま（クッキーあり）
    for method, path, body in [("POST", "/api/kiosk/sessions", None), ("POST", "/api/kiosk/check-in", {"code": "ABCD-EFGH"}),
                               ("GET", "/api/kiosk/passes/x.html", None), ("POST", "/api/kiosk/sessions/x/messages", {"text": "a"})]:
        r = client.request(method, path, json=body, headers=H)
        assert r.status_code == 401 and r.json()["error"]["code"] == "kiosk_not_registered", path


def test_revoked_or_unknown_token_is_rejected(client):
    device = register_device(client)
    k = TestClient(client.app)
    assert k.post("/api/kiosk/sessions", headers={**H, "Authorization": "Bearer yk_wrong"}).status_code == 401
    assert client.delete(f"/api/kiosk/devices/{device['device_id']}", headers=H).status_code == 200
    assert k.post("/api/kiosk/sessions", headers=kiosk_headers(device)).status_code == 401
    assert client.delete(f"/api/kiosk/devices/{device['device_id']}", headers=H).status_code == 404


def test_kiosk_still_needs_client_header(client):
    device = register_device(client)
    k = TestClient(client.app)
    r = k.post("/api/kiosk/sessions", headers={"Authorization": f"Bearer {device['token']}"})
    assert r.status_code == 403 and r.json()["error"]["code"] == "csrf"


# ---------------------------------------------------------------- 受付端末：コードの照合


def kiosk_codes(client):
    """当日の正しいコード、違う日のコード、取り消したコード、間違ったコードを用意する。"""
    good = new_visit(client, "10004")["code"]
    other_day = new_visit(client, "10004", day="2026-11-17")["code"]
    cancelled = new_visit(client, "10004", time="16:00")
    client.delete(f"/api/visits/{cancelled['visit']['visit_id']}", headers=H)
    return good, {"other_day": other_day, "cancelled": cancelled["code"], "wrong": "ZZZZ-ZZZZ", "not_a_code": "こんにちは"}


def test_check_in_same_reply_for_bad_codes_and_pass(client, settings):
    good, bad = kiosk_codes(client)
    device = register_device(client)
    k = TestClient(client.app)
    kh = kiosk_headers(device)

    replies = {name: k.post("/api/kiosk/check-in", json={"code": code}, headers=kh) for name, code in bad.items()}
    assert {r.status_code for r in replies.values()} == {200}
    bodies = {json.dumps(r.json(), ensure_ascii=False, sort_keys=True) for r in replies.values()}
    assert len(bodies) == 1, bodies
    assert next(iter(replies.values())).json()["ok"] is False

    ok = k.post("/api/kiosk/check-in", json={"code": good.lower().replace("-", "")}, headers=kh).json()
    assert ok["ok"] is True and set(ok) == set(replies["wrong"].json())
    assert ok["visit"]["host_name"] == "高橋 美咲" and ok["visit"]["room"] == "5階 第2会議室"
    # 受付端末には、社員番号・部署・予定の番号を出さない
    text = json.dumps(ok, ensure_ascii=False)
    assert "10004" not in text and "営業部" not in text and "visit_id" not in text

    # 担当者へのお知らせと、受付の記録が残る
    login(client, "10004")
    assert any("到着" in n["message"] for n in client.get("/api/notifications?unread=1").json()["notifications"])
    login(client, "10002")
    assert [r["category"] for r in client.get("/api/reception/records").json()["records"]] == ["来訪"]

    # 入館票は発行した端末だけが開ける。ほかの端末・ない番号は同じ返事
    p = k.get(f"/api/kiosk/passes/{ok['pass_id']}.html", headers=kh)
    assert p.status_code == 200 and "入館票" in p.text and good not in p.text
    assert "default-src 'none'" in p.headers["content-security-policy"]
    other = register_device(client, "受付2")
    o1 = k.get(f"/api/kiosk/passes/{ok['pass_id']}.html", headers=kiosk_headers(other))
    o2 = k.get("/api/kiosk/passes/nothing.html", headers=kh)
    assert o1.status_code == o2.status_code == 404 and o1.json() == o2.json()


def test_kiosk_replies_take_the_same_time(settings):
    settings.kiosk_min_seconds = 0.3
    with TestClient(create_app(settings, FakeLLM())) as c:
        good, bad = kiosk_codes(c)
        device = register_device(c)
        k = TestClient(c.app)
        for code in [good, *bad.values()]:
            started = time.monotonic()
            k.post("/api/kiosk/check-in", json={"code": code}, headers=kiosk_headers(device))
            assert time.monotonic() - started >= 0.3


def test_too_many_failures_stop_checking(settings):
    settings.kiosk_max_failures = 3
    with TestClient(create_app(settings, FakeLLM())) as c:
        good, _ = kiosk_codes(c)
        device = register_device(c)
        k = TestClient(c.app)
        kh = kiosk_headers(device)
        for _ in range(3):
            assert k.post("/api/kiosk/check-in", json={"code": "ZZZZ-ZZZZ"}, headers=kh).json()["ok"] is False
        # 間違いが続いた端末では、正しいコードでも照合しない（返事は失敗と同じ形）
        assert k.post("/api/kiosk/check-in", json={"code": good}, headers=kh).json()["ok"] is False
        other = register_device(c, "受付2")
        assert k.post("/api/kiosk/check-in", json={"code": good}, headers=kiosk_headers(other)).json()["ok"] is True


# ---------------------------------------------------------------- 受付端末：会話


def test_kiosk_session_same_reply_for_bad_codes(client):
    good, bad = kiosk_codes(client)
    device = register_device(client)
    k = TestClient(client.app)
    kh = kiosk_headers(device)

    replies = []
    for code in (bad["other_day"], bad["cancelled"], bad["wrong"]):
        sid = k.post("/api/kiosk/sessions", headers=kh).json()["session_id"]
        r = k.post(f"/api/kiosk/sessions/{sid}/messages", json={"text": code}, headers=kh).json()
        replies.append({key: v for key, v in r.items() if key != "session_id"})
    assert replies[0] == replies[1] == replies[2] and replies[0]["visit"] is None and replies[0]["pass_id"] is None

    first = k.post("/api/kiosk/sessions", headers=kh).json()
    assert set(first) == {"session_id", "finished", "reply", "visit", "pass_id"} and "コード" in first["reply"]
    end = k.post(f"/api/kiosk/sessions/{first['session_id']}/messages", json={"text": good}, headers=kh).json()
    assert end["finished"] is True and end["visit"]["visitor_name"] == "木村 誠" and end["pass_id"]
    assert "10004" not in json.dumps(end, ensure_ascii=False)
    assert k.get(f"/api/kiosk/passes/{end['pass_id']}.html", headers=kh).status_code == 200


def test_kiosk_session_of_other_device_is_hidden(client):
    a = register_device(client, "受付1")
    b = register_device(client, "受付2")
    k = TestClient(client.app)
    sid = k.post("/api/kiosk/sessions", headers=kiosk_headers(a)).json()["session_id"]
    other = k.post(f"/api/kiosk/sessions/{sid}/messages", json={"text": "こんにちは"}, headers=kiosk_headers(b))
    missing = k.post("/api/kiosk/sessions/nothing/messages", json={"text": "こんにちは"}, headers=kiosk_headers(b))
    assert other.status_code == missing.status_code == 404 and other.json() == missing.json()


def test_helpdesk_cannot_continue_a_kiosk_session(client):
    device = register_device(client)
    k = TestClient(client.app)
    sid = k.post("/api/kiosk/sessions", headers=kiosk_headers(device)).json()["session_id"]
    login(client, "10005")
    assert client.post(f"/api/chat/sessions/{sid}/messages", json={"text": "はい"}, headers=H).status_code == 404
