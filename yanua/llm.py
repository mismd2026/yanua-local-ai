"""Ollama との通信。

同じマシン（または社内のサーバー）で動く Ollama の /api/chat を呼ぶ。
社外には通信しない。標準ライブラリだけで動く。
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Protocol

DEFAULT_OLLAMA_URL = "http://localhost:11434"
DEFAULT_MODEL = "qwen2.5:3b"


class LLMError(RuntimeError):
    pass


class ChatModel(Protocol):
    name: str

    def chat(self, messages: list[dict]) -> str: ...


class OllamaChat:
    def __init__(self, model: str = DEFAULT_MODEL, base_url: str = DEFAULT_OLLAMA_URL, timeout: float = 300.0):
        self.name = model
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    def check(self) -> None:
        """Ollama に接続でき、モデルがあることを確かめる。問題があれば LLMError。"""
        try:
            with urllib.request.urlopen(f"{self.base_url}/api/tags", timeout=10) as res:
                data = json.loads(res.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, ValueError) as e:
            raise LLMError(
                f"Ollama（{self.base_url}）に接続できません: {e}。"
                "別のターミナルで `ollama serve` を実行しているか確認してください"
            ) from e
        names = [m.get("name", "") for m in data.get("models", [])]
        wanted = self.name if ":" in self.name else f"{self.name}:latest"
        if wanted not in names:
            raise LLMError(
                f"Ollama にモデル {self.name} がありません。今あるモデル: {', '.join(names) or 'なし'}。"
                f"`ollama pull {self.name}` を実行してください"
            )

    def chat(self, messages: list[dict]) -> str:
        body = json.dumps(
            {
                "model": self.name,
                "messages": messages,
                "stream": False,
                # 同じ質問には同じように答えるよう、ばらつきを抑える
                "options": {"temperature": 0},
            }
        ).encode("utf-8")
        req = urllib.request.Request(
            f"{self.base_url}/api/chat", data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as res:
                data = json.loads(res.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            # Ollama には届いたが、エラーが返ってきた（モデルがない、メモリが足りないなど）
            detail = e.read().decode("utf-8", errors="replace")
            try:
                detail = json.loads(detail).get("error", detail)
            except ValueError:
                pass
            if e.code == 404:
                raise LLMError(
                    f"Ollama にモデル {self.name} がありません（{detail}）。"
                    f"`ollama list` で名前を確認するか、`ollama pull {self.name}` を実行してください"
                ) from e
            raise LLMError(f"Ollama がエラーを返しました（HTTP {e.code}）: {detail}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            raise LLMError(f"Ollama（{self.base_url}）に接続できません: {e}") from e
        try:
            return data["message"]["content"]
        except (KeyError, TypeError) as e:
            raise LLMError(f"Ollama の応答の形が想定と違います: {data}") from e
