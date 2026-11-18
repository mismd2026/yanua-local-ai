"""画面からの会話（ヘルプデスク・受付端末の受付）。

- 会話の状態は LangGraph のチェックポイントとして SQLite（chat.db）に残す。サーバーを再起動しても続けられる
- 会話はそれを始めた人（受付端末なら、その端末）しか続けられない。ほかの人の会話の番号を送っても「見つかりません」と返す
- ヘルプデスクと受付は、同じ chat.db・同じ接続・同じチェックポイントを使う（別々に開くと書き込みが待たされるため）
- 返事のないまま chat_idle_minutes が過ぎた会話は消す
- ログインした人は、ログインの時点で本人が分かっているので、本人確認を省いてお困りごとから始める
"""

from __future__ import annotations

import contextvars
import sqlite3
import threading
import uuid
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.types import Command

from .. import hr
from ..flows.external.reception import build_app as build_reception
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


class ChatDB:
    """会話の一覧（chat_sessions）とチェックポイント。ヘルプデスクと受付で共有する。"""

    def __init__(self, chat_db_path: Path, idle_minutes: int):
        self.idle = timedelta(minutes=idle_minutes)
        Path(chat_db_path).parent.mkdir(parents=True, exist_ok=True)
        # チェックポイント用と、会話の一覧（chat_sessions）用で接続を分ける（書き込みのまとまりが混ざらないように）
        self._saver_conn = sqlite3.connect(str(chat_db_path), check_same_thread=False, timeout=10)
        # 会話の一覧用の接続は自動で確定する（書き込みを開いたままにして、もう片方を待たせないため）
        self._conn = sqlite3.connect(str(chat_db_path), check_same_thread=False, timeout=10, isolation_level=None)
        self._conn.executescript(SCHEMA)
        self.saver = SqliteSaver(self._saver_conn)
        self.saver.setup()
        self._lock = threading.Lock()          # chat_sessions の読み書き
        self._session_locks: dict[str, threading.Lock] = {}

    def close(self) -> None:
        self._conn.close()
        self._saver_conn.close()

    def session_lock(self, session_id: str) -> threading.Lock:
        with self._lock:
            return self._session_locks.setdefault(session_id, threading.Lock())

    def expire(self) -> None:
        limit = (_now() - self.idle).isoformat()
        with self._lock:
            old = [r[0] for r in self._conn.execute(
                "SELECT session_id FROM chat_sessions WHERE last_used < ? OR finished = 1 AND last_used < ?",
                (limit, limit))]
            # 会話の一覧の行は、1つ消すたびに確定する。確定しないまま次のチェックポイントを消そうとすると、
            # もう片方の接続（チェックポイント用）が書き込みを待たされて「database is locked」になる
            for sid in old:
                self.saver.delete_thread(sid)
                self._conn.execute("DELETE FROM chat_sessions WHERE session_id = ?", (sid,))
                self._conn.commit()
                self._session_locks.pop(sid, None)

    def create(self, flow: str, owner: str) -> str:
        self.expire()
        session_id = uuid.uuid4().hex
        now = _now().isoformat()
        with self._lock:
            self._conn.execute("INSERT INTO chat_sessions VALUES (?, ?, ?, ?, ?, 0)", (session_id, flow, owner, now, now))
            self._conn.commit()
        return session_id

    def open(self, flow: str, owner: str, session_id: str) -> None:
        """続けてよい会話かを確かめる。ほかの人・ほかの種類の会話は、ない会話と同じ返事にする。"""
        self.expire()
        with self._lock:
            row = self._conn.execute("SELECT owner, finished, flow FROM chat_sessions WHERE session_id = ?",
                                     (session_id,)).fetchone()
        if not row or row[0] != owner or row[2] != flow:
            # 他の人の会話かどうかを返事の違いから分からせない
            raise not_found("会話")
        if row[1]:
            raise ApiError(409, "finished", "この会話は終わっています。新しく始めてください")

    def mark(self, session_id: str, finished: bool) -> None:
        with self._lock:
            self._conn.execute("UPDATE chat_sessions SET last_used = ?, finished = ? WHERE session_id = ?",
                               (_now().isoformat(), int(finished), session_id))
            self._conn.commit()

    def delete(self, flow: str, owner: str, session_id: str) -> None:
        with self._lock:
            row = self._conn.execute("SELECT owner, flow FROM chat_sessions WHERE session_id = ?", (session_id,)).fetchone()
            if not row or row[0] != owner or row[1] != flow:
                raise not_found("会話")
            self.saver.delete_thread(session_id)
            self._conn.execute("DELETE FROM chat_sessions WHERE session_id = ?", (session_id,))
            self._conn.commit()
            self._session_locks.pop(session_id, None)

    def run(self, app, session_id: str, value, busy_check: bool = True) -> dict:
        lock = self.session_lock(session_id)
        if not lock.acquire(blocking=not busy_check):
            raise ApiError(409, "busy", "前のメッセージを処理しています。返事を待ってから送ってください")
        try:
            return app.invoke(value, {"configurable": {"thread_id": session_id}})
        finally:
            lock.release()


def _finished(state: dict) -> bool:
    return "__interrupt__" not in state


def _text(text: str) -> str:
    text = (text or "").strip()
    if not text:
        raise ApiError(400, "empty", "メッセージが空です")
    return text


# ヘルプデスクの1回の返事の間に、社内文書から答えた（断った）質問を集める。
# グラフのノードが別のスレッドで動いても、文脈は写されて同じ一覧に入る
_answered: contextvars.ContextVar[list | None] = contextvars.ContextVar("yanua_helpdesk_answers", default=None)


def _observe(question, answer, procedure=None) -> None:
    got = _answered.get()
    if got is not None:
        got.append((question, answer, procedure))


class HelpdeskSessions:
    FLOW = "helpdesk"

    def __init__(self, chats: ChatDB, db_path: Path, helpdesk_data: Path, llm: ChatModel, top_k: int, policy=None,
                 on_answer=None):
        """on_answer(User, 質問, Answer か None, 手順)：案内したたびに呼ぶ（監査ログに残すため）。"""
        self.on_answer = on_answer
        self.chats = chats
        self.policy = policy
        self.db_path = db_path
        self.helpdesk_data = helpdesk_data
        self.llm = llm
        self.top_k = top_k
        self._local = threading.local()
        self._generation = 0      # 方針を入れ替えた回数。スレッドごとのグラフを作り直す目印

    def set_policy(self, policy) -> None:
        """本人確認の方針を入れ替える（設定の画面で保存したとき。再起動しなくても、次の返事から使う）。"""
        self.policy = policy
        self._generation += 1

    def _app(self):
        # 社内文書の保存先（SQLite）はスレッドをまたいで使えないので、グラフもスレッドごとに作る
        if not hasattr(self._local, "store"):
            self._local.store = Store(self.db_path)
        if getattr(self._local, "generation", None) != self._generation:
            self._local.app = build_app(self._local.store, self.llm, self.helpdesk_data, self.top_k,
                                        checkpointer=self.chats.saver, policy=self.policy, observe=_observe)
            self._local.generation = self._generation
        return self._local.app

    def _app_store(self) -> Store:
        self._app()
        return self._local.store

    def _reply(self, session_id: str, state: dict) -> dict:
        finished = _finished(state)
        out = {"session_id": session_id, "finished": finished,
               "reply": state["result"] if finished else state["__interrupt__"][0].value}
        if finished:
            out["ticket_id"] = (state.get("ticket") or {}).get("ticket_id")
            out["sources"] = state.get("sources", [])
        self.chats.mark(session_id, finished)
        return out

    def start(self, user: User, account: str | None = None) -> dict:
        session_id = self.chats.create(self.FLOW, user.employee_no)
        if account is None:
            p = hr.lookup(self._app_store().db, user.employee_no) or {}
            start = {"account": "", "requester": {
                "employee_no": user.employee_no, "name": user.name, "dept": user.dept,
                "dept_code": p.get("dept_code", ""), "account": "", "account_type": "ログイン", "verified": "ログイン"}}
        else:
            start = {"account": account}   # 開発用：代表アカウントなどからの問い合わせを試す
        return self._run(user, session_id, start, busy_check=False)

    def send(self, user: User, session_id: str, text: str) -> dict:
        self.chats.open(self.FLOW, user.employee_no, session_id)
        text = _text(text)
        return self._run(user, session_id, Command(resume=text))

    def _run(self, user: User, session_id: str, value, **kw) -> dict:
        got: list = []
        token = _answered.set(got)
        try:
            state = self.chats.run(self._app(), session_id, value, **kw)
        finally:
            _answered.reset(token)
        if self.on_answer:
            for question, answer, procedure in got:
                self.on_answer(user, question, answer, procedure)
        return self._reply(session_id, state)

    def end(self, user: User, session_id: str) -> None:
        self.chats.delete(self.FLOW, user.employee_no, session_id)


class ReceptionSessions:
    """受付端末の受付の会話（来訪）。持ち主は端末（kiosk:<端末の番号>）。

    返事として端末に返すのは、受付の会話の文と、コードで受け付けたときの予定（visit）だけ。
    記録（reception_records）の中身は返さない（社内向けの項目があるため）。
    """

    FLOW = "reception"

    def __init__(self, chats: ChatDB, db_path: Path, reception_data: Path, llm: ChatModel, top_k: int,
                 today: date | None = None):
        self.chats = chats
        self.db_path = db_path
        self.reception_data = reception_data
        self.llm = llm
        self.top_k = top_k
        self.today = today
        self._local = threading.local()

    def _app(self):
        if not hasattr(self._local, "app"):
            self._local.store = Store(self.db_path)
            self._local.app = build_reception(self._local.store, self.llm, self.reception_data, self.today,
                                              self.top_k, checkpointer=self.chats.saver)
        return self._local.app

    def _reply(self, session_id: str, state: dict) -> dict:
        finished = _finished(state)
        out = {"session_id": session_id, "finished": finished,
               "reply": state["result"] if finished else state["__interrupt__"][0].value,
               # コードで受け付けたとき（welcome を通ったとき）だけ。名前で照合した予定は入館票にしない
               "visit": state.get("code_visit") and state.get("visit") if finished else None}
        self.chats.mark(session_id, finished)
        return out

    def start(self, owner: str) -> dict:
        session_id = self.chats.create(self.FLOW, owner)
        return self._reply(session_id, self.chats.run(self._app(), session_id, {"channel": "来訪"}, busy_check=False))

    def send(self, owner: str, session_id: str, text: str) -> dict:
        self.chats.open(self.FLOW, owner, session_id)
        text = _text(text)
        return self._reply(session_id, self.chats.run(self._app(), session_id, Command(resume=text)))
