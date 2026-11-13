"""コマンドラインから使う。

    python -m yanua ingest demo/company/docs            # 資料を取り込む（変わった文書だけ反映）
    python -m yanua search "在宅勤務は週何日？" --department 営業部
    python -m yanua docs                                 # 取り込んだ文書の一覧
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from .search import search
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

    args = p.parse_args(argv)
    store = Store(args.db)
    try:
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
