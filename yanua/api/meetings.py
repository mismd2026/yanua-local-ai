"""議事録（S-13）を、画面から使うための処理。社員（総務を含む）が使い、作った人だけが見られる。

流れ：録画のアップロード → ジョブ（文字起こし → 話し手 → 要約） → 確認と修正 → Word で出力
- 文字起こしは社内の faster-whisper（models pull で入手し、ハッシュで照合したもの）。
  文字起こし済みの発言（JSON）をアップロードしたときは、文字起こしをしない（デモや、別の端末で起こしたもの）
- 話し手は録画の画面の名前から読む（yanua/meeting/speakers.py）。読めない発言は「不明」。画面でまとめて直せる
- 議事録の Word は、出力するたびに今の内容から作る（ファイルとして残さない）
"""

from __future__ import annotations

import json
import sqlite3
import tempfile
from datetime import date, datetime
from pathlib import Path

from ..meeting import minutes
from ..meeting.run import company_words, load_segments
from ..meeting.speakers import PRESETS, label_segments
from ..meeting.summarize import Summary, summarize
from ..meeting.transcribe import Segment, build_prompt

TOOLS = ("zoom", "teams", "meet", "none")
PROCESSING, REVIEW, FAILED = "処理中", "確認待ち", "失敗"

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id   TEXT PRIMARY KEY,
    owner        TEXT NOT NULL,
    job_id       TEXT,
    title        TEXT NOT NULL,
    date         TEXT NOT NULL,
    place        TEXT NOT NULL DEFAULT '',
    tool         TEXT NOT NULL,
    participants TEXT NOT NULL,         -- JSON
    status       TEXT NOT NULL,
    error        TEXT,
    segments     TEXT,                  -- JSON（start・end・text・speaker）
    summary      TEXT,                  -- JSON（summary・points・decisions・todos・next_meeting）
    media_name   TEXT NOT NULL DEFAULT '',
    created_at   TEXT NOT NULL,
    updated_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_meetings_owner ON meetings(owner, created_at);
"""

KEYS = ("meeting_id", "owner", "job_id", "title", "date", "place", "tool", "participants", "status", "error",
        "segments", "summary", "media_name", "created_at", "updated_at")


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def create(db: sqlite3.Connection, *, meeting_id: str, owner: str, title: str, day: str, place: str, tool: str,
           participants: list[str], media_name: str) -> None:
    ensure_schema(db)
    now = _now()
    with db:
        db.execute("INSERT INTO meetings (meeting_id, owner, title, date, place, tool, participants, status, media_name, "
                   "created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (meeting_id, owner, title, day, place, tool, json.dumps(participants, ensure_ascii=False), PROCESSING,
                    media_name, now, now))


def set_job(db: sqlite3.Connection, meeting_id: str, job_id: str) -> None:
    with db:
        db.execute("UPDATE meetings SET job_id = ? WHERE meeting_id = ?", (job_id, meeting_id))


def _row(r) -> dict:
    m = dict(zip(KEYS, r))
    m["participants"] = json.loads(m["participants"])
    m["segments"] = json.loads(m["segments"]) if m["segments"] else []
    m["summary"] = json.loads(m["summary"]) if m["summary"] else None
    return m


def get(db: sqlite3.Connection, meeting_id: str, owner: str) -> dict | None:
    """作った人の議事録だけを返す。ほかの人のものは None（ないものと区別しない）。"""
    ensure_schema(db)
    r = db.execute(f"SELECT {', '.join(KEYS)} FROM meetings WHERE meeting_id = ?", (meeting_id,)).fetchone()
    if not r or r[1] != owner:
        return None
    m = _row(r)
    m.pop("owner")
    m["unknown_speakers"] = sum(1 for s in m["segments"] if (s.get("speaker") or "不明") == "不明")
    m["attendees"] = minutes.attendees([Segment(**s) for s in m["segments"]], m["participants"])
    return m


def list_for(db: sqlite3.Connection, owner: str) -> list[dict]:
    ensure_schema(db)
    rows = db.execute("SELECT meeting_id, job_id, title, date, status, created_at, updated_at FROM meetings "
                      "WHERE owner = ? ORDER BY created_at DESC", (owner,))
    keys = ("meeting_id", "job_id", "title", "date", "status", "created_at", "updated_at")
    return [dict(zip(keys, r)) for r in rows]


def process(db: sqlite3.Connection, meeting_id: str, *, media: Path, segments_file: Path | None, transcriber_factory,
            llm, use_summary: bool, progress=lambda pct, stage: None) -> dict:
    """ジョブの本体：文字起こし → 話し手 → 要約。結果は meetings の行に残す。"""
    r = db.execute(f"SELECT {', '.join(KEYS)} FROM meetings WHERE meeting_id = ?", (meeting_id,)).fetchone()
    m = _row(r)
    try:
        names, depts = company_words(db)
        people = [*m["participants"], *names]
        if segments_file is not None:
            progress(10, "文字起こし済みの発言を読んでいます")
            segs = load_segments(segments_file)
        else:
            progress(10, "文字起こしをしています（長い会議は時間がかかります）")
            segs, _ = transcriber_factory().transcribe(media, build_prompt([], people, depts))
        found = 0
        if m["tool"] != "none" and m["tool"] in PRESETS:
            progress(50, "録画の画面から話し手を読み取っています")
            found = label_segments(media, segs, m["tool"], people)
        for s in segs:
            s.speaker = s.speaker or "不明"
        summary = Summary()
        if use_summary and llm is not None:
            progress(75, "要約しています")
            summary = summarize(llm, segs, m["title"], people)
        with db:
            db.execute("UPDATE meetings SET segments = ?, summary = ?, status = ?, updated_at = ? WHERE meeting_id = ?",
                       (json.dumps([s.as_dict() for s in segs], ensure_ascii=False),
                        json.dumps(summary.__dict__, ensure_ascii=False), REVIEW, _now(), meeting_id))
        progress(100, "完了")
        return {"meeting_id": meeting_id, "segments": len(segs), "speakers_found": found,
                "unknown_speakers": sum(1 for s in segs if s.speaker == "不明")}
    except Exception as e:
        with db:
            db.execute("UPDATE meetings SET status = ?, error = ?, updated_at = ? WHERE meeting_id = ?",
                       (FAILED, str(e)[:500], _now(), meeting_id))
        raise


class MeetingError(ValueError):
    pass


def _strs(v, what: str, limit: int = 2000) -> list[str]:
    if not isinstance(v, list) or not all(isinstance(x, str) for x in v):
        raise MeetingError(f"{what}は文字の一覧にしてください")
    return [x.strip()[:limit] for x in v if x.strip()]


def update(db: sqlite3.Connection, meeting_id: str, changes: dict) -> None:
    """確認と修正。発言は数と時刻を変えず、話し手と文だけを直せる。"""
    r = db.execute(f"SELECT {', '.join(KEYS)} FROM meetings WHERE meeting_id = ?", (meeting_id,)).fetchone()
    m = _row(r)
    if m["status"] == PROCESSING:
        raise MeetingError("まだ処理中です。終わってから直してください")
    sets: dict = {}
    for key in ("title", "place"):
        if changes.get(key) is not None:
            sets[key] = str(changes[key]).strip()[:200]
    if not sets.get("title", m["title"]):
        raise MeetingError("会議名が空です")
    if changes.get("date") is not None:
        try:
            sets["date"] = date.fromisoformat(changes["date"]).isoformat()
        except ValueError as e:
            raise MeetingError("開催日は YYYY-MM-DD で指定してください") from e
    if changes.get("segments") is not None:
        new = changes["segments"]
        if not isinstance(new, list) or len(new) != len(m["segments"]):
            raise MeetingError("発言の数が変わっています。画面を読み込み直してください")
        segs = []
        for old, s in zip(m["segments"], new):
            if not isinstance(s, dict) or not isinstance(s.get("speaker", ""), str) or not isinstance(s.get("text", ""), str):
                raise MeetingError("発言の形が正しくありません")
            segs.append({**old, "speaker": (s.get("speaker") or "不明").strip()[:60] or "不明",
                         "text": s.get("text", old["text"]).strip()[:2000]})
        sets["segments"] = json.dumps(segs, ensure_ascii=False)
    if changes.get("summary") is not None:
        sm = changes["summary"]
        if not isinstance(sm, dict):
            raise MeetingError("要約の形が正しくありません")
        todos = sm.get("todos", [])
        if not isinstance(todos, list) or not all(isinstance(t, dict) for t in todos):
            raise MeetingError("宿題の形が正しくありません")
        sets["summary"] = json.dumps({
            "summary": str(sm.get("summary", "")).strip()[:4000],
            "points": _strs(sm.get("points", []), "要点"),
            "decisions": _strs(sm.get("decisions", []), "決まったこと"),
            "todos": [{"who": str(t.get("who", "")).strip()[:60] or "（未定）", "what": str(t.get("what", "")).strip()[:500],
                       "due": str(t.get("due", "")).strip()[:60]} for t in todos if str(t.get("what", "")).strip()],
            "next_meeting": str(sm.get("next_meeting", "")).strip()[:200],
        }, ensure_ascii=False)
    if not sets:
        return
    sets["updated_at"] = _now()
    with db:
        db.execute(f"UPDATE meetings SET {', '.join(f'{k} = ?' for k in sets)} WHERE meeting_id = ?",
                   (*sets.values(), meeting_id))


def minutes_docx(m: dict, template: Path) -> tuple[bytes, str]:
    """今の内容から議事録の Word を作る（ファイルは残さない）。"""
    segs = [Segment(**s) for s in m["segments"]]
    summary = Summary(**(m["summary"] or {}))
    d = date.fromisoformat(m["date"])
    v = minutes.values(m["title"], f"{d.year}年{d.month}月{d.day}日", m["place"], minutes.attendees(segs, m["participants"]),
                       summary, Path(m["media_name"] or "録画"), segs)
    name = f"{d:%Y%m%d}_議事録_{m['title']}.docx"
    with tempfile.TemporaryDirectory() as tmp:
        out = minutes.to_docx(template, Path(tmp) / "minutes.docx", v, summary)
        return out.read_bytes(), name
