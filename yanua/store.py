"""取り込んだ資料の保存先（SQLite）。

- documents: 文書ごとの情報（持ち主の部署・見るのに要る権限・社外公開か・外したか）と、内容のハッシュ
- doc_targets: 文書の公開先（1行に1つ。権限の対象の番号：dept:部署・group:グループ・special:all（全社））
- chunks: 検索の単位になる本文の区切り

見られるのは、見るのに要る権限（既定 documents.view）を、公開先を含む対象で持っている人だけ（docs/access-model.md）。
公開先「全社」は、どの対象にも含まれる（要る権限をどこかに持っていれば見られる）。
社外公開（external）は、社外向けの受付だけが使う印。社内での見え方は公開先で決める（社外公開の文書は公開先「全社」）。

取り込みは「変わった文書だけ」を反映する。内容のハッシュが同じ文書は何もしない。
取り込み元から消えた文書は、保存先からも消す。公開先は、初めて取り込んだときにファイルの先頭（access）か画面で選んだもので決める。
取り込み直したとき、ファイルの先頭で決めた文書はファイルの access に合わせ直す。画面で決めた・直した文書は、画面のものを残す。

検索用の本文の取り出し（chunks_for）は、必ず見てよい範囲（DocScope）で絞り込んだSQLで行う。
見られない文書の本文は、検索の候補にすら入らない。外した文書（removed_at）は、どこにも出さない。
"""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from . import ALL_ACCESS, PUBLIC_ACCESS
from .documents import Document, DocumentError, find_files, load_document

EVERY_DEPARTMENT = "*"       # 部署で決める使い方（コマンド・照合したヘルプデスク）で「すべての部署」
ALL_TARGET = "special:all"   # 公開先「全社」（yanua/access.py の ALL と同じ）
VIEW = "documents.view"      # 見るのに要る権限の既定

SCHEMA = """
CREATE TABLE IF NOT EXISTS documents (
    doc_id       TEXT PRIMARY KEY,
    source       TEXT NOT NULL,
    path         TEXT NOT NULL,
    title        TEXT NOT NULL,
    owner        TEXT NOT NULL DEFAULT '',
    updated      TEXT NOT NULL DEFAULT '',
    content_hash TEXT NOT NULL,
    indexed_at   TEXT NOT NULL,
    required     TEXT NOT NULL DEFAULT 'documents.view',   -- 見るのに要る権限
    external     INTEGER NOT NULL DEFAULT 0,                -- 社外公開（社外向けの受付が使う）
    removed_at   TEXT,                                      -- 外した（検索に出さない）。空なら出す
    removed_by   TEXT NOT NULL DEFAULT '',
    decided_by   TEXT NOT NULL DEFAULT 'file'               -- 公開先をどこで決めたか：file（ファイルの先頭）・screen（画面）
);
CREATE TABLE IF NOT EXISTS doc_targets (
    doc_id     TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    target     TEXT NOT NULL,                               -- dept:部署・group:グループ・special:all
    PRIMARY KEY (doc_id, target)
);
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    doc_id    TEXT NOT NULL REFERENCES documents(doc_id) ON DELETE CASCADE,
    position  INTEGER NOT NULL,
    section   TEXT NOT NULL,
    text      TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_doc ON chunks(doc_id);
CREATE INDEX IF NOT EXISTS idx_targets_target ON doc_targets(target);
"""


@dataclass(frozen=True)
class DocScope:
    """見てよい文書の範囲：見るのに要る権限ごとに、その権限が効く公開先（対象の番号の集まり）。None なら、すべての公開先。
    権限を持っていなければ、その権限は入らない（その権限が要る文書は見られない）。公開先「全社」は、権限を持っていれば見られる。
    作るのは yanua/access.py の Engine.doc_scope（権限の判定と同じ決まり）。"""
    perms: tuple[tuple[str, frozenset[str] | None], ...]

    @classmethod
    def of(cls, perms: dict) -> "DocScope":
        return cls(tuple(sorted((p, None if t is None else frozenset(t)) for p, t in perms.items())))

    @classmethod
    def departments(cls, department) -> "DocScope":
        """部署で決める使い方（コマンドの search・ヘルプデスクで照合した人）：社員の役割と同じく、documents.view × その部署。
        部署が空なら公開先「全社」の文書だけ。EVERY_DEPARTMENT ならすべて。"""
        if isinstance(department, DocScope):
            return department
        if department == EVERY_DEPARTMENT:
            return cls.of({VIEW: None})
        depts = [department] if isinstance(department, str) or department is None else list(department)
        return cls.of({VIEW: {f"dept:{d}" for d in depts if d}})

    def to_json(self) -> dict:
        return {p: (None if t is None else sorted(t)) for p, t in self.perms}

    @classmethod
    def from_json(cls, data: dict) -> "DocScope":
        return cls.of({p: (None if t is None else set(t)) for p, t in (data or {}).items()})


def access_to_targets(access) -> tuple[list[str], bool]:
    """前の書き方（ファイルの先頭の access：部署・全社・社外公開）→ 公開先と、社外公開か。
    社外公開の文書は、社内では全社に見せていたので、公開先「全社」にする。"""
    targets, external = [], False
    for a in access:
        if a == PUBLIC_ACCESS:
            external = True
            targets.append(ALL_TARGET)
        elif a == ALL_ACCESS:
            targets.append(ALL_TARGET)
        else:
            targets.append(f"dept:{a}")
    return list(dict.fromkeys(targets)), external


@dataclass
class DocSettings:
    """取り込むときの公開先の決め方（画面で選んだもの）。None ならファイルの先頭の書き方を使う。"""
    owner: str
    targets: list[str]
    required: str = VIEW
    external: bool = False


def _visible_sql(scope: DocScope, alias: str = "d") -> tuple[str, list]:
    parts, args = [], []
    for perm, targets in scope.perms:
        if targets is None:
            parts.append(f"{alias}.required = ?")
            args.append(perm)
        else:
            ids = sorted(set(targets) | {ALL_TARGET})
            parts.append(f"({alias}.required = ? AND {alias}.doc_id IN "
                         f"(SELECT doc_id FROM doc_targets WHERE target IN ({', '.join('?' * len(ids))})))")
            args += [perm, *ids]
    where = " OR ".join(parts) if parts else "0"
    return f"({alias}.removed_at IS NULL AND ({where}))", args


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
        self._migrate_columns()
        self.db.executescript(SCHEMA)
        self._migrate_access()

    def _migrate_columns(self) -> None:
        cols = {r[1] for r in self.db.execute("PRAGMA table_info(documents)")}
        if cols and "required" not in cols:
            with self.db:
                self.db.execute("ALTER TABLE documents ADD COLUMN required TEXT NOT NULL DEFAULT 'documents.view'")
                self.db.execute("ALTER TABLE documents ADD COLUMN external INTEGER NOT NULL DEFAULT 0")
                self.db.execute("ALTER TABLE documents ADD COLUMN removed_at TEXT")
                self.db.execute("ALTER TABLE documents ADD COLUMN removed_by TEXT NOT NULL DEFAULT ''")
                self.db.execute("ALTER TABLE documents ADD COLUMN decided_by TEXT NOT NULL DEFAULT 'file'")

    def _migrate_access(self) -> None:
        """前の公開先（doc_access：部署・全社・社外公開）を、公開先（doc_targets）に移し替える（1回だけ）。
        公開先＝前の部署（全社は全社）。社外公開は、公開先「全社」と社外公開の印。"""
        if not self.db.execute("SELECT 1 FROM sqlite_master WHERE type = 'table' AND name = 'doc_access'").fetchone():
            return
        rows: dict[str, list[str]] = {}
        for doc_id, dept in self.db.execute("SELECT doc_id, department FROM doc_access ORDER BY rowid"):
            rows.setdefault(doc_id, []).append(dept)
        with self.db:
            for doc_id, access in rows.items():
                targets, external = access_to_targets(access)
                self.db.executemany("INSERT OR IGNORE INTO doc_targets (doc_id, target) VALUES (?, ?)", [(doc_id, t) for t in targets])
                if external:
                    self.db.execute("UPDATE documents SET external = 1 WHERE doc_id = ?", (doc_id,))
            self.db.execute("DROP TABLE doc_access")

    def close(self) -> None:
        self.db.close()

    # ------------------------------------------------------------ 取り込み

    def ingest(self, source: Path, chosen: DocSettings | None = None, owners=None) -> IngestReport:
        """source フォルダの資料を取り込む。同じ source から前回取り込んで、今回なくなった文書は消す。

        chosen：画面で選んだ持ち主の部署・公開先・要る権限（新しく足した文書だけに使う）。None ならファイルの先頭の access・owner。
        owners：扱ってよい持ち主の部署の集まり（documents.manage が効く部署）。None ならすべて。ほかの部署の文書は取り込まず、消しもしない。"""
        source = source.resolve()
        report = IngestReport()
        docs: dict[str, Document] = {}
        for path in find_files(source):
            try:
                doc = load_document(path, need_access=chosen is None)
                if chosen is not None:
                    doc.owner = chosen.owner
                if owners is not None and doc.owner not in owners:
                    raise DocumentError(f"{path.name}: 持ち主の部署（{doc.owner or 'なし'}）の文書は、あなたは扱えません")
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
                self._write(doc, source, keep=bool(old), chosen=chosen)
                (report.updated if old else report.added).append(doc.id)

            owner_of = {r[0]: r[1] for r in self.db.execute("SELECT doc_id, owner FROM documents")}
            for doc_id, (_, doc_source) in existing.items():
                if owners is not None and owner_of.get(doc_id) not in owners:
                    continue
                if doc_source == str(source) and doc_id not in docs:
                    self.db.execute("DELETE FROM documents WHERE doc_id = ?", (doc_id,))
                    report.deleted.append(doc_id)
        return report

    def _write(self, doc: Document, source: Path, keep: bool = False, chosen: DocSettings | None = None) -> None:
        """文書を書く。keep（取り込み直し）のときは、持ち主・要る権限・外したかを前のまま残す。公開先は、ファイルの先頭で
        決めた文書ならファイルの access に合わせ直し、画面で決めた文書なら画面のものを残す。"""
        now = datetime.now(timezone.utc).isoformat(timespec="seconds")
        if keep:
            self.db.execute("UPDATE documents SET path = ?, title = ?, updated = ?, content_hash = ?, indexed_at = ? WHERE doc_id = ?",
                            (str(doc.path.relative_to(source)), doc.title, doc.updated, doc.content_hash, now, doc.id))
            self.db.execute("DELETE FROM chunks WHERE doc_id = ?", (doc.id,))
            decided = self.db.execute("SELECT decided_by FROM documents WHERE doc_id = ?", (doc.id,)).fetchone()[0]
            if decided == "file" and chosen is None and doc.access:
                targets, external = access_to_targets(doc.access)
                self.db.execute("UPDATE documents SET external = ? WHERE doc_id = ?", (int(external), doc.id))
                self.db.execute("DELETE FROM doc_targets WHERE doc_id = ?", (doc.id,))
                self.db.executemany("INSERT INTO doc_targets (doc_id, target) VALUES (?, ?)", [(doc.id, t) for t in targets])
        else:
            if chosen is not None:
                targets, external, required = list(chosen.targets), chosen.external, chosen.required
            else:
                (targets, external), required = access_to_targets(doc.access), VIEW
            self.db.execute(
                "INSERT INTO documents (doc_id, source, path, title, owner, updated, content_hash, indexed_at, required, external,"
                " decided_by) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (doc.id, str(source), str(doc.path.relative_to(source)), doc.title, doc.owner, doc.updated, doc.content_hash, now,
                 required, int(external), "file" if chosen is None else "screen"),
            )
            self.db.executemany("INSERT INTO doc_targets (doc_id, target) VALUES (?, ?)", [(doc.id, t) for t in targets])
        self.db.executemany(
            "INSERT INTO chunks (doc_id, position, section, text) VALUES (?, ?, ?, ?)",
            [(doc.id, c.position, c.section, c.text) for c in doc.chunks],
        )

    # ------------------------------------------------------------ 取り出し

    def chunks_for(self, scope) -> list[StoredChunk]:
        """見てよい範囲（DocScope。部署の名前・部署の集まり・EVERY_DEPARTMENT も受ける）の文書のチャンクだけを返す。"""
        where, args = _visible_sql(DocScope.departments(scope))
        rows = self.db.execute(
            f"""
            SELECT c.chunk_id, c.doc_id, d.title, c.section, c.text
            FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
            WHERE {where}
            ORDER BY c.doc_id, c.position
            """,
            args,
        )
        return [StoredChunk(*row) for row in rows]

    def all_chunks(self) -> list[StoredChunk]:
        """外していない、すべての文書のチャンク（監査ログで、権限で見せなかった文書の件数を数えるときだけ使う。回答には使わない）。"""
        rows = self.db.execute(
            """
            SELECT c.chunk_id, c.doc_id, d.title, c.section, c.text
            FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
            WHERE d.removed_at IS NULL
            ORDER BY c.doc_id, c.position
            """
        )
        return [StoredChunk(*row) for row in rows]

    def public_chunks(self) -> list[StoredChunk]:
        """社外公開の文書のチャンクだけを返す。社外の人向けの検索はこれだけを使う。"""
        rows = self.db.execute(
            """
            SELECT c.chunk_id, c.doc_id, d.title, c.section, c.text
            FROM chunks c JOIN documents d ON d.doc_id = c.doc_id
            WHERE d.external = 1 AND d.removed_at IS NULL
            ORDER BY c.doc_id, c.position
            """
        )
        return [StoredChunk(*row) for row in rows]

    def can_view(self, doc_id: str, scope) -> bool:
        where, args = _visible_sql(DocScope.departments(scope))
        return self.db.execute(f"SELECT 1 FROM documents d WHERE d.doc_id = ? AND {where}", (doc_id, *args)).fetchone() is not None

    # ------------------------------------------------------------ 公開先（画面 S-50）

    def doc_settings(self, doc_id: str) -> dict | None:
        row = self.db.execute("SELECT doc_id, title, owner, required, external, removed_at, removed_by, decided_by FROM documents"
                              " WHERE doc_id = ?", (doc_id,)).fetchone()
        if not row:
            return None
        targets = [t for (t,) in self.db.execute("SELECT target FROM doc_targets WHERE doc_id = ? ORDER BY rowid", (doc_id,))]
        return {"doc_id": row[0], "title": row[1], "owner": row[2], "required": row[3], "external": bool(row[4]),
                "removed_at": row[5], "removed_by": row[6], "decided_by": row[7], "targets": targets}

    def set_publication(self, doc_id: str, targets: list[str], required: str, external: bool) -> None:
        """公開先・要る権限・社外公開だけを直す。本文とチャンクには触れない（作り直さない）。"""
        with self.db:
            self.db.execute("UPDATE documents SET required = ?, external = ?, decided_by = 'screen' WHERE doc_id = ?",
                            (required, int(external), doc_id))
            self.db.execute("DELETE FROM doc_targets WHERE doc_id = ?", (doc_id,))
            self.db.executemany("INSERT INTO doc_targets (doc_id, target) VALUES (?, ?)", [(doc_id, t) for t in targets])

    def set_removed(self, doc_id: str, removed: bool, by: str = "") -> None:
        with self.db:
            if removed:
                self.db.execute("UPDATE documents SET removed_at = ?, removed_by = ? WHERE doc_id = ?",
                                (datetime.now(timezone.utc).isoformat(timespec="seconds"), by, doc_id))
            else:
                self.db.execute("UPDATE documents SET removed_at = NULL, removed_by = '' WHERE doc_id = ?", (doc_id,))

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

    def documents(self, removed: bool = False) -> list[dict]:
        """文書の一覧（外したものは removed=True のときだけ）。access は前の形（部署・全社・社外公開）でも出す。"""
        rows = self.db.execute(
            f"""
            SELECT d.doc_id, d.title, d.path, d.updated, d.owner, d.required, d.external, d.indexed_at, d.source,
                   d.removed_at, d.removed_by, json_group_array(t.target), d.decided_by
            FROM documents d LEFT JOIN doc_targets t ON t.doc_id = d.doc_id
            WHERE d.removed_at IS {'NOT ' if removed else ''}NULL
            GROUP BY d.doc_id ORDER BY d.doc_id
            """
        )
        out = []
        for r in rows:
            targets = [t for t in json.loads(r[11]) if t]
            access = [ALL_ACCESS if t == ALL_TARGET else t.split(":", 1)[1] for t in targets]
            if r[6]:
                access = [PUBLIC_ACCESS if a == ALL_ACCESS else a for a in access]
            out.append({"doc_id": r[0], "title": r[1], "path": r[2], "updated": r[3], "access": access, "owner": r[4],
                        "required": r[5], "external": bool(r[6]), "indexed_at": r[7], "source": r[8], "removed_at": r[9],
                        "removed_by": r[10], "targets": targets, "decided_by": r[12]})
        return out
