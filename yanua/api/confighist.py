"""設定のファイルの版の記録（保存の前の版を残し、元に戻せるようにする）。

仕分けルール（K-047）で使い始め、段階4の設定の画面（S-52）でも同じ仕組みを使う。

- 画面から保存するたびに、保存した中身を1つの版として残す（だれが・いつ・なにをしたか）
- 保存する前のファイルが、最後に残した版と違うとき（ファイルを直接直したときなど）は、その中身も先に1つの版として残す。
  どの版に戻しても、それまでの中身が消えないようにするため
- 元に戻すのも「前の版の中身を、新しい版として保存する」こと。戻したこともまた戻せる
- ファイルは、同じフォルダの一時ファイルに書いてから置き換える（書いている途中で止まっても、壊れたファイルを残さない）
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
import threading
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS config_versions (
    kind      TEXT NOT NULL,          -- 設定の種類（paper_rules など）
    version   INTEGER NOT NULL,
    saved_at  TEXT NOT NULL,
    saved_by  TEXT NOT NULL,          -- 社員番号。ファイルを直接直した版は「（ファイル）」
    action    TEXT NOT NULL,          -- 画面で表示する、したこと（例：ルール「案内」を足した）
    sha256    TEXT NOT NULL,
    body      TEXT NOT NULL,
    PRIMARY KEY (kind, version)
);
"""

FILE_EDIT = "（ファイル）"
_lock = threading.Lock()   # 同じサーバーの中で、2人が同時に保存しても、版の番号とファイルが食い違わないように


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def _latest(db: sqlite3.Connection, kind: str) -> tuple[int, str] | None:
    r = db.execute("SELECT version, sha256 FROM config_versions WHERE kind = ? ORDER BY version DESC LIMIT 1", (kind,)).fetchone()
    return (r[0], r[1]) if r else None


def _add(db: sqlite3.Connection, kind: str, by: str, action: str, body: str) -> int:
    last = _latest(db, kind)
    version = (last[0] if last else 0) + 1
    db.execute("INSERT INTO config_versions (kind, version, saved_at, saved_by, action, sha256, body) VALUES (?, ?, ?, ?, ?, ?, ?)",
               (kind, version, _now(), by, action, _sha(body), body))
    return version


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def save(db: sqlite3.Connection, kind: str, path: Path, new_body, by: str, action: str) -> int:
    """new_body（文字、または今のファイルの中身を受け取って新しい中身を返す関数）を保存し、新しい版の番号を返す。

    関数を渡すと、ロックの中で今のファイルを読んでから中身を作る（ほかの人の保存を上書きで消さないため）。
    関数が例外を出したら、何も保存しない。
    """
    ensure_schema(db)
    with _lock:
        current = path.read_text(encoding="utf-8") if path.is_file() else None
        body = new_body(current) if callable(new_body) else new_body
        with db:
            last = _latest(db, kind)
            if current is not None and (last is None or last[1] != _sha(current)):
                _add(db, kind, FILE_EDIT, "保存の前の中身（ファイルの版）", current)
            version = _add(db, kind, by, action, body)
            _write(path, body)   # 書けなかったら、版の記録も取り消す（with db の中で例外になる）
        return version


def history(db: sqlite3.Connection, kind: str, limit: int = 50) -> list[dict]:
    ensure_schema(db)
    rows = db.execute("SELECT version, saved_at, saved_by, action FROM config_versions WHERE kind = ? "
                      "ORDER BY version DESC LIMIT ?", (kind, limit))
    return [dict(zip(("version", "saved_at", "saved_by", "action"), r)) for r in rows]


def get(db: sqlite3.Connection, kind: str, version: int) -> str | None:
    ensure_schema(db)
    r = db.execute("SELECT body FROM config_versions WHERE kind = ? AND version = ?", (kind, version)).fetchone()
    return r[0] if r else None


def current_version(db: sqlite3.Connection, kind: str, path: Path) -> int | None:
    """今のファイルが、どの版と同じか（ファイルを直接直していて、どの版とも違えば None）。"""
    ensure_schema(db)
    if not path.is_file():
        return None
    last = _latest(db, kind)
    return last[0] if last and last[1] == _sha(path.read_text(encoding="utf-8")) else None
