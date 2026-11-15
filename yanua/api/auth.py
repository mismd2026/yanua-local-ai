"""ログインとセッション。

- ログインできたら、ランダムなトークンをクッキー（HttpOnly・SameSite=Lax）で渡す。保存するのはトークンのハッシュだけ
- 毎回のリクエストで人事データを引き直す。退職・在籍でなくなった人は、その時点で使えなくなる
- 本番の SSO（OIDC/SAML）は K-008。今は開発用ログイン（社員番号を選ぶ）だけ
"""

from __future__ import annotations

import hashlib
import secrets
import sqlite3
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

from .. import hr
from .roles import RoleConfig

COOKIE = "yanua_session"

SCHEMA = """
CREATE TABLE IF NOT EXISTS web_sessions (
    token_hash  TEXT PRIMARY KEY,
    employee_no TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    last_used   TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
"""


@dataclass
class User:
    employee_no: str
    name: str
    dept: str
    roles: list[str]

    def as_dict(self) -> dict:
        return {"employee_no": self.employee_no, "name": self.name, "dept": self.dept, "roles": self.roles}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def create_session(db: sqlite3.Connection, employee_no: str, max_hours: int) -> str:
    ensure_schema(db)
    token = secrets.token_urlsafe(32)
    now = _now()
    with db:
        db.execute("INSERT INTO web_sessions VALUES (?, ?, ?, ?, ?)",
                   (_hash(token), employee_no, now.isoformat(), now.isoformat(),
                    (now + timedelta(hours=max_hours)).isoformat()))
        # 期限の切れたセッションは、ついでに消す
        db.execute("DELETE FROM web_sessions WHERE expires_at < ?", (now.isoformat(),))
    return token


def delete_session(db: sqlite3.Connection, token: str) -> None:
    ensure_schema(db)
    with db:
        db.execute("DELETE FROM web_sessions WHERE token_hash = ?", (_hash(token),))


def user_for_token(db: sqlite3.Connection, token: str, roles: RoleConfig, idle_minutes: int) -> User | None:
    """トークンからログイン中の人を返す。期限切れ・操作のない時間が長すぎる・在籍でない人は None。"""
    if not token:
        return None
    ensure_schema(db)
    h = _hash(token)
    row = db.execute("SELECT employee_no, last_used, expires_at FROM web_sessions WHERE token_hash = ?", (h,)).fetchone()
    if not row:
        return None
    now = _now()
    employee_no, last_used, expires_at = row
    if now > datetime.fromisoformat(expires_at) or now - datetime.fromisoformat(last_used) > timedelta(minutes=idle_minutes):
        delete_session(db, token)
        return None
    person = hr.lookup(db, employee_no)
    if not person:
        delete_session(db, token)
        return None
    with db:
        db.execute("UPDATE web_sessions SET last_used = ? WHERE token_hash = ?", (now.isoformat(), h))
    return User(person["employee_no"], person["name"], person["dept"], roles.roles_for(person["employee_no"], person["dept"]))
