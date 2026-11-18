"""段階3の API（PDF の仕分け・変換、議事録、ファイルの受け渡し）のテスト。Ollama と文字起こしのモデルは使わない。

確かめること：権限（PDF は総務、議事録は社員）、ほかの人の予定・議事録・ファイルが見えないこと、
アップロードの大きさの上限、結果のファイルの期限、ダウンロードできる人。
"""

import json
import shutil
import sqlite3
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api import DEMO, H, FakeLLM, login  # noqa: E402
from test_api import settings as base_settings  # noqa: E402,F401  （fixture を使う）

from yanua.api import create_app  # noqa: E402
from yanua.api.jobs import QUEUED  # noqa: E402
from yanua.meeting.transcribe import Segment  # noqa: E402

PAPER = DEMO / "paper"
MEETING = DEMO / "meeting"
TEXT_PDFS = ["scan_0001.pdf", "scan_0002.pdf", "scan_0003.pdf", "scan_0005.pdf"]   # 文字のある PDF（OCR しない）

# デモの人事データ：10002 佐藤（総務部・総務）、10003 鈴木（営業部。このテストでは社員番号で総務にする）、
# 10004 高橋（営業部・社員）、10005 田中（情報システム部・情シス・管理者）


@pytest.fixture
def settings(base_settings, tmp_path):
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    for name in TEXT_PDFS:
        shutil.copy2(PAPER / "inbox" / name, inbox / name)
    roles = tmp_path / "roles.json"
    roles.write_text(json.dumps({"roles": {"総務": {"departments": ["総務部"], "employees": ["10003"]},
                                           "管理者": {"employees": ["10005"]}}}, ensure_ascii=False), encoding="utf-8")
    base_settings.roles_path = roles
    base_settings.work_dir = tmp_path / "work"
    base_settings.paper_inbox = inbox
    base_settings.paper_out = tmp_path / "sorted"
    base_settings.paper_rules = PAPER / "rules.json"
    base_settings.meeting_template = MEETING / "templates" / "議事録.docx"
    return base_settings


class FakeTranscriber:
    calls = 0

    def transcribe(self, media, prompt):
        FakeTranscriber.calls += 1
        return [Segment(0, 3, "それでは始めます。"), Segment(3, 6, "工事は土曜日に行うことにしました。")], {}


@pytest.fixture
def client(settings):
    with TestClient(create_app(settings, FakeLLM(), transcriber_factory=FakeTranscriber)) as c:
        yield c


def wait_job(client, job_id: str, timeout: float = 60) -> dict:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in (QUEUED, "実行中"):
            return job
        time.sleep(0.05)
    raise AssertionError("ジョブが終わりません")


def upload(client, kind: str, name: str, body: bytes):
    return client.post(f"/api/uploads?kind={kind}&name={name}", content=body,
                       headers={**H, "Content-Type": "application/octet-stream"})


# ---------------------------------------------------------------- 権限


@pytest.mark.parametrize("method,path,body", [
    ("GET", "/api/paper/inbox", None), ("GET", "/api/paper/rules", None), ("POST", "/api/paper/plans", {}),
    ("GET", "/api/paper/plans/x", None), ("PUT", "/api/paper/plans/x/items/0", {"rule": None}),
    ("POST", "/api/paper/plans/x/execute", {}), ("GET", "/api/paper/history", None),
    ("POST", "/api/paper/convert", {"inbox": "scan_0001.pdf", "to": "docx"}),
])
def test_paper_is_only_for_general_affairs(client, method, path, body):
    for who in ("10004", "10005"):       # 社員・情シス／管理者は使えない
        login(client, who)
        r = client.request(method, path, json=body, headers=H)
        assert r.status_code == 403, (who, path, r.text)


def test_upload_kind_needs_its_permission(client):
    login(client, "10004")
    assert upload(client, "pdf", "a.pdf", b"%PDF-1.4 x").status_code == 403       # PDF は総務だけ
    assert upload(client, "recording", "a.mp4", b"0000").status_code == 201       # 録画は社員が使える
    assert upload(client, "other", "a.txt", b"x").status_code == 400
    assert client.post("/api/uploads?kind=pdf&name=a.pdf", content=b"x").status_code in (401, 403)


def test_meetings_are_for_every_employee(client):
    for who in ("10004", "10002", "10005"):
        login(client, who)
        assert client.get("/api/meetings").status_code == 200


# ---------------------------------------------------------------- アップロード


def test_upload_limits_and_checks(client, settings):
    settings.pdf_max_bytes = 100
    login(client, "10002")
    big = upload(client, "pdf", "big.pdf", b"%PDF-" + b"0" * 200)
    assert big.status_code == 413 and big.json()["error"]["code"] == "too_large"
    assert not any((settings.work_dir / "uploads").glob("*")), "上限を超えたファイルが残っている"
    assert upload(client, "pdf", "fake.pdf", b"hello world").json()["error"]["code"] == "invalid_type"
    assert upload(client, "pdf", "a.exe", b"%PDF-1.4").status_code == 400
    assert upload(client, "pdf", "empty.pdf", b"").status_code == 400
    ok = upload(client, "pdf", "../../請求書.pdf", b"%PDF-1.4 small")
    assert ok.status_code == 201
    body = ok.json()
    assert "/" not in body["name"] and body["name"].endswith("請求書.pdf") and not body["name"].startswith(".")
    assert "path" not in body and body["kind"] == "pdf"
    # 置き場所はサーバーが決める（送られた名前を使わない）
    assert [p.name for p in (settings.work_dir / "uploads").iterdir()] == [body["file_id"] + ".pdf"]


def test_upload_rejected_when_work_dir_is_full(client, settings):
    settings.work_max_bytes = 300
    login(client, "10002")
    first = upload(client, "pdf", "a.pdf", b"%PDF-" + b"0" * 195)        # 200 バイト
    assert first.status_code == 201
    # 合計が上限を超えるものは断り、受け取りかけたファイルも残さない
    full = upload(client, "pdf", "b.pdf", b"%PDF-" + b"0" * 195)
    assert full.status_code == 507 and full.json()["error"]["code"] == "work_full"
    assert [p.name for p in (settings.work_dir / "uploads").iterdir()] == [first.json()["file_id"] + ".pdf"]
    # ほかの人のファイルも合計に入る（社員の録音も、総務の PDF と同じ場所を使う）
    login(client, "10004")
    assert upload(client, "recording", "r.m4a", b"0" * 150).status_code == 507
    assert upload(client, "recording", "r.m4a", b"0" * 80).status_code == 201
    # 期限が来て消えたら、また送れる
    with sqlite3.connect(settings.db_path) as db:
        db.execute("UPDATE work_files SET expires_at = '2000-01-01T00:00:00+00:00'")
    login(client, "10002")
    assert upload(client, "pdf", "b.pdf", b"%PDF-" + b"0" * 195).status_code == 201


def test_bad_segments_json_is_rejected(client):
    login(client, "10004")
    assert upload(client, "segments", "s.json", b"{not json").status_code == 400
    assert upload(client, "segments", "s.json", json.dumps({"a": 1}).encode()).status_code == 400


# ---------------------------------------------------------------- PDF の仕分け


def test_paper_plan_rechoose_execute_and_history(client, settings):
    login(client, "10002")
    inbox = client.get("/api/paper/inbox").json()["files"]
    assert [f["name"] for f in inbox] == TEXT_PDFS
    rules = [r["name"] for r in client.get("/api/paper/rules").json()["rules"]]
    assert "請求書" in rules and "見積書" in rules

    job = client.post("/api/paper/plans", json={}, headers=H)
    assert job.status_code == 202
    done = wait_job(client, job.json()["job_id"])
    assert done["status"] == "完了", done
    plan_id = done["result"]["plan_id"]
    # ジョブの内容に、サーバーの置き場所は出さない
    assert str(settings.paper_inbox) not in json.dumps(done, ensure_ascii=False)

    plan = client.get(f"/api/paper/plans/{plan_id}").json()
    items = {x["file"]: x for x in plan["items"]}
    assert items["scan_0001.pdf"]["dest"] == "経理/請求書/2026/11/20261110_請求書_株式会社サンプル印刷_132000円.pdf"
    assert items["scan_0005.pdf"]["rule"] == "" and items["scan_0005.pdf"]["dest"].startswith("未分類/")
    # 取り出した文字（本文）は画面に返さない（予定には残し、ルールを選び直すときに使う）
    assert "ご請求金額" not in json.dumps(plan, ensure_ascii=False) and all("text" not in x for x in plan["items"])

    # 未分類のものを、ルールを選び直して「見積書」にする（PDF は読み直さない）
    idx = items["scan_0005.pdf"]["index"]
    r = client.put(f"/api/paper/plans/{plan_id}/items/{idx}", json={"rule": "見積書"}, headers=H).json()
    assert r["rule"] == "見積書" and r["by"] == "forced" and r["dest"].startswith("総務/見積書/")
    back = client.put(f"/api/paper/plans/{plan_id}/items/{idx}", json={"rule": None}, headers=H).json()
    assert back["rule"] == "" and back["dest"].startswith("未分類/")
    assert client.put(f"/api/paper/plans/{plan_id}/items/{idx}", json={"rule": "なし"}, headers=H).status_code == 400
    assert client.put(f"/api/paper/plans/{plan_id}/items/99", json={"rule": None}, headers=H).status_code == 404

    # 予定を作ったあとに差し替えられたファイルは置かない
    (settings.paper_inbox / "scan_0003.pdf").write_bytes((PAPER / "inbox" / "scan_0002.pdf").read_bytes())
    result = client.post(f"/api/paper/plans/{plan_id}/execute", json={}, headers=H).json()
    assert len(result["placed"]) == 3 and [s["file"] for s in result["skipped"]] == ["scan_0003.pdf"]
    assert (settings.paper_out / items["scan_0001.pdf"]["dest"]).is_file()
    assert (settings.paper_inbox / "scan_0001.pdf").is_file()          # 既定はコピー（受信箱に残す）
    assert client.post(f"/api/paper/plans/{plan_id}/execute", json={}, headers=H).status_code == 409
    assert client.put(f"/api/paper/plans/{plan_id}/items/0", json={"rule": None}, headers=H).status_code == 409

    history = client.get("/api/paper/history").json()["records"]
    assert len(history) == 3 and {h["source"] for h in history} == {"scan_0001.pdf", "scan_0002.pdf", "scan_0005.pdf"}

    # ほかの総務の人には、この予定は見えない（ないものと同じ）。仕分けの記録は総務の全員が見られる
    login(client, "10003")
    assert client.get("/api/paper/history").json()["records"] == history
    other = client.get(f"/api/paper/plans/{plan_id}")
    assert other.status_code == 404 and other.json() == client.get("/api/paper/plans/nothing").json()
    assert client.post(f"/api/paper/plans/{plan_id}/execute", json={}, headers=H).status_code == 404


def test_paper_plan_only_inbox_files(client):
    login(client, "10002")
    for name in ("../rules.json", "nothing.pdf", ".hidden.pdf"):
        r = client.post("/api/paper/plans", json={"files": [name]}, headers=H)
        assert r.status_code == 400 and r.json()["error"]["code"] == "not_in_inbox"


# ---------------------------------------------------------------- PDF の変換とダウンロード


def test_convert_and_download_only_by_owner(client, settings):
    login(client, "10002")
    up = upload(client, "pdf", "請求書.pdf", (PAPER / "inbox" / "scan_0001.pdf").read_bytes()).json()
    job = client.post("/api/paper/convert", json={"file_id": up["file_id"], "to": "xlsx", "form": True}, headers=H)
    assert job.status_code == 202
    done = wait_job(client, job.json()["job_id"])
    assert done["status"] == "完了", done
    assert "様式" in done["result"]["message"]
    f = done["result"]["files"][0]
    assert f["name"] == "請求書.xlsx" and "path" not in f
    r = client.get(f"/api/files/{f['file_id']}")
    assert r.status_code == 200 and r.content[:2] == b"PK" and "no-store" in r.headers["cache-control"]
    assert "attachment" in r.headers["content-disposition"]

    # ほかの総務の人・社員には開けない（ないファイルと同じ返事）
    missing = client.get("/api/files/nothing").json()
    for who in ("10003", "10004"):
        login(client, who)
        r = client.get(f"/api/files/{f['file_id']}")
        assert r.status_code == 404 and r.json() == missing
        # ほかの人がアップロードした PDF を使って変換することもできない
        if who == "10003":
            r = client.post("/api/paper/convert", json={"file_id": up["file_id"], "to": "docx"}, headers=H)
            assert r.status_code == 404


def test_download_needs_the_role_now(client, settings):
    login(client, "10003")
    up = upload(client, "pdf", "a.pdf", (PAPER / "inbox" / "scan_0001.pdf").read_bytes()).json()
    done = wait_job(client, client.post("/api/paper/convert", json={"file_id": up["file_id"], "to": "docx"},
                                        headers=H).json()["job_id"])
    fid = done["result"]["files"][0]["file_id"]
    assert client.get(f"/api/files/{fid}").status_code == 200
    # 総務から外れたら、自分が作ったファイルでも開けない
    settings.roles_path.write_text(json.dumps({"roles": {"総務": {"departments": ["総務部"]}}}), encoding="utf-8")
    with TestClient(create_app(settings, FakeLLM())) as c:
        login(c, "10003")
        assert c.get(f"/api/files/{fid}").status_code == 404


def test_expired_files_are_removed(client, settings):
    import sqlite3

    login(client, "10002")
    up = upload(client, "pdf", "a.pdf", b"%PDF-1.4 small").json()
    path = settings.work_dir / "uploads" / f"{up['file_id']}.pdf"
    assert path.is_file()
    db = sqlite3.connect(settings.db_path)
    with db:
        db.execute("UPDATE work_files SET expires_at = ?", ((datetime.now(timezone.utc) - timedelta(hours=1)).isoformat(),))
    db.close()
    assert client.get(f"/api/files/{up['file_id']}").status_code == 404     # 期限が過ぎたら開けない
    upload(client, "pdf", "b.pdf", b"%PDF-1.4 small")                        # 次のアップロードのときに消す
    assert not path.exists()


# ---------------------------------------------------------------- 議事録


def make_meeting(client, who: str = "10004", **over) -> tuple[str, dict]:
    login(client, who)
    media = upload(client, "recording", "会議.mp4", (MEETING / "demo_zoom.mp4").read_bytes()).json()
    seg = upload(client, "segments", "seg.json", (MEETING / "demo_zoom_segments.json").read_bytes()).json()
    body = {"upload_id": media["file_id"], "segments_upload_id": seg["file_id"], "title": "本社移転の定例会",
            "date": "2026-11-12", "tool": "none", "participants": ["本社5F会議室"], **over}
    r = client.post("/api/meetings", json=body, headers=H)
    assert r.status_code == 202, r.text
    done = wait_job(client, r.json()["job_id"])
    return r.json()["meeting_id"], done


def test_meeting_flow_fix_unknown_speakers_and_word(client):
    meeting_id, done = make_meeting(client)
    assert done["status"] == "完了", done
    m = client.get(f"/api/meetings/{meeting_id}").json()
    assert m["status"] == "確認待ち" and m["title"] == "本社移転の定例会" and len(m["segments"]) > 3
    assert m["unknown_speakers"] == len(m["segments"])          # tool=none なので、話し手はすべて「不明」
    assert "owner" not in m
    assert m["summary"]["decisions"], "決まった言い方の発言から、決まったことを拾う"
    assert [x["meeting_id"] for x in client.get("/api/meetings").json()["meetings"]] == [meeting_id]

    # 「不明」の話し手をまとめて直す（数と時刻は変えない）
    segs = [{**s, "speaker": "佐藤 花子"} if i % 2 == 0 else s for i, s in enumerate(m["segments"])]
    fixed = client.put(f"/api/meetings/{meeting_id}", json={"segments": segs, "place": "本社5F会議室"}, headers=H).json()
    assert fixed["unknown_speakers"] == len(segs) // 2 and fixed["place"] == "本社5F会議室"
    assert "佐藤 花子" in fixed["attendees"]
    assert client.put(f"/api/meetings/{meeting_id}", json={"segments": segs[:-1]}, headers=H).status_code == 400
    assert client.put(f"/api/meetings/{meeting_id}", json={"date": "2026/11/12"}, headers=H).status_code == 400
    # 要約と宿題を直す
    sm = {**fixed["summary"], "todos": [{"who": "田中 健", "what": "手順をまとめる", "due": "11月20日"}]}
    fixed = client.put(f"/api/meetings/{meeting_id}", json={"summary": sm}, headers=H).json()
    assert fixed["summary"]["todos"] == [{"who": "田中 健", "what": "手順をまとめる", "due": "11月20日"}]

    r = client.get(f"/api/meetings/{meeting_id}/minutes.docx")
    assert r.status_code == 200 and r.content[:2] == b"PK" and "filename*=UTF-8''" in r.headers["content-disposition"]
    from io import BytesIO

    from docx import Document

    text = "\n".join(p.text for p in Document(BytesIO(r.content)).paragraphs)
    tables = [[[c.text for c in row.cells] for row in t.rows] for t in Document(BytesIO(r.content)).tables]
    assert "本社移転の定例会" in text + json.dumps(tables, ensure_ascii=False)
    assert ["田中 健", "手順をまとめる", "11月20日"] in tables[1]


def test_meetings_of_others_are_hidden(client):
    meeting_id, _ = make_meeting(client, "10004")
    missing = None
    for who in ("10002", "10005"):          # 総務・管理者でも、ほかの人の議事録は見えない
        login(client, who)
        missing = missing or client.get("/api/meetings/nothing").json()
        for method, path in [("GET", f"/api/meetings/{meeting_id}"), ("PUT", f"/api/meetings/{meeting_id}"),
                             ("GET", f"/api/meetings/{meeting_id}/minutes.docx")]:
            r = client.request(method, path, json={"title": "x"}, headers=H)
            assert r.status_code == 404 and r.json() == missing, (who, path)
        assert client.get("/api/meetings").json()["meetings"] == []


def test_meeting_needs_own_upload(client):
    login(client, "10004")
    media = upload(client, "recording", "m.mp4", b"0000").json()
    login(client, "10001")
    r = client.post("/api/meetings", json={"upload_id": media["file_id"], "title": "t", "tool": "none"}, headers=H)
    assert r.status_code == 404


def test_meeting_transcribes_without_segments(client):
    login(client, "10004")
    media = upload(client, "recording", "m.mp4", b"0000").json()
    before = FakeTranscriber.calls
    r = client.post("/api/meetings", json={"upload_id": media["file_id"], "title": "工事の打ち合わせ", "tool": "none"},
                    headers=H).json()
    done = wait_job(client, r["job_id"])
    assert done["status"] == "完了" and FakeTranscriber.calls == before + 1
    m = client.get(f"/api/meetings/{r['meeting_id']}").json()
    assert [s["text"] for s in m["segments"]] == ["それでは始めます。", "工事は土曜日に行うことにしました。"]
    assert "工事は土曜日に行う" in "".join(m["summary"]["decisions"])


def test_meeting_without_model_shows_only_a_plain_message(settings, tmp_path, caplog):
    # 文字起こしのモデルがないとき：社員の画面には連絡のお願いだけ。入手のコマンドはサーバーのログに残す
    settings.models_dir = tmp_path / "no-models"
    with TestClient(create_app(settings, FakeLLM())) as c:
        login(c, "10004")
        media = upload(c, "recording", "m.mp4", b"0000").json()
        r = c.post("/api/meetings", json={"upload_id": media["file_id"], "title": "t", "tool": "none"}, headers=H).json()
        job = wait_job(c, r["job_id"])
        m = c.get(f"/api/meetings/{r['meeting_id']}").json()
    shown = "文字起こしのモデルが準備されていません。管理者に連絡してください"
    assert job["status"] == "失敗" and job["error"] == shown
    assert m["status"] == "失敗" and m["error"] == shown
    assert "models pull whisper-small" in caplog.text


def test_meeting_failure_is_recorded(settings):
    class Broken:
        def transcribe(self, media, prompt):
            raise RuntimeError("/srv/secret/whisper の読み込みに失敗")

    with TestClient(create_app(settings, FakeLLM(), transcriber_factory=Broken)) as c:
        login(c, "10004")
        media = upload(c, "recording", "m.mp4", b"0000").json()
        r = c.post("/api/meetings", json={"upload_id": media["file_id"], "title": "t", "tool": "none"}, headers=H).json()
        assert wait_job(c, r["job_id"])["status"] == "失敗"
        m = c.get(f"/api/meetings/{r['meeting_id']}").json()
        # 思わぬ失敗の中身（置き場所など）は画面に出さず、ジョブ番号つきの決まった文にする
        assert m["status"] == "失敗" and "secret" not in m["error"] and r["job_id"] in m["error"]
        assert c.get(f"/api/meetings/{r['meeting_id']}/minutes.docx").status_code == 409


def test_paper_unknown_fields_are_flagged_and_same_names_do_not_overwrite(client, settings):
    # 読み取れる項目のない同じような PDF を2つ「見積書」にすると、置き先の名前に「不明」が入り、同じ名前になる
    for name in TEXT_PDFS:
        (settings.paper_inbox / name).unlink()
    for name in ("a.pdf", "b.pdf"):
        shutil.copy2(PAPER / "inbox" / "scan_0005.pdf", settings.paper_inbox / name)
    login(client, "10002")
    done = wait_job(client, client.post("/api/paper/plans", json={}, headers=H).json()["job_id"])
    plan_id = done["result"]["plan_id"]
    items = [client.put(f"/api/paper/plans/{plan_id}/items/{i}", json={"rule": "見積書"}, headers=H).json() for i in (0, 1)]
    assert items[0]["dest"] == items[1]["dest"] and "不明" in items[0]["dest"]
    assert items[0]["missing"], items[0]            # 「不明」にした項目を返す（画面が注意を出す）
    # すでに同じ名前のファイルがあっても上書きしない
    dest = settings.paper_out / items[0]["dest"]
    dest.parent.mkdir(parents=True)
    dest.write_bytes(b"before")
    result = client.post(f"/api/paper/plans/{plan_id}/execute", json={}, headers=H).json()
    assert dest.read_bytes() == b"before"
    assert len(result["placed"]) == 2 and len(set(result["placed"])) == 2
    assert [r["placed"].rsplit(".", 1)[0][-2:] for r in result["renamed"]] == ["_2", "_3"]
    assert all((settings.paper_out / p).read_bytes()[:5] == b"%PDF-" for p in result["placed"])


class CountingLLM:
    """要約のモデルとして呼ばれた回数を数え、JSON にならない答えを返す（3b で要約が空になった形）。"""

    name = "small-model"

    def __init__(self):
        self.calls = 0

    def chat(self, messages):
        self.calls += 1
        return "すみません、うまくまとめられませんでした。"


def test_summary_uses_its_own_model_and_empty_summary_is_flagged(settings, caplog):
    main, summary_llm = CountingLLM(), CountingLLM()
    with TestClient(create_app(settings, main, summary_llm=summary_llm)) as c:
        meeting_id, done = make_meeting(c)
        m = c.get(f"/api/meetings/{meeting_id}").json()
        assert done["status"] == "完了" and m["status"] == "確認待ち"
        assert summary_llm.calls > 0 and main.calls == 0, "要約は要約のモデルで作る"
        # 要約が空のとき：画面には決まった文、理由はログに
        assert m["summary"]["summary"] == "" and m["summary_note"] == "要約を作れませんでした。録画と照らして書いてください"
        assert "要約が空になりました（モデル small-model）" in caplog.text and "JSON" in caplog.text
        assert m["summary"]["decisions"], "決まった言い方の発言から拾った分は残る"


def test_default_summary_model_is_7b(settings):
    from yanua.llm import OllamaChat

    assert settings.summary_model == "qwen2.5:7b"
    seen = {}

    def fake_process(db, meeting_id, **kw):
        seen["llm"] = kw["llm"]
        return {}

    app = create_app(settings, OllamaChat("qwen2.5:3b-instruct", "http://127.0.0.1:11434"))
    from yanua.api import meetings as mod

    orig, mod.process = mod.process, fake_process
    try:
        with TestClient(app) as c:
            meeting_id, _ = make_meeting(c)
    finally:
        mod.process = orig
    assert seen["llm"].name == "qwen2.5:7b" and seen["llm"].base_url == "http://127.0.0.1:11434"


def test_attendees_are_screen_names_and_can_be_edited(client):
    # 会議ツールの画面に出た名前（会議室のアカウント名も）を、そのまま出席者に使う
    meeting_id, _ = make_meeting(client, participants=["本社5F会議室", "佐藤 花子"])
    m = client.get(f"/api/meetings/{meeting_id}").json()
    assert m["attendees"] == ["本社5F会議室", "佐藤 花子"], m["attendees"]
    segs = [{**s, "speaker": "本社5F会議室"} for s in m["segments"]]
    m = client.put(f"/api/meetings/{meeting_id}", json={"segments": segs}, headers=H).json()
    assert m["segments"][0]["speaker"] == "本社5F会議室" and "本社5F会議室" in m["attendees"]
    # 出席者を画面で直す。Word にも直した出席者が入る
    m = client.put(f"/api/meetings/{meeting_id}", json={"attendees": ["佐藤 花子", " 高橋 美咲 ", ""]}, headers=H).json()
    assert m["attendees"] == ["佐藤 花子", "高橋 美咲"]
    assert client.get(f"/api/meetings/{meeting_id}").json()["attendees"] == ["佐藤 花子", "高橋 美咲"]
    from docx import Document
    import io

    doc = Document(io.BytesIO(client.get(f"/api/meetings/{meeting_id}/minutes.docx").content))
    text = "\n".join(p.text for p in doc.paragraphs) + "\n".join(c.text for t in doc.tables for r in t.rows for c in r.cells)
    assert "佐藤 花子、高橋 美咲" in text, text[:300]
    assert client.put(f"/api/meetings/{meeting_id}", json={"attendees": "佐藤"}, headers=H).status_code == 400


# ---------------------------------------------------------------- 仕分けルールの下書き・保存・前の版（K-047）


@pytest.fixture
def own_rules(settings, tmp_path):
    # ルールのファイルは書き換えるので、作業用の場所に写す（様式のフォルダも一緒に）
    d = tmp_path / "paper"
    shutil.copytree(PAPER / "templates", d / "templates")
    shutil.copy2(PAPER / "rules.json", d / "rules.json")
    settings.paper_rules = d / "rules.json"
    return settings.paper_rules


def test_rule_draft_save_replan_history_and_restore(client, settings, own_rules):
    original = own_rules.read_text(encoding="utf-8")
    login(client, "10002")
    plan_id = wait_job(client, client.post("/api/paper/plans", json={}, headers=H).json()["job_id"])["result"]["plan_id"]
    items = {x["file"]: x for x in client.get(f"/api/paper/plans/{plan_id}").json()["items"]}
    unsorted = items["scan_0005.pdf"]
    assert unsorted["rule"] == ""

    # 未分類の1件から下書きを作る（ジョブ。予定を作ったときの文字を使う）
    job = client.post("/api/paper/rule-drafts", json={"plan_id": plan_id, "index": unsorted["index"]}, headers=H)
    assert job.status_code == 202
    done = wait_job(client, job.json()["job_id"])
    assert done["status"] == "完了", done
    draft = done["result"]["draft"]
    assert draft["name"] == "案内" and "案内" in draft["keywords"]["any"] and done["result"]["source"] == "scan_0005.pdf"

    # 誤りは、ルールの中の位置つきで返す（全部まとめて）。保存しない
    bad = {**draft, "filename": "{date}_{nothing}.pdf", "fields": {"no": "番号[:：]"}, "keywords": {"any": []}}
    r = client.post("/api/paper/rules", json={"rule": bad}, headers=H)
    assert r.status_code == 400 and r.json()["error"]["code"] == "invalid_rule"
    where = {d["where"] for d in r.json()["error"]["details"]}
    assert {"filename", "fields.no", "keywords"} <= where, where
    assert client.post("/api/paper/rules", json={"rule": {**draft, "name": "請求書"}}, headers=H).json()["error"]["details"][0]["where"] == "name"
    assert client.post("/api/paper/rules", json={"rule": {**draft, "templates": {"xlsx": "../../x.xlsx"}}}, headers=H).status_code == 400
    assert client.post("/api/paper/rules/validate", json={"rule": bad}, headers=H).status_code == 400
    assert own_rules.read_text(encoding="utf-8") == original

    # 正しいものだけを保存する。保存すると、予定を作り直したときに新しいルールが使われる
    assert client.post("/api/paper/rules/validate", json={"rule": draft}, headers=H).json() == {"ok": True}
    saved = client.post("/api/paper/rules", json={"rule": draft}, headers=H)
    assert saved.status_code == 201 and "案内" in [x["name"] for x in saved.json()["rules"]]
    again = wait_job(client, client.post("/api/paper/plans", json={"files": ["scan_0005.pdf"]}, headers=H).json()["job_id"])
    new_plan = client.get(f"/api/paper/plans/{again['result']['plan_id']}").json()
    assert new_plan["items"][0]["rule"] == "案内"

    # 前の版が残っていて、元に戻せる（戻したことも1つの版になる）
    hist = client.get("/api/paper/rules/history").json()
    assert [v["version"] for v in hist["versions"]] == [2, 1] and hist["current"] == 2
    assert hist["versions"][0]["saved_by_name"] == "佐藤 花子" and "案内" in hist["versions"][0]["action"]
    assert "案内" not in client.get("/api/paper/rules/history/1").json()["rules"]
    restored = client.post("/api/paper/rules/history/1/restore", json={}, headers=H).json()
    assert restored["version"] == 3 and "案内" not in [x["name"] for x in restored["rules"]]
    assert own_rules.read_text(encoding="utf-8") == original
    assert client.get("/api/paper/rules/history").json()["current"] == 3
    assert client.post("/api/paper/rules/history/99/restore", json={}, headers=H).status_code == 404


def test_rule_file_edited_by_hand_is_kept_as_a_version(client, own_rules):
    login(client, "10002")
    data = json.loads(own_rules.read_text(encoding="utf-8"))
    data["self_names"].append("デモ商事 本社")
    own_rules.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")   # ファイルを直接直した
    rule = {"name": "案内", "keywords": {"any": ["案内"]}, "filename": "{date}_案内.pdf", "folder": "総務/案内"}
    assert client.post("/api/paper/rules", json={"rule": rule}, headers=H).status_code == 201
    hist = client.get("/api/paper/rules/history").json()["versions"]
    assert [v["saved_by"] for v in hist] == ["10002", "（ファイル）"]
    assert "デモ商事 本社" in client.get("/api/paper/rules/history/1").json()["body"]


def test_rule_save_is_for_general_affairs_only(client, own_rules):
    login(client, "10004")
    rule = {"name": "案内", "keywords": {"any": ["案内"]}}
    for method, path, body in (("POST", "/api/paper/rules", {"rule": rule}), ("POST", "/api/paper/rules/validate", {"rule": rule}),
                               ("POST", "/api/paper/rule-drafts", {"inbox": "scan_0001.pdf"}), ("GET", "/api/paper/rules/history", None),
                               ("POST", "/api/paper/rules/history/1/restore", {})):
        r = client.request(method, path, json=body, headers=H)
        assert r.status_code == 403, (path, r.status_code)


def test_rule_draft_from_inbox_and_other_users_plan(client, own_rules):
    login(client, "10002")
    done = wait_job(client, client.post("/api/paper/rule-drafts", json={"inbox": "scan_0002.pdf", "name": "見積の控え"}, headers=H).json()["job_id"])
    assert done["status"] == "完了" and done["result"]["draft"]["name"] == "見積の控え"
    assert client.post("/api/paper/rule-drafts", json={"inbox": "../rules.json"}, headers=H).status_code == 400
    plan_id = wait_job(client, client.post("/api/paper/plans", json={}, headers=H).json()["job_id"])["result"]["plan_id"]
    login(client, "10003")    # ほかの総務の人の予定からは作れない（ないものと同じ）
    assert client.post("/api/paper/rule-drafts", json={"plan_id": plan_id, "index": 0}, headers=H).status_code == 404
