"""社外への通信の入口（1か所）。Yanua のコードが社外に通信するときは、必ずここを通す（S-53・K-052）。

- つなぐ前に、宛先が「ON の宛先」（yanua/api/netsettings.py が決める。netpolicy.Policy の allowed_hosts）かを確かめ、
  そうでなければ断る（NetworkBlocked）。リダイレクト先も1件ずつ確かめる
- https だけ。証明書の検証は外さない
- このマシンの中（127.0.0.1・localhost・::1）は社外ではないので、ここでは断らない（手元の Ollama など）
- Hugging Face のトークンが要るときは、環境変数 HF_TOKEN から読むだけ。画面にも記録にも出さない
- Hugging Face のライブラリの通信（モデルの入手）は netpolicy.guarded_hf_session で、同じ確かめをする
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from urllib.parse import urlsplit

from .netpolicy import NetworkBlocked, Policy

LOOPBACK = {"127.0.0.1", "localhost", "::1"}
USER_AGENT = "yanua"


def is_local(url: str) -> bool:
    return (urlsplit(url).hostname or "").lower() in LOOPBACK


def check(url: str, policy: Policy | None) -> None:
    """ON の宛先でなければ、つなぐ前に断る。policy が None（network.json が読めない）なら、社外にはどこにもつながない。"""
    if is_local(url):
        return
    if policy is None:
        raise NetworkBlocked("通信の設定（config/network.json）が読めないので、社外には通信しません")
    policy.check(url)


class _CheckedRedirects(urllib.request.HTTPRedirectHandler):
    def __init__(self, policy: Policy | None):
        self.policy = policy

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check(newurl, self.policy)        # リダイレクト先も確かめる
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _headers(url: str, extra: dict | None) -> dict:
    h = {"User-Agent": USER_AGENT, **(extra or {})}
    host = (urlsplit(url).hostname or "").lower()
    token = os.environ.get("HF_TOKEN")
    if token and (host == "huggingface.co" or host.endswith(".huggingface.co")):
        h["Authorization"] = f"Bearer {token}"     # 環境変数から読むだけ。ほかには書かない
    return h


def fetch(url: str, policy: Policy | None, headers: dict | None = None, timeout: float = 30.0) -> bytes:
    """GET して本文を返す。宛先は、つなぐ前に確かめる。"""
    check(url, policy)
    return _open(url, policy, headers, timeout)


def _open(url: str, policy: Policy | None, headers: dict | None, timeout: float) -> bytes:
    """実際につなぐところ（テストでは、ここを偽のものに替える。宛先の確かめは fetch で済んでいる）。"""
    opener = urllib.request.build_opener(_CheckedRedirects(policy))
    req = urllib.request.Request(url, headers=_headers(url, headers))
    try:
        with opener.open(req, timeout=timeout) as res:
            return res.read()
    except urllib.error.HTTPError as e:
        raise ConnectionError(f"{urlsplit(url).hostname} が {e.code} を返しました") from e
    except (urllib.error.URLError, TimeoutError) as e:
        raise ConnectionError(f"{urlsplit(url).hostname} につながりません: {getattr(e, 'reason', e)}") from e


def fetch_json(url: str, policy: Policy | None, headers: dict | None = None, timeout: float = 30.0):
    return json.loads(fetch(url, policy, headers, timeout).decode("utf-8"))
