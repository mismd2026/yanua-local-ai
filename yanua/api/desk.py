"""問い合わせ票（情シス・S-30）と受付の記録（総務・S-23）の、対応の状態。

票と記録そのものは、会話の流れが作ったまま（helpdesk_tickets・reception_records）。
対応の状態（未対応・対応中・対応済み）と、だれがいつ変えたかは、別の表に持つ（元の表を作り替えない）。
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime

from ..flows.external.reception.graph import TABLE_SCHEMA as RECORD_SCHEMA
from ..flows.internal.helpdesk.graph import TICKET_SCHEMA

TICKET_STATUSES = ("未対応", "対応中", "対応済み")
RECORD_STATUSES = ("未対応", "対応済み")
PRIORITIES = ("高", "中", "低")

SCHEMA = """
CREATE TABLE IF NOT EXISTS desk_status (
    kind        TEXT NOT NULL,          -- ticket / record
    item_id     TEXT NOT NULL,
    status      TEXT NOT NULL,
    note        TEXT NOT NULL DEFAULT '',
    updated_at  TEXT NOT NULL,
    updated_by  TEXT NOT NULL,
    PRIMARY KEY (kind, item_id)
);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(TICKET_SCHEMA + RECORD_SCHEMA + SCHEMA)


def _status(db, kind: str, item_id: str) -> dict:
    row = db.execute("SELECT status, note, updated_at, updated_by FROM desk_status WHERE kind = ? AND item_id = ?",
                     (kind, item_id)).fetchone()
    if not row:
        return {"status": "未対応", "note": "", "status_updated_at": None, "status_updated_by": None}
    return {"status": row[0], "note": row[1], "status_updated_at": row[2], "status_updated_by": row[3]}


def set_status(db, kind: str, item_id: str, status: str, note: str | None, by: str) -> None:
    old = _status(db, kind, item_id)
    with db:
        db.execute("INSERT OR REPLACE INTO desk_status VALUES (?, ?, ?, ?, ?, ?)",
                   (kind, item_id, status, old["note"] if note is None else note, _now(), by))


# ------------------------------------------------------------ 問い合わせ票


def _ticket(db, row) -> dict:
    tid, created_at, category, priority, payload = row
    data = json.loads(payload)
    return {"ticket_id": tid, "created_at": created_at, "category": category, "priority": priority,
            "problem": data.get("problem", ""),
            "requester": {k: (data.get("requester") or {}).get(k, "") for k in ("employee_no", "name", "dept")},
            **_status(db, "ticket", tid)}


def tickets(db, status: str | None = None, priority: str | None = None) -> list[dict]:
    ensure_schema(db)
    rows = db.execute("SELECT ticket_id, created_at, category, priority, payload FROM helpdesk_tickets "
                      "ORDER BY created_at DESC, ticket_id DESC")
    out = [_ticket(db, r) for r in rows]
    return [t for t in out if (not status or t["status"] == status) and (not priority or t["priority"] == priority)]


def ticket(db, ticket_id: str) -> dict | None:
    ensure_schema(db)
    row = db.execute("SELECT ticket_id, created_at, category, priority, payload FROM helpdesk_tickets WHERE ticket_id = ?",
                     (ticket_id,)).fetchone()
    if not row:
        return None
    return {**_ticket(db, row), "detail": json.loads(row[4])}


# ------------------------------------------------------------ 受付の記録


def _record(db, row) -> dict:
    rid, created_at, channel, category, route, priority, payload = row
    data = json.loads(payload)
    visitor = data.get("visitor") or {}
    return {"record_id": rid, "created_at": created_at, "channel": channel, "category": category, "route": route,
            "priority": priority, "purpose": data.get("purpose", ""),
            "visitor": {"company": visitor.get("company", ""), "name": visitor.get("name", "")},
            "contact": data.get("contact", ""), "details": data.get("details", ""),
            "host": data.get("host"), "candidates": data.get("candidates", []),
            "resolved": bool(data.get("resolved")), **_status(db, "record", rid)}


def records(db, status: str | None = None, day: str | None = None) -> list[dict]:
    ensure_schema(db)
    rows = db.execute("SELECT record_id, created_at, channel, category, route, priority, payload FROM reception_records "
                      "ORDER BY created_at DESC, record_id DESC")
    out = [_record(db, r) for r in rows]
    return [r for r in out if (not status or r["status"] == status) and (not day or r["created_at"][:10] == day)]


def record_exists(db, record_id: str) -> bool:
    ensure_schema(db)
    return db.execute("SELECT 1 FROM reception_records WHERE record_id = ?", (record_id,)).fetchone() is not None


def add_record(db, *, channel: str, category: str, purpose: str, visitor: dict, host: dict | None, route: str,
               priority: str, appointment: dict | None = None) -> str:
    """会話の流れを通らない受付（受付端末でコードを入れたとき）も、記録に残す。番号の付け方は会話の流れと同じ。"""
    ensure_schema(db)
    now = datetime.now().astimezone()
    n = db.execute("SELECT COUNT(*) FROM reception_records").fetchone()[0] + 1
    rid = f"RC-{now:%Y%m%d}-{n:04d}"
    r = {"record_id": rid, "created_at": now.isoformat(timespec="seconds"), "channel": channel, "category": category,
         "purpose": purpose, "visitor": visitor, "contact": "", "details": "",
         "host": {"employee_no": host["employee_no"], "name": host["name"]} if host else None,
         "host_said": "", "candidates": [], "appointment": appointment, "answer": None, "resolved": False,
         "route": route, "priority": priority, "exclude_when_external": ["visitor", "contact", "host"]}
    with db:
        db.execute("INSERT INTO reception_records VALUES (?, ?, ?, ?, ?, ?, ?)",
                   (rid, r["created_at"], channel, category, route, priority, json.dumps(r, ensure_ascii=False)))
    return rid
