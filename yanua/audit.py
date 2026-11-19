"""監査ログ（K-009）。誰が・いつ・何をしたかを、あとから確かめられるようにする。

- 置き場所は data/audit.db（SQLite）。社内文書の保存先とは別のファイルにする
- 記録は足すだけ。画面にも API にも、消す・書き換える操作はない。データベースの中でも、書き換え（UPDATE）と、
  保存期間の内の記録を消すこと（DELETE）は、トリガーで断る
- 保存期間（既定 365日）を過ぎたものは、起動したときと1日1回まとめて消し、消したこと自体を1件記録する
- 記録に失敗しても、元の操作は止めない（サーバーのログにエラーを出す）
- 残さないもの：パスワード・端末トークン・セッションのトークン・入力されたコード（どの設定でも）。
  質問・会話の本文、文書や PDF の中身は、既定では残さない（keep_body を on にしたときだけ body に残す）

1件の形：
    at          時刻（UTC、ISO 8601）
    actor       した人の番号（社員番号。受付端末なら端末番号。ログイン前は「なし」。サーバー自身は「システム」）
    actor_name  名前（社員の名前・端末名。ログイン前は「なし」）
    actor_kind  社員・受付端末・なし・システム・サーバーの操作
    action      操作の名前（例 auth.login、paper.rules.save）
    target      対象（例 ルール名、ジョブ番号）
    result      成功・失敗・拒否
    ip          接続元の IP（リクエストのないジョブなどは空）
    detail      短い説明
    body        本文（keep_body のときだけ。ふだんは空）
"""

from __future__ import annotations

import contextvars
import logging
import re
import sqlite3
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("yanua.audit")

OK, FAILED, DENIED = "成功", "失敗", "拒否"
RESULTS = (OK, FAILED, DENIED)
EMPLOYEE, DEVICE, NOBODY, SYSTEM = "社員", "受付端末", "なし", "システム"
SERVER_OP = "サーバーの操作"      # サーバーに入れる人が、復旧用のコマンド（python -m yanua access）で変えたとき

# リクエストの接続元。API の入口（app.py のミドルウェア）で入れる。ジョブのスレッドなどでは空
client_ip: contextvars.ContextVar[str] = contextvars.ContextVar("yanua_client_ip", default="")

SCHEMA = """
CREATE TABLE IF NOT EXISTS audit_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    at          TEXT NOT NULL,
    actor       TEXT NOT NULL,
    actor_name  TEXT NOT NULL,
    actor_kind  TEXT NOT NULL,
    action      TEXT NOT NULL,
    target      TEXT NOT NULL DEFAULT '',
    result      TEXT NOT NULL,
    ip          TEXT NOT NULL DEFAULT '',
    detail      TEXT NOT NULL DEFAULT '',
    body        TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log(at);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log(actor, at);
CREATE INDEX IF NOT EXISTS idx_audit_action ON audit_log(action, at);
CREATE TABLE IF NOT EXISTS audit_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TRIGGER IF NOT EXISTS audit_no_update BEFORE UPDATE ON audit_log
BEGIN SELECT RAISE(ABORT, '監査ログは書き換えられません'); END;
CREATE TRIGGER IF NOT EXISTS audit_no_delete BEFORE DELETE ON audit_log
WHEN old.at >= COALESCE((SELECT value FROM audit_meta WHERE key = 'purge_before'), '')
BEGIN SELECT RAISE(ABORT, '監査ログは消せません（保存期間を過ぎたものだけを、決まった処理で消します）'); END;
"""

# 念のため、端末トークン（yk_…）の形の文字は、どこに入っていても伏せる
_SECRETS = re.compile(r"yk_[A-Za-z0-9_\-]{16,}")
_LIMITS = {"target": 300, "detail": 1000}


def _now() -> datetime:
    return datetime.now(timezone.utc)


def iso(t: datetime) -> str:
    return t.astimezone(timezone.utc).isoformat(timespec="seconds")


def _clean(text: str, limit: int | None = None) -> str:
    text = _SECRETS.sub("（伏せた値）", str(text or "")).replace("\r", " ").replace("\n", " ")
    return text if limit is None or len(text) <= limit else text[: limit - 1] + "…"


class Auditor:
    def __init__(self, path: Path | str, keep_days: int = 365, keep_body: bool = False,
                 names: Callable[[str], str | None] | None = None):
        """names：社員番号から名前を引く関数（記録に名前を入れるため）。"""
        self.path = Path(path)
        self.keep_days = keep_days
        self.keep_body = keep_body
        self.names = names or (lambda no: None)
        self._lock = threading.Lock()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(SCHEMA)

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(str(self.path), timeout=10)

    # ------------------------------------------------------------ 書く

    def record(self, action: str, *, actor: str | None = None, actor_name: str | None = None, kind: str | None = None,
               target: str = "", result: str = OK, detail: str = "", body: str | None = None, ip: str | None = None,
               at: datetime | None = None, strict: bool = False) -> None:
        """1件足す。失敗しても例外は出さない（元の操作を止めない）。actor が None ならログイン前（なし）。
        strict のときは、記録できなければ例外を出す（記録できないなら変えない、復旧用のコマンドで使う）。"""
        try:
            if result not in RESULTS:
                raise ValueError(f"結果は {RESULTS} のどれかにしてください: {result}")
            if actor is None:
                actor, actor_name, kind = NOBODY, NOBODY, NOBODY
            kind = kind or EMPLOYEE
            if actor_name is None:
                actor_name = (self.names(actor) if kind == EMPLOYEE else None) or actor
            row = (iso(at or _now()), _clean(actor, 100), _clean(actor_name, 100), kind, _clean(action, 100),
                   _clean(target, _LIMITS["target"]), result, _clean(client_ip.get() if ip is None else ip, 64),
                   _clean(detail, _LIMITS["detail"]), _clean(body) if (self.keep_body and body) else None)
            with self._lock, self._connect() as db:
                db.execute("INSERT INTO audit_log (at, actor, actor_name, actor_kind, action, target, result, ip, detail, body)"
                           " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", row)
        except Exception:  # noqa: BLE001  記録できなくても、元の操作は止めない
            log.exception("監査ログに記録できませんでした（%s）", action)
            if strict:
                raise

    def purge(self, now: datetime | None = None) -> int:
        """保存期間を過ぎた記録をまとめて消す。消したら、そのこと自体を1件記録する。"""
        cutoff = iso((now or _now()) - timedelta(days=self.keep_days))
        try:
            with self._lock, self._connect() as db:
                db.execute("INSERT INTO audit_meta (key, value) VALUES ('purge_before', ?)"
                           " ON CONFLICT(key) DO UPDATE SET value = excluded.value", (cutoff,))
                n = db.execute("DELETE FROM audit_log WHERE at < ?", (cutoff,)).rowcount
                db.execute("DELETE FROM audit_meta WHERE key = 'purge_before'")
        except Exception:  # noqa: BLE001
            log.exception("保存期間を過ぎた監査ログを消せませんでした")
            return 0
        if n:
            self.record("audit.purge", actor=SYSTEM, actor_name=SYSTEM, kind=SYSTEM, ip="",
                        detail=f"保存期間（{self.keep_days}日）を過ぎた {n}件を消しました（{cutoff} より前）")
        return n

    # ------------------------------------------------------------ 読む

    def query(self, *, since: str | None = None, until: str | None = None, actor: str = "", action: str = "",
              result: str = "", page: int = 1, per_page: int = 50) -> tuple[list[dict], int]:
        """新しい順。since・until は UTC の ISO の時刻（since 以上、until 未満）。
        actor は番号か名前の一部。action は名前そのものか、先頭の区切り（例 auth なら auth.login・auth.logout）。"""
        where, args = [], []
        if since:
            where.append("at >= ?")
            args.append(since)
        if until:
            where.append("at < ?")
            args.append(until)
        if actor:
            like = "%" + actor.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append("(actor = ? OR actor_name LIKE ? ESCAPE '\\')")
            args += [actor, like]
        if action:
            where.append("(action = ? OR action LIKE ? ESCAPE '\\')")
            args += [action, action.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + ".%"]
        if result:
            where.append("result = ?")
            args.append(result)
        sql_where = (" WHERE " + " AND ".join(where)) if where else ""
        keys = ("id", "at", "actor", "actor_name", "actor_kind", "action", "target", "result", "ip", "detail", "body")
        with self._connect() as db:
            total = db.execute(f"SELECT COUNT(*) FROM audit_log{sql_where}", args).fetchone()[0]
            rows = db.execute(f"SELECT {', '.join(keys)} FROM audit_log{sql_where} ORDER BY at DESC, id DESC LIMIT ? OFFSET ?",
                              [*args, per_page, (max(1, page) - 1) * per_page]).fetchall()
        return [dict(zip(keys, r)) for r in rows], total

    def actions(self) -> list[str]:
        with self._connect() as db:
            return [r[0] for r in db.execute("SELECT DISTINCT action FROM audit_log ORDER BY action")]

    def all_text(self) -> str:
        """記録の全文（テストで、秘密の値が入っていないことを確かめるため）。"""
        with self._connect() as db:
            return "\n".join("\t".join("" if v is None else str(v) for v in r) for r in db.execute("SELECT * FROM audit_log"))
