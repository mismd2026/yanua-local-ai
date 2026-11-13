"""社内向けのHTTP API。

    POST /ask     {"question": "...", "department": "営業部"}
                  -> {"answer": "...", "sources": [{"doc_id": ..., "title": ..., "section": ...}], "refused": false}
    GET  /health  -> {"status": "ok", "model": "...", "documents": 20}

department は動作確認用の仮の項目。SSO（K-008）を入れた後は、ログインした人の所属部署を使う。
既定では 127.0.0.1（このマシンの中だけ）で待ち受ける。
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from .answer import answer_question
from .llm import ChatModel, LLMError
from .store import Store

MAX_BODY = 64 * 1024


def make_handler(db_path: str, llm: ChatModel, top_k: int, debug: bool = False):
    local = threading.local()

    def store() -> Store:
        # SQLite の接続はスレッドをまたいで使えないので、スレッドごとに開く
        if not hasattr(local, "store"):
            local.store = Store(db_path)
        return local.store

    class Handler(BaseHTTPRequestHandler):
        server_version = "yanua"

        def _send(self, status: int, payload: dict) -> None:
            body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            if self.path == "/health":
                self._send(200, {"status": "ok", "model": llm.name, "documents": len(store().documents())})
            else:
                self._send(404, {"error": "not found"})

        def do_POST(self):
            if self.path != "/ask":
                self._send(404, {"error": "not found"})
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length <= 0 or length > MAX_BODY:
                self._send(400, {"error": "リクエストの本文がないか、大きすぎます"})
                return
            try:
                data = json.loads(self.rfile.read(length).decode("utf-8"))
                question = str(data.get("question", ""))
                department = str(data.get("department", ""))
            except (ValueError, AttributeError):
                self._send(400, {"error": "JSONとして読めません"})
                return
            started = time.monotonic()
            try:
                result = answer_question(store(), llm, question, department, top_k)
            except ValueError as e:
                self._send(400, {"error": str(e)})
                return
            except LLMError as e:
                print(f"エラー: {e}")
                self._send(502, {"error": str(e)})
                return
            payload = result.as_dict(debug=debug)
            payload["elapsed_sec"] = round(time.monotonic() - started, 2)
            self._send(200, payload)

        def log_message(self, fmt, *args):
            # 質問の本文はログに残さない（監査ログは K-009 で別に設計する）
            print(f"{self.address_string()} {self.command} {self.path} {args[1] if len(args) > 1 else ''}")

    return Handler


def serve(
    db_path: str, llm: ChatModel, host: str = "127.0.0.1", port: int = 8000, top_k: int = 5, debug: bool = False
) -> ThreadingHTTPServer:
    return ThreadingHTTPServer((host, port), make_handler(db_path, llm, top_k, debug))
