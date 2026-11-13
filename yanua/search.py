"""部署の権限つき検索。

BM25 で、質問に近いチャンクを探す。日本語は単語に区切らず、2文字ずつの組（バイグラム）で比べる。
形態素解析の辞書やモデルを使わないため、外部と通信せずにどこでも同じ結果になる。

権限は、検索の前に Store.chunks_for で絞り込む。
LLMへの指示や、検索結果からの後での除外に頼らないので、見られない文書は候補にも入らない。
"""

from __future__ import annotations

import math
import re
import unicodedata
from collections import Counter
from dataclasses import dataclass

from .store import Store, StoredChunk

K1 = 1.5
B = 0.75

_ASCII_WORD = re.compile(r"[a-z0-9][a-z0-9@._\-]*")
_SKIP = re.compile(r"[\s\W_]+", re.UNICODE)


def normalize(text: str) -> str:
    text = unicodedata.normalize("NFKC", text).lower()
    return text.replace(",", "")


def tokenize(text: str) -> list[str]:
    """英数字は単語ごと、それ以外は記号や空白で区切った上で2文字ずつの組にする。"""
    text = normalize(text)
    tokens = _ASCII_WORD.findall(text)
    for run in _SKIP.split(_ASCII_WORD.sub(" ", text)):
        if len(run) == 1:
            tokens.append(run)
        tokens.extend(run[i : i + 2] for i in range(len(run) - 1))
    return tokens


@dataclass
class Hit:
    doc_id: str
    title: str
    section: str
    text: str
    score: float

    def as_dict(self) -> dict:
        return {"doc_id": self.doc_id, "title": self.title, "section": self.section, "score": round(self.score, 3), "text": self.text}


def bm25(query: str, chunks: list[StoredChunk], top_k: int) -> list[Hit]:
    if not chunks:
        return []
    docs_tokens = [tokenize(c.text) for c in chunks]
    n = len(chunks)
    avgdl = sum(len(t) for t in docs_tokens) / n
    df: Counter[str] = Counter()
    for tokens in docs_tokens:
        df.update(set(tokens))

    q_terms = Counter(tokenize(query))
    scored = []
    for chunk, tokens in zip(chunks, docs_tokens):
        tf = Counter(tokens)
        dl = len(tokens)
        score = 0.0
        for term, qtf in q_terms.items():
            f = tf.get(term)
            if not f:
                continue
            idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
            score += qtf * idf * f * (K1 + 1) / (f + K1 * (1 - B + B * dl / avgdl))
        if score > 0:
            scored.append(Hit(chunk.doc_id, chunk.title, chunk.section, chunk.text, score))
    scored.sort(key=lambda h: h.score, reverse=True)
    return scored[:top_k]


def search(store: Store, query: str, department: str, top_k: int = 5) -> list[Hit]:
    """その部署が見られる文書の中だけを検索する。"""
    return bm25(query, store.chunks_for(department), top_k)


def search_public(store: Store, query: str, top_k: int = 5) -> list[Hit]:
    """社外公開の文書の中だけを検索する（社外の人向け）。"""
    return bm25(query, store.public_chunks(), top_k)
