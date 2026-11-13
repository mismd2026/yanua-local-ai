"""会話の流れで共通に使う小さな道具。"""

from __future__ import annotations

import json
import re
import unicodedata

from ..llm import ChatModel


def norm(text: str) -> str:
    return re.sub(r"\s", "", unicodedata.normalize("NFKC", text or ""))


def is_yes(text: str) -> bool:
    t = norm(text).lower()
    return t.startswith(("はい", "yes", "y", "ok", "そう", "解決", "できた", "直った", "なおった"))


def ask_json(llm: ChatModel, prompt: str) -> dict:
    """LLMにJSONで答えさせる。崩れた返事は空の辞書として扱う（小さなモデルはJSONを崩すことがある）。"""
    text = llm.chat([
        {"role": "system", "content": "指定された形式のJSONだけを返してください。説明は書かないでください。"},
        {"role": "user", "content": prompt},
    ])
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return {}
    try:
        data = json.loads(m.group(0))
    except ValueError:
        return {}
    return data if isinstance(data, dict) else {}
