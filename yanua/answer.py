"""出典つきの回答を作る。

1. 質問者の部署の権限で検索する（見られない文書は候補に入らない）
2. 検索で見つかった本文だけを番号つきでLLMに渡し、その範囲だけで答えさせる
3. 回答の中の [1] [2] などの番号から、出典の文書を決める
4. 資料に根拠がないときは、推測で答えずに「わかりません」と問い合わせ先を返す

LLMに渡すのは、質問者が見られる文書の本文だけ。
そのため、LLMがどう答えても、見られない文書の内容は回答に出てこない。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from .llm import ChatModel
from .search import Hit, search
from .store import Store

REFUSAL = "わかりません"
NOT_FOUND_MESSAGE = (
    "資料の中に、この質問に答えられる内容が見つかりませんでした。わかりません。"
    "担当の部署に問い合わせてください（問い合わせ窓口の一覧は社内ポータルにあります）。"
)

SYSTEM_PROMPT = """あなたは社内の資料に答えるアシスタントです。次の決まりを必ず守ってください。

- 下の「資料」に書かれている内容だけを使って、日本語で簡潔に答える。
- 答えに使った資料の番号を、文の後ろに [1] のように付ける。
- 資料に答えが書かれていない場合は、推測せずに「わかりません」とだけ答える。
- 資料にない数字・日付・名前を作らない。
"""


@dataclass
class Answer:
    answer: str
    sources: list[dict] = field(default_factory=list)
    refused: bool = False
    # LLMが実際に返した文（評価・調査用。serve --debug のときだけAPIの応答に含める）
    model_text: str = ""

    def as_dict(self, debug: bool = False) -> dict:
        d = {"answer": self.answer, "sources": self.sources, "refused": self.refused}
        if debug:
            d["model_text"] = self.model_text
        return d


def is_refusal(text: str) -> bool:
    """LLMの返事が「答えられない」という意味か。

    「わかりません」で始まる返事と、出典の番号がなく「わかりません」を含む返事を、答えられないとみなす。
    出典の番号を付けて答えた中に「わかりません」が混ざっているだけの返事は、回答として扱う。
    """
    head = text.strip().lstrip("「『[(（ ")
    if head.startswith(REFUSAL):
        return True
    return REFUSAL in text and not _CITATION.search(text)


def build_messages(question: str, hits: list[Hit]) -> list[dict]:
    context = "\n\n".join(f"[{i}] {h.text}" for i, h in enumerate(hits, start=1))
    user = f"資料:\n{context}\n\n質問: {question}"
    return [{"role": "system", "content": SYSTEM_PROMPT}, {"role": "user", "content": user}]


_CITATION = re.compile(r"\[(\d+)\]")


def cited_hits(text: str, hits: list[Hit]) -> list[Hit]:
    """回答の中の番号を、検索結果に対応させる。範囲外の番号は無視する。同じ文書は1つにまとめる。"""
    seen: set[str] = set()
    result = []
    for m in _CITATION.finditer(text):
        i = int(m.group(1))
        if 1 <= i <= len(hits) and hits[i - 1].doc_id not in seen:
            seen.add(hits[i - 1].doc_id)
            result.append(hits[i - 1])
    return result


def source_of(hit: Hit) -> dict:
    return {"doc_id": hit.doc_id, "title": hit.title, "section": hit.section}


def answer_question(store: Store, llm: ChatModel, question: str, department: str, top_k: int = 5) -> Answer:
    question = (question or "").strip()
    if not question:
        raise ValueError("質問が空です")

    hits = search(store, question, department, top_k)
    if not hits:
        return Answer(NOT_FOUND_MESSAGE, [], refused=True)

    text = llm.chat(build_messages(question, hits)).strip()

    if is_refusal(text):
        return Answer(NOT_FOUND_MESSAGE, [], refused=True, model_text=text)

    cited = cited_hits(text, hits)
    # 番号を付け忘れた回答は、いちばん近い検索結果を出典とする
    sources = [source_of(h) for h in (cited or hits[:1])]
    return Answer(text, sources, refused=False, model_text=text)
