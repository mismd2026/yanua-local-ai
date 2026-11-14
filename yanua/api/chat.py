"""画面からのヘルプデスクの会話。

- 会話の状態は LangGraph のチェックポイントとして SQLite（chat.db）に残す。サーバーを再起動しても続けられる
- 会話はそれを始めた人しか続けられない。ほかの人の会話の番号を送っても「見つかりません」と返す
- 返事のないまま chat_idle_minutes が過ぎた会話は消す
- 本人確認はログインした人の個人アカウントから始める（accounts.csv で社員番号を引く）
"""

from __future__ import annotations

import csv
import sqlite3
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from ..flows.internal.helpdesk import build_app
from ..llm import ChatModel
from ..store import Store
from .auth import User
from .errors import ApiError, not_found

SCHEMA = """
CREATE TABLE IF NOT EXISTS chat_sessions (
    session_id TEXT PRIMARY KEY,
    flow       TEXT NOT NULL,
    owner      TEXT NOT NULL,
    created_at TEXT NOT NULL,
    last_used  TEXT NOT NULL,
    finished   INTEGER NOT NULL DEFAULT 0
);
"""


def _now() -> datetime:
    return datetime.now(timezone.utc)


def account_for(helpdesk_data: Path, employee_no: str) -> str:
    """その社員の個人アカウント。なければ空（ヘルプデスクが従業員番号から聞く）。"""
    with (helpdesk_data / "accounts.csv").open(encoding="utf-8") as f:
        for r in csv.DictReader(f):
            if r["type"] == "personal" and r["employee_no"] == employee_no:
                return r["account"]
    return ""


class HelpdeskSessions:
    def __init__(self, db_path: Path, chat_db_path: Path, helpdesk_data: Path, llm: ChatModel, top_k: int,
                 idle_minutes: int):
        self.db_path = db_path
        self.helpdesk_data = helpdesk_data
        self.llm = llm
        self.top_k = top_k
        self.idle = timedelta(minutes=idle_minutes)
        Path(chat_db_path).parent.mkdir(parents=True, exist_ok=True)
        # チェックポイント用と、会話の一覧（chat_sessions）用で接続を分ける（書き込みのまとまりが混ざらないように）
        self._saver_conn = sqlite3.connect(str(chat_db_path), check_same_thread=False, timeout=10)
        self._conn = sqlite3.connect(str(chat_db_path), check_same_thread=False, timeout=10)
        self._conn.executescript(SCHEMA)
        self.saver = SqliteSaver(self._saver_conn)
        self.saver.setup()
        self._local = threading.local()
        self._lock = threading.Lock()          # chat_sessions の読み書き
        self._session_locks: dict[str, threading.Lock] = {}

    def close(self) -> None:
        self._conn.close()
        self._saver_conn.close()

    def _app(self):
        # 社内文書の保存先（SQLite）はスレッドをまたいで使えないので、グラフもスレッドごとに作る
        if not hasattr(self._local, "app"):
            self._local.store = Store(self.db_path)
            self._local.app = build_app(self._local.store, self.llm, self.helpdesk_data, self.top_k,
                                        checkpointer=self.saver)
        return self._local.app

    def _session_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def _expire(self) -> None:
        limit = (_now() - self.idle).isoformat()
        with self._lock:
            old = [r[0] for r in self._conn.execute(
                "SELECT session_id FROM chat_sessions WHERE last_used < ? OR finished = 1 AND last_used < ?",
                (limit, limit))]
            for sid in old:
                self.saver.delete_thread(sid)
                self._conn.execute("DELETE FROM chat_sessions WHERE session_id = ?", (sid,))
                self._session_locks.pop(sid, None)
            self._conn.commit()

    def _reply(self, session_id: str, state: dict) -> dict:
        finished = "__interrupt__" not in state
        out = {"session_id": session_id, "finished": finished,
               "reply": state["result"] if finished else state["__interrupt__"][0].value}
        if finished:
            out["ticket_id"] = (state.get("ticket") or {}).get("ticket_id")
            out["sources"] = state.get("sources", [])
        with self._lock:
            self._conn.execute("UPDATE chat_sessions SET last_used = ?, finished = ? WHERE session_id = ?",
                               (_now().isoformat(), int(finished), session_id))
            self._conn.commit()
        return out

    def start(self, user: User, account: str | None = None) -> dict:
        self._expire()
        session_id = uuid.uuid4().hex
        now = _now().isoformat()
        with self._lock:
            self._conn.execute("INSERT INTO chat_sessions VALUES (?, 'helpdesk', ?, ?, ?, 0)",
                               (session_id, user.employee_no, now, now))
            self._conn.commit()
        if account is None:
            account = account_for(self.helpdesk_data, user.employee_no)
        config = {"configurable": {"thread_id": session_id}}
        with self._session_lock(session_id):
            state = self._app().invoke({"account": account}, config)
        return self._reply(session_id, state)

    def send(self, user: User, session_id: str, text: str) -> dict:
        self._expire()
        with self._lock:
            row = self._conn.execute("SELECT owner, finished FROM chat_sessions WHERE session_id = ?",
                                     (session_id,)).fetchone()
        if not row or row[0] != user.employee_no:
            # 他の人の会話かどうかを返事の違いから分からせない
            raise not_found("会話")
        if row[1]:
            raise ApiError(409, "finished", "この会話は終わっています。新しく始めてください")
        text = (text or "").strip()
        if not text:
            raise ApiError(400, "empty", "メッセージが空です")
        lock = self._session_lock(session_id)
        if not lock.acquire(blocking=False):
            raise ApiError(409, "busy", "前のメッセージを処理しています。返事を待ってから送ってください")
        try:
            state = self._app().invoke(Command(resume=text), {"configurable": {"thread_id": session_id}})
        finally:
            lock.release()
        return self._reply(session_id, state)

    def end(self, user: User, session_id: str) -> None:
        with self._lock:
            row = self._conn.execute("SELECT owner FROM chat_sessions WHERE session_id = ?", (session_id,)).fetchone()
            if not row or row[0] != user.employee_no:
                raise not_found("会話")
            self.saver.delete_thread(session_id)
            self._conn.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
            self._conn.commit()
            self._session_locks.pop(session_id, None)
