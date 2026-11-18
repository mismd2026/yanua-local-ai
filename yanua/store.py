"""取り込んだ資料の保存先（SQLite）。

- documents: 文書ごとの情報と、内容のハッシュ
- doc_access: 文書を閲覧できる部署（1行に1部署）
- chunks: 検索の単位になる本文の区切り

取り込みは「変わった文書だけ」を反映する。内容のハッシュが同じ文書は何もしない。
取り込み元から消えた文書は、保存先からも消す。

検索用の本文の取り出し（chunks_for）は、必ず部署で絞り込んだSQLで行う。
見られない文書の本文は、検索の候補にすら入らない。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import ALL_ACCESS, PUBLIC_ACCESS
from .documents import Document, DocumentError, find_files, load_document

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    path         TEXT NOT NULL,
    title        TEXT NOT NULL,
    owner        TEXT NOT NULL DEFAULT '',
    updated      TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    indexed_at   TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS doc_access (
    doc_id     TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    department TEXT NOT NULL,
    PRIMARY KEY (doc_id, department)
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    section   TEXT NOT NULL,
    text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_access_dept ON doc_access(department);
"""


@dataclass
class IngestReport:
    added: list[str] = field(default_factory=list)
    updated: list[str] = field(default_factory=list)
    unchanged: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {k: getattr(self, k) for k in ("added", "updated", "unchanged", "deleted", "errors")}


@dataclass
class StoredChunk:
    chunk_id: int
    doc_id: str
    title: str
    section: str
    text: str


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.path)
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ 取り込み

    def ingest(self, source: Path) -> IngestReport:
        """source フォルダの資料を取り込む。同じ source から前回取り込んで、今回なくなった文書は消す。"""
        source = source.resolve()
        report = IngestReport()
        docs: dict[str, Document] = {}
        for path in find_files(source):
            try:
                doc = load_document(path)
            except DocumentError as e:
                report.errors.append(str(e))
                continue
            if doc.id in docs:
                report.errors.append(f"{path.name}: id {doc.id} が {docs[doc.id].path.name} と重複しています")
                continue
            docs[doc.id] = doc

        existing = {
            row[0]: (row[1], row[2])
            for row in self.db.execute("SELECT doc_id, content_hash, source FROM documents")
        }

        with self.db:
            for doc in docs.values():
                old = existing.get(doc.id)
                if old and old[1] != str(source):
                    report.errors.append(f"{doc.path.name}: id {doc.id} は別のフォルダ（{old[1]}）の文書で使われています")
                    continue
                if old and old[0] == doc.content_hash:
                    report.unchanged.append(doc.id)
                    continue
                self._write(doc, source)
                (report.updated if old else report.added).append(doc.id)

            for doc_id, (_, doc_source) in existing.items():
                if doc_source == str(source) and doc_id not in docs:
                    self.db.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
                    report.deleted.append(doc_id)
        return report

    def _write(self, doc: Document, source: Path) -> None:
        self.db.execute("DELETE FROM documents WHERE doc_id = ?", (doc.id,))
        self.db.execute(
            "INSERT INTO documents (doc_id, source, path, title, owner, updated, content_hash, indexed_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                doc.id,
                str(source),
                str(doc.path.relative_to(source)),
                doc.title,
                doc.owner,
                doc.updated,
                doc.content_hash,
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
            ),
        )
        self.db.executemany("INSERT INTO doc_access (doc_id, department) VALUES (?, ?)", [(doc.id, a) for a in doc.access])
        self.db.executemany(
            "INSERT INTO chunks (doc_id, position, section, text) VALUES (?, ?, ?, ?)",
            [(doc.id, c.position, c.section, c.text) for c in doc.chunks],
        )

    # ------------------------------------------------------------ 取り出し

    def chunks_for(self, department: str) -> list[StoredChunk]:
        """その部署が見られる文書のチャンクだけを返す。部署が空なら全社向け（と社外公開）の文書だけ。"""
        rows = self.db.execute(
            """
            SELECT c.chunk_id, c.doc_id, d.title, c.section, c.text
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            WHERE c.doc_id IN (
                SELECT doc_id FROM doc_access WHERE department IN (?, ?, ?)
            )
            ORDER BY c.doc_id, c.position
            """,
            (ALL_ACCESS, PUBLIC_ACCESS, department or ALL_ACCESS),
        )
        return [StoredChunk(*row) for row in rows]

    def all_chunks(self) -> list[StoredChunk]:
        """すべての文書のチャンク（監査ログで、部署の権限で見せなかった文書の件数を数えるときだけ使う。回答には使わない）。"""
        rows = self.db.execute(
            """
            SELECT c.chunk_id, c.doc_id, d.title, c.section, c.text
            FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
            ORDER BY c.doc_id, c.position
            """
        )
        return [StoredChunk(*row) for row in rows]

    def public_chunks(self) -> list[StoredChunk]:
        """社外公開の文書のチャンクだけを返す。社外の人向けの検索はこれだけを使う。"""
        rows = self.db.execute(
            """
            SELECT c.chunk_id, c.doc_id, d.title, c.section, c.text
            FROM chunks c
            JOIN documents d ON d.doc_id = c.doc_id
            WHERE c.doc_id IN (SELECT doc_id FROM doc_access WHERE department = ?)
            ORDER BY c.doc_id, c.position
            """,
            (PUBLIC_ACCESS,),
        )
        return [StoredChunk(*row) for row in rows]

    def can_view(self, doc_id: str, department: str) -> bool:
        row = self.db.execute(
            "SELECT 1 FROM doc_access WHERE doc_id = ? AND department IN (?, ?, ?)",
            (doc_id, ALL_ACCESS, PUBLIC_ACCESS, department or ALL_ACCESS),
        ).fetchone()
        return row is not None

    def document_sections(self, doc_id: str) -> dict | None:
        """文書の題名と、節ごとの本文を返す（出典を開く画面のため）。見てよいかは呼ぶ側で can_view で確かめる。"""
        row = self.db.execute("SELECT doc_id, title, updated FROM documents WHERE doc_id = ?", (doc_id,)).fetchone()
        if not row:
            return None
        sections: list[dict] = []
        title = row[1]
        for section, text in self.db.execute(
            "SELECT section, text FROM chunks WHERE doc_id = ? ORDER BY position", (doc_id,)
        ):
            # チャンクの先頭の「文書名 / 見出し」の行（検索のために付けたもの。documents.py）は画面では見出しにするので外す
            label = f"{title} / {section}" if section else title
            if text.startswith(label + "\n"):
                text = text[len(label) + 1:]
            # 同じ節が続けて分かれているときは1つにまとめる
            if sections and sections[-1]["section"] == section:
                sections[-1]["text"] += "\n" + text
            else:
                sections.append({"section": section, "text": text})
        return {"doc_id": row[0], "title": row[1], "updated": row[2], "sections": sections}

    def documents(self) -> list[dict]:
        rows = self.db.execute(
            """
            SELECT d.doc_id, d.title, d.path, d.updated, json_group_array(a.department)
            FROM documents d LEFT JOIN doc_access a ON a.doc_id = d.doc_id
            GROUP BY d.doc_id ORDER BY d.doc_id
            """
        )
        return [
            {"doc_id": r[0], "title": r[1], "path": r[2], "updated": r[3], "access": json.loads(r[4])}
            for r in rows
        ]
