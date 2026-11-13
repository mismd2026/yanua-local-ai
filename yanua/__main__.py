"""コマンドラインから使う。

    python -m yanua ingest demo/company/docs            # 資料を取り込む（変わった文書だけ反映）
    python -m yanua search "在宅勤務は週何日？" --department 営業部
    python -m yanua docs                                 # 取り込んだ文書の一覧
    python -m yanua ask "在宅勤務は週何日？" --department 営業部   # Ollamaで回答を作る
    python -m yanua serve --port 8000                    # HTTP API を起動する
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .answer import answer_question
from .llm import DEFAULT_MODEL, DEFAULT_OLLAMA_URL, LLMError, OllamaChat
from .search import search
from .server import serve
from .store import Store

DEFAULT_DB = Path("data/index.db")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="yanua")
    p.add_argument("--db", type=Path, default=DEFAULT_DB, help="保存先のSQLiteファイル")
    sub = p.add_subparsers(dest="command", required=True)

    s = sub.add_parser("ingest", help="資料のフォルダを取り込む")
    s.add_argument("source", type=Path)

    s = sub.add_parser("search", help="部署の権限つきで検索する")
    s.add_argument("query")
    s.add_argument("--department", required=True, help="質問する人の部署")
    s.add_argument("--top-k", type=int, default=5)

    sub.add_parser("docs", help="取り込んだ文書の一覧")

    def llm_options(sp):
        sp.add_argument("--model", default=DEFAULT_MODEL, help=f"Ollamaのモデル（既定: {DEFAULT_MODEL}）")
        sp.add_argument("--ollama", default=DEFAULT_OLLAMA_URL, help=f"OllamaのURL（既定: {DEFAULT_OLLAMA_URL}）")
        sp.add_argument("--top-k", type=int, default=5, help="LLMに渡す検索結果の件数")

    s = sub.add_parser("ask", help="Ollamaで出典つきの回答を作る")
    s.add_argument("question")
    s.add_argument("--department", required=True, help="質問する人の部署")
    llm_options(s)

    s = sub.add_parser("serve", help="HTTP API を起動する")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    llm_options(s)

    args = p.parse_args(argv)

    if args.command == "serve":
        if not Store(args.db).documents():
            print(f"{args.db} に文書がありません。先に ingest を実行してください。")
            return 1
        server = serve(str(args.db), OllamaChat(args.model, args.ollama), args.host, args.port, args.top_k)
        print(f"http://{args.host}:{args.port} で待ち受けています（モデル: {args.model}）。止めるには Ctrl+C")
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            server.server_close()
        return 0

    store = Store(args.db)
    try:
        if args.command == "ask":
            try:
                result = answer_question(store, OllamaChat(args.model, args.ollama), args.question, args.department, args.top_k)
            except LLMError as e:
                print(e)
                return 1
            print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
            return 0
        if args.command == "ingest":
            report = store.ingest(args.source)
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            return 1 if report.errors else 0
        if args.command == "search":
            hits = search(store, args.query, args.department, args.top_k)
            print(json.dumps([h.as_dict() for h in hits], ensure_ascii=False, indent=2))
            return 0
        if args.command == "docs":
            print(json.dumps(store.documents(), ensure_ascii=False, indent=2))
            return 0
    finally:
        store.close()
    return 2


if __name__ == "__main__":
    sys.exit(main())
