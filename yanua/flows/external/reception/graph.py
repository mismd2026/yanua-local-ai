"""社外向けの受付（LangGraph）。電話・Webの問い合わせと、来訪の受付を1つの流れで扱う。

流れ:
    （来訪）インビテーションコード → 当日の予定と合えば、予定を表示・入館票・担当者へお知らせ → 記録
    用件を聞く → 種類を決める ┬ 問い合わせ → 社外公開の文書で答える → 解決？ ─ いいえ → 連絡先 → 記録
                               ├ 取次（社員への用事） → 会社名・お名前 → 訪ね先 → 来訪予定と照合 → 記録
                               ├ クレーム → 決まったおわび → 内容 → 連絡先 → 記録（優先度：高）
                               ├ 営業 → 会社名・お名前 → 決まった返事 → 記録
                               └ 業者（納品・工事など） → 会社名・お名前・作業 → 記録

社外の人と話すので、社内向けより守りを固くしている:
- 答えに使うのは社外公開（access: 社外公開）の文書だけ。「全社」（全従業員向け）の文書は検索の候補にも入らない
- 社員は人事データから探すが、所属・連絡先・在籍しているかどうかは、お客様への返事に一切出さない
- クレームと営業には、LLMに答えを作らせない（約束や謝罪の内容を、決まった文だけにするため）
- 種類はまず決まった言葉で決め、当たらないときだけLLMに選ばせる。選択肢以外の返事は「問い合わせ」に倒す
- インビテーションコードは、違う日・間違い・取り消しを区別せず同じ返事にする。3回まで受け付け、あとは用件を聞く
"""

from __future__ import annotations

import csv
import json
import re
from datetime import date, datetime
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .... import hr, visits
from ....answer import answer_question
from ....llm import ChatModel
from ....store import Store
from ...base import ask_json, is_yes, norm

CHANNELS = ["来訪", "電話", "Web"]
MAX_CODE_TRIES = 3
# 「ABC商事の木村です」のような名乗り。会社名と名前が取れたら、聞き直さずに確認だけする
SELF_INTRO = re.compile(r"^(?P<company>[^。、,\s]{2,30}?)の(?P<name>[^。、,\s]{1,12}?)(?:です|と申します|でございます)")
CATEGORIES = ["問い合わせ", "取次", "クレーム", "営業", "業者"]

# 種類を決める言葉。上から順に見る（クレームを最優先：ほかの言葉が混ざっていても、おわびから入る）
RULES: list[tuple[str, list[str]]] = [
    ("クレーム", ["苦情", "クレーム", "届かない", "届いていない", "壊れて", "不良", "返金", "対応が悪", "ひどい",
                  "納得できない", "責任者", "謝罪", "困っている", "いい加減"]),
    ("業者", ["納品", "配達", "宅配", "お届け", "工事", "点検", "修理に", "設置に", "搬入", "回収に"]),
    ("営業", ["ご提案", "提案に", "営業で", "営業に", "ご紹介させて", "新サービス", "新しいサービス", "飛び込み", "ご案内に"]),
    ("取次", ["お約束", "約束", "アポ", "面談", "打ち合わせ", "打合せ", "面接", "さんをお願い", "様をお願い",
              "さんはいらっしゃ", "さんはいますか", "に会いに", "お伺いしました", "伺いました", "参りました", "につないで"]),
]

TABLE_SCHEMA = """
CREATE TABLE IF NOT EXISTS reception_records (
    record_id  TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    channel    TEXT NOT NULL,
    category   TEXT NOT NULL,
    route      TEXT NOT NULL,
    priority   TEXT NOT NULL,
    payload    TEXT NOT NULL
);
"""


class State(TypedDict, total=False):
    channel: str
    first: str
    code_visit: dict | None
    pass_file: str
    notified: bool
    purpose: str
    category: str
    visitor: dict
    host: dict | None
    host_said: str
    visit: dict | None
    details: str
    contact: str
    answer: dict
    resolved: bool
    reply: str
    route: str
    record: dict
    result: str


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def classify_by_rule(text: str) -> str | None:
    t = norm(text)
    for category, words in RULES:
        if any(w in t for w in words):
            return category
    return None


def as_company_voice(text: str) -> str:
    """お客様の言葉をそのまま返すと「御社の営業時間は」のようになるので、会社側の言い方に直す。"""
    return text.replace("御社", "当社").replace("貴社", "当社")


def build_app(store: Store, llm: ChatModel, data_dir: Path, today: date | None = None, top_k: int = 5,
              passes_dir: Path | None = None):
    """受付のグラフを作る。store は社外公開の文書・人事データ・来訪予定を持つ保存先。

    passes_dir を渡すと、インビテーションコードで受け付けたときに印刷用の入館票（HTML）をそこに書き出す。
    """
    routes = {r["category"]: r for r in _read_csv(data_dir / "routes.csv")}
    store.db.executescript(TABLE_SCHEMA)
    visits.ensure_schema(store.db)

    def day() -> date:
        return today or date.today()

    def phone_like(s: State) -> bool:
        return s["channel"] != "来訪"

    # ------------------------------------------------------------ ノード

    def intake(s: State) -> State:
        if s["channel"] == "来訪":
            return {"first": interrupt("いらっしゃいませ。インビテーションコードをお持ちの方は、コードをご入力ください。"
                                       "お持ちでない方は、ご用件をお聞かせください。")}
        return {"first": interrupt("お問い合わせありがとうございます。ご用件をお聞かせください。")}

    def code_check(s: State) -> State:
        # コードらしい入力だけを照合する。コードでない入力は、そのままご用件として扱う
        text = s["first"]
        if s["channel"] != "来訪" or not visits.looks_like_code(text):
            return {"purpose": text}
        for tries in range(1, MAX_CODE_TRIES + 1):
            visit = visits.find_by_code(store.db, text, day())
            if visit:
                return {"code_visit": visit}
            prompt = ("恐れ入りますが、コードを確認できませんでした。もう一度ご入力いただくか、ご用件をお聞かせください。"
                      if tries < MAX_CODE_TRIES else "恐れ入りますが、コードを確認できませんでした。ご用件をお聞かせください。")
            text = interrupt(prompt)
            if tries == MAX_CODE_TRIES or not visits.looks_like_code(text):
                return {"purpose": text}
        return {"purpose": text}

    def welcome(s: State) -> State:
        visit = visits.check_in(store.db, s["code_visit"])
        host = hr.lookup(store.db, visit["host_employee_no"])
        host_name = host["name"] if host else "担当の者"
        out: State = {"category": "来訪", "purpose": "インビテーションコードで受付", "notified": True,
                      "visitor": {"company": visit["visitor_company"], "name": visit["visitor_name"]},
                      "host": host, "visit": visit, "route": host["dept"] if host else "総務部"}
        if passes_dir:
            passes_dir.mkdir(parents=True, exist_ok=True)
            path = passes_dir / f"pass-{visit['visit_id']}.html"
            path.write_text(visits.pass_html(visit, host_name), encoding="utf-8")
            out["pass_file"] = str(path)
        out["reply"] = (
            f"{visit['visitor_company']} {visit['visitor_name']}様、お待ちしておりました。\n"
            f"日時：{visit['date']} {visit['time']}\n会議室：{visit['room']}\n担当：{host_name}\n"
            f"連絡先：{visit['host_contact'] or '受付にお声がけください'}\n\n"
            "担当の者に到着をお知らせしました。入館票をお受け取りのうえ、会議室へお進みください。"
        )
        return out

    def classify(s: State) -> State:
        category = classify_by_rule(s["purpose"])
        if category is None:
            data = ask_json(llm, (
                "受付に来た用件を、次のどれか1つに分けてJSONで返してください。\n"
                f'形式: {{"category": {"・".join(CATEGORIES)} のどれか}}\n'
                "問い合わせ：会社や製品についての質問。取次：特定の社員への用事。クレーム：苦情や不満。"
                "営業：売り込みや提案。業者：納品・工事・点検。\n\n"
                f"用件: {s['purpose']}"
            ))
            category = data.get("category") if data.get("category") in CATEGORIES else "問い合わせ"
        return {"category": category}

    def ask_visitor(s: State) -> State:
        m = SELF_INTRO.match(norm(s["purpose"]))
        if m and is_yes(interrupt(f"{m['company']}の{m['name']}様でいらっしゃいますね。（はい／いいえ）")):
            return {"visitor": {"company": m["company"], "name": m["name"]}}
        company = interrupt("恐れ入りますが、会社名をお聞かせください。")
        name = interrupt("お名前をお聞かせください。")
        return {"visitor": {"company": company.strip(), "name": name.strip()}}

    def find_host(s: State) -> State:
        # 用件の中に社員の名前があればそれを使い、なければ聞く。同じ姓が複数いれば、フルネームを聞き直す
        found = hr.find_in_text(store.db, s["purpose"])
        said = ""
        if len(found) != 1:
            said = interrupt("どの者をお訪ねでしょうか。担当者のお名前（できればフルネーム）をお聞かせください。")
            found = hr.find_in_text(store.db, said)
        host = found[0] if len(found) == 1 else None

        # コードを持っていない来訪者も、当日の予定と会社名で照合する
        visit = None
        if s["channel"] == "来訪":
            company = norm(s["visitor"]["company"])
            for v in visits.for_date(store.db, day()):
                same_company = company and (norm(v["visitor_company"]) in company or company in norm(v["visitor_company"]))
                same_host = host and v["host_employee_no"] == host["employee_no"]
                if same_company and (same_host or host is None):
                    visit = visits.check_in(store.db, v)
                    host = host or hr.lookup(store.db, v["host_employee_no"])
                    break
        return {"host": host, "host_said": said, "visit": visit, "notified": visit is not None}

    def relay(s: State) -> State:
        # お客様への返事には、社員の所属・連絡先・在籍しているかどうかを入れない（見つからなくても同じ返事にする）
        name = s["visitor"]["name"]
        if s.get("visit"):
            return {"reply": f"{name}様、お待ちしておりました。担当の者に到着をお知らせしました。"
                             f"{s['visit']['room']}へご案内しますので、少々お待ちください。",
                    "route": s["host"]["dept"] if s.get("host") else "総務部"}
        route = s["host"]["dept"] if s.get("host") else "総務部"   # 見つからなければ総務部が確認する
        if phone_like(s):
            return {"route": route, "contact": interrupt("担当の者に申し伝えます。折り返しのご連絡先（お電話番号など）をお聞かせください。"),
                    "reply": "承知しました。担当の者から折り返しご連絡いたします。"}
        return {"route": route, "reply": "担当の者に確認いたしますので、少々お待ちください。"}

    def answer(s: State) -> State:
        ans = answer_question(store, llm, s["purpose"], "", top_k, public=True)
        if ans.refused:
            return {"answer": ans.as_dict(), "resolved": False}
        return {"answer": {**ans.as_dict(), "answer": as_company_voice(ans.answer)}}

    def confirm(s: State) -> State:
        # 回答（LLMの呼び出し）と返事待ちを別のノードにする。再開したときにLLMがもう一度呼ばれないようにするため
        return {"resolved": is_yes(interrupt(f"{s['answer']['answer']}\n\nこちらでお役に立てましたか？（はい／いいえ）"))}

    def take_contact(s: State) -> State:
        refused = s.get("answer", {}).get("refused")
        lead = s["answer"]["answer"] if refused else "承知しました。"
        contact = interrupt(f"{lead}\n\nお名前と、ご連絡先（お電話番号かメールアドレス）をお聞かせください。")
        # 答えられなかったときは、上の文で「改めてご連絡」と伝え済みなので、同じ文を繰り返さない
        reply = "承りました。お問い合わせの内容を、担当の部署にお伝えします。" if refused else routes["問い合わせ"]["reply"]
        return {"contact": contact, "route": routes["問い合わせ"]["route"], "reply": reply}

    def claim(s: State) -> State:
        # おわびと聞き取りは決まった文だけ。LLMに答えを作らせない（できない約束をしないため）
        details = interrupt("ご不便をおかけし、誠に申し訳ございません。"
                            "差し支えなければ、いつ・どの製品やサービスで・どのようなことがあったかをお聞かせください。")
        contact = interrupt("お名前と、ご連絡先（お電話番号かメールアドレス）をお聞かせください。")
        return {"details": details, "contact": contact,
                "route": routes["クレーム"]["route"], "reply": routes["クレーム"]["reply"]}

    def sales(s: State) -> State:
        return {"route": routes["営業"]["route"], "reply": routes["営業"]["reply"]}

    def vendor(s: State) -> State:
        details = interrupt("作業の内容（納品・工事・点検など）をお聞かせください。")
        return {"details": details, "route": routes["業者"]["route"], "reply": routes["業者"]["reply"]}

    def record(s: State) -> State:
        now = datetime.now().astimezone()
        n = store.db.execute("SELECT COUNT(*) FROM reception_records").fetchone()[0] + 1
        rid = f"RC-{now:%Y%m%d}-{n:04d}"
        category = s["category"]
        host = s.get("host")
        r = {
            "record_id": rid,
            "created_at": now.isoformat(timespec="seconds"),
            "channel": s["channel"],
            "category": category,
            "purpose": s["purpose"],
            "visitor": s.get("visitor", {}),
            "contact": s.get("contact", ""),
            "details": s.get("details", ""),
            # 社内向けの項目：訪ね先は社員番号と名前だけ残す（お客様への返事には出していない）
            "host": {"employee_no": host["employee_no"], "name": host["name"]} if host else None,
            "host_said": s.get("host_said", ""),
            "appointment": s["visit"] if s.get("visit") else None,
            "answer": s.get("answer"),
            "resolved": bool(s.get("resolved")),
            "route": s.get("route") or routes.get(category, routes["問い合わせ"])["route"],
            "priority": "低" if s.get("resolved") else routes.get(category, routes["問い合わせ"])["priority"],
            # 外部のAIに問い合わせるときに除く項目（個人を特定できる情報）
            "exclude_when_external": ["visitor", "contact", "host"],
        }
        with store.db:
            store.db.execute("INSERT INTO reception_records VALUES (?, ?, ?, ?, ?, ?, ?)",
                             (rid, r["created_at"], r["channel"], category, r["route"], r["priority"],
                              json.dumps(r, ensure_ascii=False)))
            # 担当者（わかれば本人、わからなければ部署）へのお知らせ。解決済みと、到着を知らせ済みのものは除く
            if not s.get("resolved") and not s.get("notified"):
                who = f"{r['visitor'].get('company', '')} {r['visitor'].get('name', '')}".strip() or "お客様"
                visits.notify(store.db, host["employee_no"] if host else "", r["route"], category,
                              f"【{category}】{who}：{s['purpose']}", ref=rid, commit=False)
        return {"record": r, "result": s["reply"]}

    def done(s: State) -> State:
        # 解決した問い合わせも、よく聞かれる内容を集計できるように記録する（お知らせは出さない）
        return {"route": "（解決）", "reply": "ありがとうございました。またのお問い合わせをお待ちしております。"}

    # ------------------------------------------------------------ 分岐

    def after_code(s: State) -> str:
        return "welcome" if s.get("code_visit") else "classify"

    def after_classify(s: State) -> str:
        if s["category"] in ("取次", "営業", "業者"):
            return "ask_visitor"
        return "claim" if s["category"] == "クレーム" else "answer"

    def after_visitor(s: State) -> str:
        return {"取次": "find_host", "営業": "sales", "業者": "vendor"}[s["category"]]

    def after_answer(s: State) -> str:
        return "take_contact" if s.get("resolved") is False else "confirm"

    def after_confirm(s: State) -> str:
        return "done" if s["resolved"] else "take_contact"

    g = StateGraph(State)
    for name, fn in [("intake", intake), ("code_check", code_check), ("welcome", welcome), ("classify", classify), ("ask_visitor", ask_visitor),
                     ("find_host", find_host), ("relay", relay), ("answer", answer), ("confirm", confirm),
                     ("take_contact", take_contact), ("claim", claim), ("sales", sales), ("vendor", vendor),
                     ("record", record), ("done", done)]:
        g.add_node(name, fn)
    g.add_edge(START, "intake")
    g.add_edge("intake", "code_check")
    g.add_conditional_edges("code_check", after_code, ["welcome", "classify"])
    g.add_conditional_edges("classify", after_classify, ["ask_visitor", "claim", "answer"])
    g.add_conditional_edges("ask_visitor", after_visitor, ["find_host", "sales", "vendor"])
    g.add_edge("find_host", "relay")
    g.add_conditional_edges("answer", after_answer, ["take_contact", "confirm"])
    g.add_conditional_edges("confirm", after_confirm, ["done", "take_contact"])
    for name in ("welcome", "relay", "take_contact", "claim", "sales", "vendor", "done"):
        g.add_edge(name, "record")
    g.add_edge("record", END)
    return g.compile(checkpointer=InMemorySaver())
