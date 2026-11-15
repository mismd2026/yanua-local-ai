"""ジョブ（すぐには終わらない処理）。

- 受け付けたらすぐジョブの番号を返し、画面は GET /api/jobs/{id} で進み具合を見る
- 状態は SQLite に残す（待ち・実行中・完了・失敗・取り消し）
- GPU がないので、重い処理は同時に workers 個（既定1）ずつ流す
- サーバーを再起動したとき、終わっていなかったジョブは「中断」として失敗にする（勝手にやり直さない）
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

log = logging.getLogger("yanua.jobs")

QUEUED, RUNNING, DONE, FAILED, CANCELLED = "待ち", "実行中", "完了", "失敗", "取り消し"
FINISHED = (DONE, FAILED, CANCELLED)

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    job_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    owner       TEXT NOT NULL,
    status      TEXT NOT NULL,
    progress    INTEGER NOT NULL DEFAULT 0,
    stage       TEXT NOT NULL DEFAULT '',
    params      TEXT NOT NULL,
    result      TEXT,
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_owner ON jobs(owner, created_at);
"""


class JobCancelled(Exception):
    pass


class JobContext:
    """ジョブの関数に渡す。進み具合を知らせ、取り消されていないか確かめる。"""

    def __init__(self, q: "JobQueue", job_id: str):
        self._q = q
        self.job_id = job_id

    def progress(self, percent: int, stage: str = "") -> None:
        if self._q.status_of(self.job_id) == CANCELLED:
            raise JobCancelled()
        self._q._update(self.job_id, progress=max(0, min(100, int(percent))), stage=stage)

    def cancelled(self) -> bool:
        return self._q.status_of(self.job_id) == CANCELLED


JobFn = Callable[[JobContext, dict], dict]


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class JobQueue:
    def __init__(self, db_path: Path | str, workers: int = 1):
        self.db_path = str(db_path)
        self.workers = workers
        self._kinds: dict[str, JobFn] = {}
        self._queue: queue.Queue[str | None] = queue.Queue()
        self._threads: list[threading.Thread] = []
        self._lock = threading.Lock()
        with self._connect() as db:
            db.executescript(SCHEMA)
            # 前回の起動で終わらなかったジョブ
            db.execute("UPDATE jobs SET status = ?, error = ?, finished_at = ? WHERE status IN (?, ?)",
                       (FAILED, "サーバーの再起動で中断しました", _now(), QUEUED, RUNNING))

    def _connect(self) -> sqlite3.Connection:
        return sqlite3.connect(self.db_path, timeout=10)

    def register(self, kind: str, fn: JobFn) -> None:
        self._kinds[kind] = fn

    # ------------------------------------------------------------ 受付と参照

    def submit(self, kind: str, owner: str, params: dict) -> dict:
        if kind not in self._kinds:
            raise ValueError(f"知らないジョブの種類です: {kind}")
        job_id = uuid.uuid4().hex
        with self._connect() as db:
            db.execute("INSERT INTO jobs (job_id, kind, owner, status, params, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                       (job_id, kind, owner, QUEUED, json.dumps(params, ensure_ascii=False), _now()))
        self._queue.put(job_id)
        return self.get(job_id)

    def get(self, job_id: str) -> dict | None:
        with self._connect() as db:
            row = db.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            cols = [c[0] for c in db.execute("SELECT * FROM jobs LIMIT 0").description]
        return self._as_dict(dict(zip(cols, row))) if row else None

    def list(self, owner: str | None = None, limit: int = 50) -> list[dict]:
        sql, args = "SELECT * FROM jobs", []
        if owner is not None:
            sql += " WHERE owner = ?"
            args.append(owner)
        sql += " ORDER BY created_at DESC LIMIT ?"
        args.append(limit)
        with self._connect() as db:
            cur = db.execute(sql, args)
            cols = [c[0] for c in cur.description]
            return [self._as_dict(dict(zip(cols, r))) for r in cur.fetchall()]

    def status_of(self, job_id: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
        return row[0] if row else None

    def cancel(self, job_id: str) -> dict | None:
        """待ちのジョブは取り消す。実行中のジョブは、次に進み具合を知らせたところで止まる。"""
        with self._lock, self._connect() as db:
            db.execute("UPDATE jobs SET status = ?, finished_at = ? WHERE job_id = ? AND status IN (?, ?)",
                       (CANCELLED, _now(), job_id, QUEUED, RUNNING))
        return self.get(job_id)

    @staticmethod
    def _as_dict(r: dict) -> dict:
        return {
            "job_id": r["job_id"], "kind": r["kind"], "owner": r["owner"], "status": r["status"],
            "progress": r["progress"], "stage": r["stage"],
            "result": json.loads(r["result"]) if r["result"] else None,
            "error": r["error"], "created_at": r["created_at"],
            "started_at": r["started_at"], "finished_at": r["finished_at"],
        }

    def _update(self, job_id: str, **values) -> None:
        cols = ", ".join(f"{k} = ?" for k in values)
        with self._lock, self._connect() as db:
            db.execute(f"UPDATE jobs SET {cols} WHERE job_id = ?", (*values.values(), job_id))

    # ------------------------------------------------------------ 実行

    def start(self) -> None:
        for i in range(self.workers):
            t = threading.Thread(target=self._work, name=f"yanua-job-{i}", daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        for _ in self._threads:
            self._queue.put(None)
        for t in self._threads:
            t.join(timeout=5)
        self._threads.clear()

    def run_one(self, job_id: str) -> None:
        """1つのジョブを、呼んだスレッドで実行する（ワーカーとテストから使う）。"""
        with self._lock, self._connect() as db:
            row = db.execute("SELECT kind, params, status FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
            if not row or row[2] != QUEUED:
                return
            db.execute("UPDATE jobs SET status = ?, started_at = ? WHERE job_id = ?", (RUNNING, _now(), job_id))
        kind, params = row[0], json.loads(row[1])
        try:
            result = self._kinds[kind](JobContext(self, job_id), params)
        except JobCancelled:
            return
        except Exception as e:  # ジョブの失敗はジョブの結果として残し、ワーカーは止めない
            log.exception("ジョブ %s（%s）が失敗しました", job_id, kind)
            if self.status_of(job_id) != CANCELLED:
                self._update(job_id, status=FAILED, error=str(e) or e.__class__.__name__, finished_at=_now())
            return
        if self.status_of(job_id) != CANCELLED:
            self._update(job_id, status=DONE, progress=100, result=json.dumps(result, ensure_ascii=False),
                         finished_at=_now())

    def _work(self) -> None:
        while True:
            job_id = self._queue.get()
            if job_id is None:
                return
            self.run_one(job_id)
