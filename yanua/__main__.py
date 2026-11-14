"""コマンドラインから使う。

    python -m yanua ingest demo/company/docs            # 資料を取り込む（変わった文書だけ反映）
    python -m yanua search "在宅勤務は週何日？" --department 営業部
    python -m yanua docs                                 # 取り込んだ文書の一覧
    python -m yanua ask "在宅勤務は週何日？" --department 営業部   # Ollamaで回答を作る
    python -m yanua serve --port 8000                    # HTTP API を起動する

    python -m yanua hr-mapping-draft 人事出力.csv          # 人事データの対応表の下書きを作る
    python -m yanua hr-import 人事出力.csv --mapping 対応表.json   # 人事データを取り込む
    python -m yanua helpdesk --account yamada.t            # 社内向けヘルプデスク（対話）
    python -m yanua helpdesk --demo 1                      # ヘルプデスクのデモ
    python -m yanua ingest demo/company/public             # 社外公開の文書を取り込む（受付が使う）
    python -m yanua reception --channel 電話               # 社外向けの受付（対話）
    python -m yanua reception --demo 1                     # 受付のデモ
    python -m yanua visit-create --host 10004 --date 2026-11-20 --time 14:00 --room "5階 第2会議室" \
        --company ABC商事 --visitor "木村 誠"               # 来訪予定を登録し、インビテーションコードを作る
    python -m yanua visit-list --date 2026-11-20           # 来訪予定の一覧
    python -m yanua notifications --employee 10004         # 担当者へのお知らせ
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
DEFAULT_HELPDESK_DATA = Path("demo/company/flows/helpdesk")
DEFAULT_RECEPTION_DATA = Path("demo/company/flows/reception")
DEFAULT_PASSES = Path("data/passes")
HR_STALE_DAYS = 30   # 人事データがこれより古ければ警告する


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

    s = sub.add_parser("hr-mapping-draft", help="人事データの列名から対応表の下書きを作る（中身は読まない）")
    s.add_argument("source", type=Path)
    s.add_argument("--sheet", help="Excelのシート名（省略時は最初のシート）")
    s.add_argument("--use-llm", action="store_true", help="決まったルールで当たらない列だけ、Ollamaに候補を聞く")
    llm_options(s)

    s = sub.add_parser("hr-import", help="人事システムの出力（CSV / Excel / JSON）を取り込む")
    s.add_argument("source", type=Path)
    s.add_argument("--mapping", type=Path, required=True, help="列名の対応表（JSON）")

    s = sub.add_parser("helpdesk", help="社内向けヘルプデスク（会話）")
    s.add_argument("--account", default="", help="ログイン中のアカウント（SSOの代わり）")
    s.add_argument("--demo", choices=["1", "2", "3", "4"], help="用意したデモを流す")
    s.add_argument("--data", type=Path, default=DEFAULT_HELPDESK_DATA, help="手順・アカウント・障害情報のフォルダ")
    llm_options(s)

    s = sub.add_parser("reception", help="社外向けの受付（会話）。社外公開の文書だけを使う")
    s.add_argument("--channel", choices=["来訪", "電話", "Web"], default="電話", help="どこからの問い合わせか")
    s.add_argument("--demo", choices=["1", "2", "3", "4", "5", "6"], help="用意したデモを流す")
    s.add_argument("--today", help="来訪予定と照合する日（YYYY-MM-DD）。省略時は今日。--demo ではデモの日")
    s.add_argument("--data", type=Path, default=DEFAULT_RECEPTION_DATA, help="引き継ぎ先・来訪予定のフォルダ")
    llm_options(s)

    s = sub.add_parser("visit-create", help="来訪予定を登録し、インビテーションコードと案内文を作る")
    s.add_argument("--host", required=True, help="担当者の社員番号")
    s.add_argument("--date", required=True, help="YYYY-MM-DD")
    s.add_argument("--time", required=True, help="例: 14:00")
    s.add_argument("--room", required=True)
    s.add_argument("--company", required=True, help="訪問者の会社名")
    s.add_argument("--visitor", required=True, help="訪問者のお名前")
    s.add_argument("--purpose", default="")
    s.add_argument("--contact", default="", help="訪問者に見せてよい担当者の連絡先（例: 内線4100）")

    s = sub.add_parser("visit-import", help="来訪予定のCSVをまとめて登録する（Googleカレンダーから書き出したCSVも可）")
    s.add_argument("source", type=Path)

    s = sub.add_parser("visit-list", help="来訪予定の一覧（コードは表示しない）")
    s.add_argument("--date", help="YYYY-MM-DD（省略時は今日）")

    s = sub.add_parser("notifications", help="担当者・部署へのお知らせ（未読）")
    s.add_argument("--employee", default="", help="社員番号")
    s.add_argument("--dept", default="", help="部署（部署あてのお知らせ）")
    s.add_argument("--all", action="store_true", help="既読も表示する")
    s.add_argument("--mark-read", action="store_true", help="表示したお知らせを既読にする")

    s = sub.add_parser("serve", help="HTTP API を起動する")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--debug", action="store_true", help="LLMが実際に返した文（model_text）も応答に含める。評価・調査用")
    llm_options(s)

    args = p.parse_args(argv)

    if args.command == "serve":
        if not Store(args.db).documents():
            print(f"{args.db} に文書がありません。先に ingest を実行してください。")
            return 1
        llm = OllamaChat(args.model, args.ollama)
        try:
            llm.check()
        except LLMError as e:
            print(e)
            return 1
        server = serve(str(args.db), llm, args.host, args.port, args.top_k, args.debug)
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
        if args.command == "hr-mapping-draft":
            from .hr import read_rows, suggest_mapping

            rows = read_rows(args.source, args.sheet)
            headers = list(rows[0].keys()) if rows else []
            llm = OllamaChat(args.model, args.ollama) if args.use_llm else None
            draft = suggest_mapping(headers, llm)
            if args.sheet:
                draft["sheet"] = args.sheet
            print(json.dumps(draft, ensure_ascii=False, indent=2))
            print("\n※ 下書きです。列の対応と active_values（在籍とみなす値）を確認してから、JSONファイルとして保存してください。")
            return 0
        if args.command == "hr-import":
            from .hr import HRImportError, import_employees, load_mapping

            try:
                report = import_employees(store.db, args.source, load_mapping(args.mapping))
            except HRImportError as e:
                print(e)
                return 1
            print(json.dumps(report.as_dict(), ensure_ascii=False, indent=2))
            return 1 if report.errors else 0
        if args.command == "helpdesk":
            return _helpdesk(args, store)
        if args.command == "reception":
            return _reception(args, store)
        if args.command in ("visit-create", "visit-import", "visit-list", "notifications"):
            return _visits(args, store)
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


def _helpdesk(args, store: Store) -> int:
    from .flows.internal.helpdesk import build_app
    from .flows.internal.helpdesk.cli import demo_answers, run
    from .hr import last_import

    if not store.documents():
        print(f"{args.db} に文書がありません。先に ingest を実行してください。")
        return 1
    info = last_import(store.db)
    if not info:
        print("人事データが取り込まれていません。先に hr-import を実行してください（本人確認に使います）。")
        return 1
    if info["age_days"] > HR_STALE_DAYS:
        print(f"注意：人事データが {info['age_days']} 日前のものです。入社したばかりの人が本人確認で一致しない場合があります。")

    llm = OllamaChat(args.model, args.ollama)
    try:
        llm.check()
    except LLMError as e:
        print(e)
        return 1
    app = build_app(store, llm, args.data, args.top_k)
    if args.demo:
        account, title, answer_for = demo_answers(args.demo)
        print(f"=== デモ{args.demo}：{title}（アカウント：{account}） ===")
        run(app, account, answer_for, echo=True)
    else:
        print(f"=== ヘルプデスク（アカウント：{args.account or '未ログイン'}） ===")
        run(app, args.account, lambda q: input("利用者> "))
    return 0



def _reception(args, store: Store) -> int:
    from datetime import date

    from .flows.external.reception import build_app
    from .flows.external.reception.cli import DEMO_TODAY, demo_answers, run
    from .hr import last_import

    if not store.public_chunks():
        print(f"{args.db} に社外公開の文書がありません。先に ingest demo/company/public を実行してください。")
        return 1
    if not last_import(store.db):
        print("注意：人事データが取り込まれていません。訪ね先の社員を探せないため、取次はすべて総務部に回します。")

    llm = OllamaChat(args.model, args.ollama)
    try:
        llm.check()
    except LLMError as e:
        print(e)
        return 1
    from . import visits

    if args.demo:
        visits.import_csv(store.db, args.data / "visits.csv")   # デモの予定（何度取り込んでも重ならない）
    today = args.today or (DEMO_TODAY if args.demo else None)
    app = build_app(store, llm, args.data, date.fromisoformat(today) if today else None, args.top_k, DEFAULT_PASSES)
    if args.demo:
        channel, title, answer_for = demo_answers(args.demo)
        print(f"=== 受付デモ{args.demo}：{title}（{channel}） ===")
        run(app, channel, answer_for, echo=True)
    else:
        print(f"=== 受付（{args.channel}） ===")
        run(app, args.channel, lambda q: input("お客様> "))
    return 0



def _visits(args, store: Store) -> int:
    from datetime import date

    from . import hr, visits

    if args.command == "visit-create":
        try:
            visit, code = visits.create(store.db, host_employee_no=args.host, visit_date=args.date, time=args.time,
                                        room=args.room, visitor_company=args.company, visitor_name=args.visitor,
                                        purpose=args.purpose, host_contact=args.contact)
        except visits.VisitError as e:
            print(e)
            return 1
        host = hr.lookup(store.db, visit["host_employee_no"])
        print("■ 訪問者に送る案内文（コードは、ここでしか表示しません）\n")
        print(visits.invitation_text(visit, code, host["name"]))
        return 0
    if args.command == "visit-import" and visits.is_calendar_export(args.source):
        report = visits.import_calendar_csv(store.db, args.source)
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1 if report["errors"] else 0
    if args.command == "visit-import":
        try:
            created = visits.import_csv(store.db, args.source)
        except (visits.VisitError, KeyError) as e:
            print(f"取り込めませんでした: {e}")
            return 1
        for visit, code in created:
            print(f"{visit['date']} {visit['time']} {visit['visitor_company']} {visit['visitor_name']}様：{visits.format_code(code)}")
        print(f"{len(created)} 件を登録しました。")
        return 0
    if args.command == "visit-list":
        day = date.fromisoformat(args.date) if args.date else date.today()
        print(json.dumps(visits.for_date(store.db, day), ensure_ascii=False, indent=2))
        return 0
    items = visits.notifications(store.db, args.employee, args.dept, unread_only=not args.all)
    print(json.dumps(items, ensure_ascii=False, indent=2))
    if args.mark_read and items:
        visits.mark_read(store.db, [i["notification_id"] for i in items])
    return 0


if __name__ == "__main__":
    sys.exit(main())
