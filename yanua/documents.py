"""社内資料の読み込みと分割。

1文書1ファイルのMarkdownを読み、先頭の項目（id・title・access など）と本文に分ける。
本文は見出し（## ）ごとの区切り（チャンク）に分け、検索の単位にする。

閲覧できる部署（access）が書かれていない文書は取り込まない。
書き忘れた文書が全員に見えてしまうのを防ぐため、既定では閉じる側に倒す。
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

SUPPORTED_SUFFIXES = (".md", ".markdown", ".txt")

# 1つのチャンクの目安の長さ（文字数）。これを超える節は段落で分ける。
MAX_CHUNK_CHARS = 600


class DocumentError(ValueError):
    """取り込めない文書。メッセージにファイル名を含める。"""


@dataclass
class Chunk:
    position: int
    section: str
    text: str


@dataclass
class Document:
    id: str
    title: str
    access: tuple[str, ...]
    owner: str
    updated: str
    path: Path
    body: str
    content_hash: str
    chunks: list[Chunk] = field(default_factory=list)


def parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    """先頭の --- で囲まれた「キー: 値」を読む。"""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    meta: dict[str, str] = {}
    for line in raw[3:end].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    body = raw[end + len("\n---"):].lstrip("\n")
    return meta, body


def parse_access(value: str) -> tuple[str, ...]:
    return tuple(dict.fromkeys(a.strip() for a in re.split(r"[,、]", value or "") if a.strip()))


_HEADING = re.compile(r"^(#{1,6})\s+(.*)$")


def split_chunks(title: str, body: str, max_chars: int = MAX_CHUNK_CHARS) -> list[Chunk]:
    """本文を見出しごとに分ける。各チャンクの先頭に文書名と見出しを付け、単体でも意味が通るようにする。"""
    sections: list[tuple[str, list[str]]] = []
    current_heading = ""
    current_lines: list[str] = []
    for line in body.splitlines():
        m = _HEADING.match(line)
        if m and len(m.group(1)) >= 2:
            if any(l.strip() for l in current_lines):
                sections.append((current_heading, current_lines))
            current_heading = m.group(2).strip()
            current_lines = []
        elif m:  # 「# 文書名」は文書名と同じなので本文に含めない
            continue
        else:
            current_lines.append(line)
    if any(l.strip() for l in current_lines):
        sections.append((current_heading, current_lines))

    chunks: list[Chunk] = []
    for heading, lines in sections:
        text = "\n".join(lines).strip()
        for part in _split_long(text, max_chars):
            label = f"{title} / {heading}" if heading else title
            chunks.append(Chunk(position=len(chunks), section=heading, text=f"{label}\n{part}"))
    return chunks


def _split_long(text: str, max_chars: int) -> list[str]:
    if len(text) <= max_chars:
        return [text]
    parts: list[str] = []
    buf = ""
    for para in re.split(r"\n\s*\n", text):
        if buf and len(buf) + len(para) + 2 > max_chars:
            parts.append(buf.strip())
            buf = ""
        buf += para + "\n\n"
    if buf.strip():
        parts.append(buf.strip())
    return parts


def load_document(path: Path) -> Document:
    raw = path.read_text(encoding="utf-8")
    meta, body = parse_front_matter(raw)
    doc_id = meta.get("id", "").strip()
    if not doc_id:
        raise DocumentError(f"{path.name}: 先頭に id がありません")
    access = parse_access(meta.get("access", ""))
    if not access:
        raise DocumentError(f"{path.name}: 先頭に access（閲覧できる部署）がありません")
    title = meta.get("title", "").strip() or path.stem
    doc = Document(
        id=doc_id,
        title=title,
        access=access,
        owner=meta.get("owner", "").strip(),
        updated=meta.get("updated", "").strip(),
        path=path,
        body=body,
        content_hash=hashlib.sha256(raw.encode("utf-8")).hexdigest(),
    )
    doc.chunks = split_chunks(title, body)
    if not doc.chunks:
        raise DocumentError(f"{path.name}: 本文が空です")
    return doc


def find_files(source: Path) -> list[Path]:
    return sorted(p for p in source.rglob("*") if p.is_file() and p.suffix.lower() in SUPPORTED_SUFFIXES and p.name != "README.md")
