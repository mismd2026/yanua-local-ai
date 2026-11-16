"""受付端末（K-043）：端末の登録・端末トークン・入館票。

- 受付端末は人ではなく端末。管理者が登録すると端末トークンを1回だけ表示する。保存するのはハッシュだけ
- 端末トークン（Authorization: Bearer ...）で使えるのは /api/kiosk/... だけ。ほかの API は拒否する（app.py の入口で確かめる）
- /api/kiosk/... は端末トークン専用。社内のログインのクッキーでは使えない（端末の登録は /api/admin/kiosk-devices）
- 入館票は、受付端末の画面が check-in（または受付の会話）の返事の予定から作って印刷する。サーバーには入館票を残さない
- コードの照合は、違う日・間違い・取り消しのコードに、同じ形の返事を返す（visits.find_by_code が理由を区別しない）。
  かかる時間もそろえるため、受付の API の返事は決まった時間より早くは返さない（app.py。待つ間はほかの処理をふさがない）
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
import threading
import time
from collections import deque
from datetime import datetime, timezone

SCHEMA = """
CREATE TABLE IF NOT EXISTS kiosk_devices (
    device_id   TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    token_hash  TEXT NOT NULL UNIQUE,
    created_at  TEXT NOT NULL,
    created_by  TEXT NOT NULL,
    last_used   TEXT,
    revoked_at  TEXT
);
"""

FAILED_CODE = "恐れ入りますが、コードを確認できませんでした。お手数ですが、もう一度ご入力いただくか、ご用件をお聞かせください。"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def register(db: sqlite3.Connection, name: str, created_by: str) -> dict:
    """端末を登録し、端末トークンを返す。トークンはここでしか取り出せない。"""
    ensure_schema(db)
    device_id = "KD-" + secrets.token_hex(4).upper()
    token = "yk_" + secrets.token_urlsafe(32)
    now = _now().isoformat(timespec="seconds")
    with db:
        db.execute("INSERT INTO kiosk_devices (device_id, name, token_hash, created_at, created_by) VALUES (?, ?, ?, ?, ?)",
                   (device_id, name, _hash(token), now, created_by))
    return {"device_id": device_id, "name": name, "created_at": now, "token": token}


def devices(db: sqlite3.Connection) -> list[dict]:
    ensure_schema(db)
    keys = ("device_id", "name", "created_at", "created_by", "last_used", "revoked_at")
    rows = db.execute(f"SELECT {', '.join(keys)} FROM kiosk_devices ORDER BY created_at")
    return [dict(zip(keys, r)) for r in rows]


def revoke(db: sqlite3.Connection, device_id: str) -> bool:
    ensure_schema(db)
    with db:
        cur = db.execute("UPDATE kiosk_devices SET revoked_at = ? WHERE device_id = ? AND revoked_at IS NULL",
                         (_now().isoformat(timespec="seconds"), device_id))
    return cur.rowcount > 0


def device_for_token(db: sqlite3.Connection, token: str) -> dict | None:
    if not token:
        return None
    ensure_schema(db)
    row = db.execute("SELECT device_id, name FROM kiosk_devices WHERE token_hash = ? AND revoked_at IS NULL",
                     (_hash(token),)).fetchone()
    if not row:
        return None
    with db:
        db.execute("UPDATE kiosk_devices SET last_used = ? WHERE device_id = ?",
                   (_now().isoformat(timespec="seconds"), row[0]))
    return {"device_id": row[0], "name": row[1]}


class FailureLimiter:
    """端末ごとに、コードの照合に失敗した回数を数える。1分に max_per_minute 回を超えたら、照合せずに失敗と同じ返事にする
    （コードを片端から試されないようにするため）。止めているかどうかは返事に出さない（文言・形・時間とも同じ）。
    受付端末の画面は、自分で数えた失敗の回数から「受付の者をお呼びください」の案内を出す（web/src/kiosk）。"""

    def __init__(self, max_per_minute: int):
        self.max = max_per_minute
        self._fails: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def blocked(self, device_id: str) -> bool:
        with self._lock:
            q = self._fails.setdefault(device_id, deque())
            now = time.monotonic()
            while q and now - q[0] > 60:
                q.popleft()
            return len(q) >= self.max

    def fail(self, device_id: str) -> None:
        with self._lock:
            self._fails.setdefault(device_id, deque()).append(time.monotonic())


def visit_for_kiosk(visit: dict, host: dict | None) -> dict:
    """受付端末に見せる予定。来訪者が知っている内容（日時・会議室・担当者の名前と連絡先・ご自身の名前）だけにする。"""
    return {
        "date": visit["date"], "time": visit["time"], "room": visit["room"],
        "host_name": host["name"] if host else "担当の者",
        "host_contact": visit["host_contact"] or "受付にお声がけください",
        "visitor_company": visit["visitor_company"], "visitor_name": visit["visitor_name"],
    }
