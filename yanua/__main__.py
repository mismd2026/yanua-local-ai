"""コマンドラインから使う。

    python -m yanua ingest demo/company/docs            # 資料を取り込む（変わった文書だけ反映）
    python -m yanua search "在宅勤務は週何日？" --department 営業部
    python -m yanua docs                                 # 取り込んだ文書の一覧
    python -m yanua ask "在宅勤務は週何日？" --department 営業部   # Ollamaで回答を作る
    python -m yanua serve --dev-login                    # Web画面向けの API を起動する（開発用ログイン）

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
    python -m yanua pdf-sort demo/company/paper/inbox --rules demo/company/paper/rules.json --out 仕分け済み --dry-run
                                                           # スキャンしたPDFを、ルールに沿って名前を付けて仕分ける
    python -m yanua pdf-rule-draft 見本.pdf                 # 見本のPDFから、仕分けルールの下書きを作る
    python -m yanua pdf-convert 請求書.pdf --to xlsx        # PDFを Word（docx）/ Excel（xlsx）に変換する
    python -m yanua models pull whisper-small               # 文字起こしのモデルを入手する（許可リストの宛先からだけ）
    python -m yanua meeting 録画.mp4 --title 定例会 --tool zoom  # 会議の録画から議事録を作る
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

    s = sub.add_parser("pdf-text", help="PDFの文字を取り出す（文字のないページはOCR）")
    s.add_argument("source", type=Path)
    s.add_argument("--no-ocr", action="store_true", help="OCRをしない")

    s = sub.add_parser("pdf-rules", help="仕分けルールの一覧")
    s.add_argument("--rules", type=Path, required=True)

    s = sub.add_parser("pdf-rule-draft", help="見本のPDFから、仕分けルールの下書きを作る")
    s.add_argument("source", type=Path)
    s.add_argument("--name", help="書類の種類の名前（省略時は書類から推測）")
    s.add_argument("--add-to", type=Path, help="下書きを、このルールのファイルの最後に足す")
    s.add_argument("--use-llm", action="store_true", help="見分けに使う言葉を、LLM（社内）にも挙げさせる")
    llm_options(s)

    s = sub.add_parser("pdf-sort", help="PDFをルールに沿って名前を付け、保存先へ仕分ける")
    s.add_argument("source", type=Path, help="PDFのファイル、またはPDFの入ったフォルダ")
    s.add_argument("--rules", type=Path, required=True)
    s.add_argument("--out", type=Path, required=True, help="保存先のフォルダ（この中に、ルールのフォルダを作る）")
    s.add_argument("--rule", help="ルールを指定する（すべてのPDFにこのルールを使う）")
    s.add_argument("--dry-run", action="store_true", help="置かずに、予定だけを表示する")
    s.add_argument("--move", action="store_true", help="元のファイルを移す（既定はコピー）")
    s.add_argument("--text", action="store_true", help="取り出した文字を、同じ名前の .txt でも残す")
    s.add_argument("--form", action="store_true", help="ルールに様式があれば、様式に差し込んだファイルも置く")
    s.add_argument("--no-ocr", action="store_true", help="OCRをしない")
    s.add_argument("--use-llm", action="store_true", help="どのルールにも当たらないとき、LLM（社内）にルールを選ばせる")
    llm_options(s)

    s = sub.add_parser("pdf-convert", help="PDFを Word（docx）/ Excel（xlsx）に変換する")
    s.add_argument("source", type=Path)
    s.add_argument("--to", choices=["docx", "xlsx"], required=True)
    s.add_argument("--out", type=Path, help="出力先（省略時は元のファイルと同じ場所・同じ名前）")
    s.add_argument("--rules", type=Path, help="仕分けルール。当てはまるルールに様式があれば、様式に差し込む")
    s.add_argument("--rule", help="ルールを指定する（--rules と一緒に）")

    s = sub.add_parser("models", help="モデルの入手（pull）・照合（verify）・一覧（list）")
    s.add_argument("action", choices=["pull", "verify", "list"])
    s.add_argument("name", nargs="?", default="whisper-small")
    s.add_argument("--update", action="store_true", help="記録した版ではなく、いまの版を入手する")
    s.add_argument("--models-dir", type=Path, default=Path("data/models"))

    s = sub.add_parser("meeting", help="会議の録画・録音から議事録を作る（文字起こし・話し手・要約）")
    s.add_argument("media", type=Path, help="録画（mp4 など）か録音（wav / m4a / mp3 など）")
    s.add_argument("--title", required=True, help="会議名")
    s.add_argument("--date", help="開催日（YYYY-MM-DD。省略時は今日）")
    s.add_argument("--place", default="", help="場所（任意）")
    s.add_argument("--tool", choices=["zoom", "teams", "meet", "none"], default="zoom",
                   help="録画した会議ツール（画面の名前から話し手を読む）。none なら話し手を読まない")
    s.add_argument("--participants", default="", help="出席者（カンマ区切り。会議室の名前なども）。話し手の表記合わせに使う")
    s.add_argument("--glossary", type=Path, help="用語集（1行に1語）。文字起こしのヒントにする")
    s.add_argument("--segments", type=Path, help="文字起こし済みの発言（JSON）。あれば文字起こしをしない")
    s.add_argument("--model-name", default="whisper-small", help="文字起こしのモデル（models pull で入手したもの）")
    s.add_argument("--models-dir", type=Path, default=Path("data/models"))
    s.add_argument("--template", type=Path, help="議事録の様式（Word）。省略時は Markdown だけ")
    s.add_argument("--out", type=Path, default=Path("data/minutes"), help="議事録の出力先")
    s.add_argument("--no-summary", action="store_true", help="要約しない（LLMを使わない）")
    s.add_argument("--debug-frames", type=Path, help="話し手の読み取りに使った画像を保存する（位置や色の調整用）")
    llm_options(s)

    s = sub.add_parser("serve", help="Web画面・クライアントアプリ向けの API を起動する（docs/screens.md）")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--dev-login", action="store_true",
                   help="開発用ログイン（SSOなしで社員番号を選ぶ）。127.0.0.1 で待ち受けるときだけ使える")
    s.add_argument("--chat-db", type=Path, default=Path("data/chat.db"), help="会話の途中の状態の保存先")
    s.add_argument("--roles", type=Path, default=Path("config/roles.json"), help="役割の設定")
    s.add_argument("--helpdesk-data", type=Path, default=DEFAULT_HELPDESK_DATA)
    llm_options(s)

    s = sub.add_parser("serve-legacy", help="前の HTTP API（POST /ask と GET /health だけ）。評価用に残している")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8000)
    s.add_argument("--debug", action="store_true", help="LLMが実際に返した文（model_text）も応答に含める。評価・調査用")
    llm_options(s)

    args = p.parse_args(argv)

    if args.command.startswith("pdf-"):
        return _paper(args)
    if args.command == "models":
        return _models(args)

    if args.command == "serve":
        return _serve(args)

    if args.command == "serve-legacy":
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
        if args.command == "meeting":
            return _meeting(args, store)
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



def _paper(args) -> int:
    from .paper import convert, rules, sort
    from .paper.extract import OCRUnavailable, read_pdf

    def llm_or_none():
        if not getattr(args, "use_llm", False):
            return None
        llm = OllamaChat(args.model, args.ollama)
        llm.check()
        return llm

    try:
        if args.command == "pdf-text":
            doc = read_pdf(args.source, ocr=not args.no_ocr)
            for page in doc.pages:
                print(f"--- {page.number}ページ{'（OCR）' if page.ocr else ''} ---\n{page.text}")
            return 0
        if args.command == "pdf-rules":
            rs = rules.load_rules(args.rules)
            for r in rs.rules:
                print(f"{r.name}：言葉 {'・'.join(r.any + r.all) or '（なし）'} → {r.folder}/{r.filename}")
            print(f"（どれにも当たらない → {rs.unmatched_folder}/{rs.unmatched_filename}）")
            return 0
        if args.command == "pdf-rule-draft":
            doc = read_pdf(args.source)
            draft = rules.draft_rule(doc.text, args.name, llm_or_none())
            print(json.dumps(draft, ensure_ascii=False, indent=2))
            if args.add_to:
                rs = rules.load_rules(args.add_to)
                if draft["name"] in rs.names():
                    print(f"「{draft['name']}」は、もう {args.add_to} にあります。足しませんでした。")
                    return 1
                data = json.loads(args.add_to.read_text(encoding="utf-8"))
                data.setdefault("rules", []).append(draft)
                args.add_to.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
                rules.load_rules(args.add_to)   # 足したあとも読めることを確かめる
                print(f"{args.add_to} に足しました。言葉・ファイル名・保存先を確かめて、必要なら直してください。")
            else:
                print("\n※ 下書きです。確かめてから、ルールのファイルの rules に足してください（--add-to でも足せます）。")
            return 0
        if args.command == "pdf-sort":
            rs = rules.load_rules(args.rules)
            src = args.source
            pdfs = sorted(src.glob("*.pdf")) + sorted(src.glob("*.PDF")) if src.is_dir() else [src]
            if not pdfs:
                print(f"{src} にPDFがありません。")
                return 1
            plans = sort.plan(pdfs, rs, llm_or_none(), args.rule, ocr=not args.no_ocr)
            for pl in plans:
                how = {"rule": "", "llm": "（LLMが選択）", "forced": "（指定）", "none": ""}[pl.by]
                ocr = "［OCR］" if pl.ocr else ""
                print(f"{pl.source.name}{ocr} → " + (f"読めません: {pl.error}" if pl.error else
                                                      f"{pl.rule or '未分類'}{how}：{pl.dest}"))
            if args.dry_run:
                print("\n（--dry-run のため、置いていません）")
                return 0
            placed = sort.execute(plans, args.out, move=args.move, text=args.text, form=args.form, rules=rs)
            print(f"\n{len(placed)} 件を {args.out} に{'移しました' if args.move else 'コピーしました'}。"
                  f"記録：{args.out / sort.LOG_NAME}")
            return 1 if any(pl.error for pl in plans) else 0
        if args.command == "pdf-convert":
            out = args.out or args.source.with_suffix("." + args.to)
            if out.resolve() == args.source.resolve():
                print("出力先が元のファイルと同じです。")
                return 1
            if args.rules:
                from .paper import form

                rs = rules.load_rules(args.rules)
                doc = read_pdf(args.source, tables=True)
                m = rules.choose(rs, doc.text, forced=args.rule)
                template = m.rule.templates.get(args.to) if m.rule else None
                if template:
                    done = form.fill(template, out, doc, rules.find_fields(doc.text, m.rule, rs.self_names))
                    print(f"「{m.rule.name}」の様式（{template.name}）に差し込みました：{done}")
                    print("読み取った内容は、元の書類と照らして確認してください。")
                    return 0
                print(("どのルールにも当たらないため" if not m.rule else f"「{m.rule.name}」に {args.to} の様式がないため")
                      + "、様式なしで変換します。")
                (convert.to_docx if args.to == "docx" else convert.to_xlsx)(args.source, out, doc)
                print(f"{out} に変換しました。")
                return 0
            (convert.to_docx if args.to == "docx" else convert.to_xlsx)(args.source, out)
            print(f"{out} に変換しました。")
            return 0
    except (rules.RuleError, OCRUnavailable, LLMError, FileNotFoundError, ValueError) as e:
        print(e)
        return 1
    return 1



def _models(args) -> int:
    from . import models
    from .netpolicy import NetworkBlocked, load_policy

    policy = load_policy()
    try:
        if args.action == "list":
            lock = models.read_lock(args.models_dir)
            for name, spec in policy.models.items():
                got = lock.get(name)
                print(f"{name}：{spec['repo']}　" + (f"入手済み（版 {got['revision'][:12]}、{got['pulled_at']}）" if got else "未入手"))
            print(f"通信してよい宛先：{'、'.join(policy.allowed_hosts)}（config/network.json）")
            return 0
        if args.action == "pull":
            entry = models.pull(args.name, policy, args.models_dir, update=args.update)
            print(f"{args.name} を入手しました（版 {entry['revision'][:12]}、ファイル {len(entry['files'])} 件を記録）。")
            return 0
        path = models.verify(args.name, args.models_dir)
        print(f"{args.name}：記録と一致しました（{path}）。")
        return 0
    except (models.ModelError, NetworkBlocked) as e:
        print(e)
        return 1


def _meeting(args, store: Store) -> int:
    from datetime import date

    from . import models
    from .meeting import run as meeting_run
    from .meeting.transcribe import WhisperTranscriber, read_glossary

    if not args.media.exists():
        print(f"{args.media} がありません。")
        return 1
    names, depts = meeting_run.company_words(store.db)
    participants = [p.strip() for p in args.participants.split(",") if p.strip()]
    transcriber, segments = None, None
    try:
        if args.segments:
            segments = meeting_run.load_segments(args.segments)
        else:
            transcriber = WhisperTranscriber(models.verify(args.model_name, args.models_dir))
        llm = None
        if not args.no_summary:
            llm = OllamaChat(args.model, args.ollama)
            llm.check()
    except (models.ModelError, LLMError) as e:
        print(e)
        return 1
    print("文字起こし・話し手の読み取り・要約をしています（長い会議は時間がかかります）…")
    result = meeting_run.run(
        args.media, title=args.title, out_dir=args.out, llm=llm, transcriber=transcriber, segments=segments,
        tool=args.tool, names=names, depts=depts, participants=participants, glossary=read_glossary(args.glossary),
        template=args.template, meeting_date=date.fromisoformat(args.date) if args.date else None, place=args.place,
        debug=args.debug_frames)
    info = result.info
    if "elapsed" in info:
        print(f"文字起こし：{info['duration']}秒の録音を {info['elapsed']}秒で処理しました。")
    if "speakers_found" in info:
        print(f"話し手：{len(result.segments)} 件の発言のうち {info['speakers_found']} 件で、画面から名前を読み取りました。")
    for f in result.files:
        print(f"→ {f}")
    return 0


def _serve(args) -> int:
    from .api import Settings, create_app
    from .api.roles import RoleConfigError, load_roles

    if not Store(args.db).documents():
        print(f"{args.db} に文書がありません。先に ingest を実行してください。")
        return 1
    settings = Settings(db_path=args.db, chat_db_path=args.chat_db, helpdesk_data=args.helpdesk_data,
                        roles_path=args.roles, top_k=args.top_k, host=args.host, dev_login=args.dev_login)
    try:
        settings.check()
        load_roles(settings.roles_path)
    except (ValueError, RoleConfigError) as e:
        print(e)
        return 1
    llm = OllamaChat(args.model, args.ollama)
    try:
        llm.check()
    except LLMError as e:
        print(e)
        return 1
    import uvicorn

    app = create_app(settings, llm)
    print(f"http://{args.host}:{args.port} で待ち受けます（モデル: {args.model}）。止めるには Ctrl+C")
    if args.dev_login:
        print(f"開発用ログインが有効です。API の一覧: http://{args.host}:{args.port}/api/docs")
    uvicorn.run(app, host=args.host, port=args.port, log_level="info")
    return 0


if __name__ == "__main__":
    sys.exit(main())
