"""画面とのファイルの受け渡し（段階3：PDF・録画のアップロード、変換した結果のダウンロード）。

- アップロードは、本文にファイルをそのまま送る（multipart を使わない。新しい依存を増やさないため）。
  受け取りながら大きさを数え、上限を超えたらその場で止めて消す
- 置き場所はサーバーの作業用フォルダ（settings.work_dir）。ファイル名はサーバーが決める（送られた名前は表示にだけ使う）
- アップロードしたファイルも結果のファイルも、期限（settings.file_keep_hours）が来たら消す
- 作業用フォルダの合計にも上限（settings.work_max_bytes）がある。置いているファイルと、受け取り中のファイルの
  合計が上限を超えるアップロードは断る（ディスクがいっぱいになって、ほかの処理が止まらないように）
- 開けるのは、そのファイルを作った人だけ。そのうえで、その種類の操作の権限（perm）を今も持っていること。
  ほかの人のファイル・期限切れ・ないものは、どれも同じ「見つかりません」にする
"""

from __future__ import annotations

import secrets
import shutil
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_files (
    file_id     TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    kind        TEXT NOT NULL,          -- pdf / recording / segments / result
    perm        TEXT NOT NULL,          -- 開くのに要る操作（roles.py の PERMISSIONS）
    name        TEXT NOT NULL,          -- 画面に出す名前
    path        TEXT NOT NULL,
    size        INTEGER NOT NULL,
    job_id      TEXT,
    created_at  TEXT NOT NULL,
    expires_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_work_files_expires ON work_files(expires_at);
"""

KEYS = ("file_id", "owner", "kind", "perm", "name", "path", "size", "job_id", "created_at", "expires_at")
BAD_NAME = str.maketrans({c: "_" for c in '\\/:*?"<>|\r\n\t'})


def _now() -> datetime:
    return datetime.now(timezone.utc)


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def safe_name(name: str, default: str = "file") -> str:
    """画面に出す名前。フォルダの区切りなどを除き、長さを切る。置き場所には使わない。"""
    name = (name or "").translate(BAD_NAME).strip(" .")
    return (name or default)[:120]


def new_id() -> str:
    return secrets.token_urlsafe(18)


def add(db: sqlite3.Connection, *, file_id: str, owner: str, kind: str, perm: str, name: str, path: Path, keep_hours: int,
        job_id: str | None = None) -> dict:
    ensure_schema(db)
    now = _now()
    row = (file_id, owner, kind, perm, safe_name(name), str(path), path.stat().st_size, job_id,
           now.isoformat(timespec="seconds"), (now + timedelta(hours=keep_hours)).isoformat(timespec="seconds"))
    with db:
        db.execute(f"INSERT INTO work_files ({', '.join(KEYS)}) VALUES ({', '.join('?' * len(KEYS))})", row)
    return public(dict(zip(KEYS, row)))


def public(f: dict) -> dict:
    """画面に返す項目（置き場所のパスは返さない）。"""
    return {k: f[k] for k in ("file_id", "kind", "name", "size", "created_at", "expires_at")}


def total_size(db: sqlite3.Connection) -> int:
    """作業用フォルダに置いているファイル（期限の来ていないもの）の大きさの合計。"""
    ensure_schema(db)
    return db.execute("SELECT COALESCE(SUM(size), 0) FROM work_files WHERE expires_at >= ?",
                      (_now().isoformat(),)).fetchone()[0]


def get(db: sqlite3.Connection, file_id: str, owner: str, kind: str | None = None) -> dict | None:
    """持ち主のファイルを返す。ほかの人のもの・期限切れ・消えたもの・種類が違うものは None（区別しない）。"""
    ensure_schema(db)
    r = db.execute(f"SELECT {', '.join(KEYS)} FROM work_files WHERE file_id = ?", (file_id,)).fetchone()
    if not r:
        return None
    f = dict(zip(KEYS, r))
    if f["owner"] != owner or f["expires_at"] < _now().isoformat() or (kind and f["kind"] != kind):
        return None
    if not Path(f["path"]).is_file():
        return None
    return f


def cleanup(db: sqlite3.Connection, work_dir: Path) -> int:
    """期限の来たファイルを消す。消した数を返す。"""
    ensure_schema(db)
    now = _now().isoformat()
    old = list(db.execute("SELECT file_id, path FROM work_files WHERE expires_at < ?", (now,)))
    root = work_dir.resolve()
    for _, path in old:
        p = Path(path).resolve()
        if p.is_relative_to(root):          # 念のため、作業用フォルダの外は消さない
            p.unlink(missing_ok=True)
            # 結果のフォルダ（results/<ジョブ>）が空になったら消す
            if p.parent != root and p.parent.is_dir() and not any(p.parent.iterdir()):
                shutil.rmtree(p.parent, ignore_errors=True)
    with db:
        db.executemany("DELETE FROM work_files WHERE file_id = ?", [(i,) for i, _ in old])
    return len(old)
