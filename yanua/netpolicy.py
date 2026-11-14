"""通信の方針：決めた相手（許可リスト）にだけ、決めたときだけ通信する。

- 許可リストは config/network.json に書く（ホスト名。"*.hf.co" のような前方のワイルドカードも可）
- ふだんの処理（文字起こし・要約・検索など）は通信しない。モデルの入手のような、管理者が実行するコマンドのときだけ通信する
- 通信は https だけ。証明書の検証は外さない
- アプリの中の制限は「うっかり」を防ぐためのもの。強制は会社のファイアウォール・プロキシで、同じ許可リストに絞って行う
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import urlsplit

DEFAULT_POLICY = Path(__file__).resolve().parent.parent / "config" / "network.json"


class NetworkBlocked(PermissionError):
    pass


@dataclass
class Policy:
    allowed_hosts: list[str]
    models: dict[str, dict] = field(default_factory=dict)

    def allows(self, url: str) -> bool:
        parts = urlsplit(url)
        if parts.scheme != "https" or not parts.hostname:
            return False
        host = parts.hostname.lower().rstrip(".")
        for rule in self.allowed_hosts:
            rule = rule.lower()
            if rule.startswith("*."):
                if host.endswith(rule[1:]) and host != rule[2:]:
                    return True
            elif host == rule:
                return True
        return False

    def check(self, url: str) -> None:
        if not self.allows(url):
            raise NetworkBlocked(f"許可リストにない宛先には通信しません: {urlsplit(url).hostname or url}"
                                 f"（必要なら config/network.json の allowed_hosts に追加）")


def load_policy(path: Path | None = None) -> Policy:
    path = path or Path(os.environ.get("YANUA_NETWORK_POLICY", DEFAULT_POLICY))
    data = json.loads(path.read_text(encoding="utf-8"))
    return Policy(list(data.get("allowed_hosts", [])), data.get("models", {}))


def offline() -> None:
    """ふだんの処理の前に呼ぶ。ライブラリ（Hugging Face）が勝手にダウンロードしないようにする。"""
    os.environ["HF_HUB_OFFLINE"] = "1"
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"


def guarded_hf_session(policy: Policy) -> None:
    """Hugging Face への通信を、許可リストの宛先だけに絞る（リダイレクト先も1件ずつ確かめる）。"""
    import huggingface_hub
    from huggingface_hub.utils._http import default_client_factory

    os.environ.pop("HF_HUB_OFFLINE", None)
    os.environ["HF_HUB_DISABLE_TELEMETRY"] = "1"
    # Xet（Rust で直接通信する仕組み）を使うと、ここでの宛先の確認を通らないので使わない
    os.environ["HF_HUB_DISABLE_XET"] = "1"

    def hook(request) -> None:
        policy.check(str(request.url))

    def factory():
        client = default_client_factory()
        client.event_hooks["request"].append(hook)
        return client

    huggingface_hub.set_client_factory(factory)
