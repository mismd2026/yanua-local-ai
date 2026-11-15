"""社内向けヘルプデスクの会話の流れ。

本人確認 → お困りごと → 分類 → 状況の聞き返し（2回まで）→ 障害の確認
→ 案内（社内文書から出典つきで答える。なければ一般的な手順で補う）→ 解決の確認 → 解決しなければ問い合わせ票

- 本人確認：SSOの個人アカウントなら人事データの本人を確認するだけ。代表アカウントなどは、
  従業員番号と氏名の両方を聞き、取り込んだ人事データ（yanua.hr）と照合する。人事データの中身は画面に出さない。
- 照合できた人は、その部署の権限で社内文書を検索する。照合できない人は「全社」の文書だけを使う。
- セキュリティの問い合わせ（不審なメールなど）は、検索の出来に関係なく決まったルールで拾い、
  聞き返しより先に「ネットワークから切り離す」などの案内を出して、必ず引き継ぐ。
- 優先度は本人に聞かず、影響の範囲 × 業務への影響 から決める。

利用者の返事を待つところは LangGraph の interrupt で止め、Command(resume=返事) で再開する。
"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import TypedDict

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import interrupt

from .... import hr
from ....answer import answer_question
from ....llm import ChatModel
from ....search import bm25
from ....store import Store, StoredChunk
from ...base import ask_json, is_yes, norm

MAX_CLARIFY = 2
KB_MIN_SCORE = 8.0   # 手順の資料との一致を採用する最低のBM25スコア（正しい一致は10以上、無関係なものは5未満だった）

SLOT_LABEL = {
    "device": "対象の機器・システム（例：3階の複合機、VPN）",
    "location": "場所（例：本社3階、在宅）",
    "scope": "影響の範囲（自分だけ／部署・複数人／全社）",
    "business": "業務への影響（止まっている／一部できない／支障なし）",
}
SCOPES = ["自分だけ", "部署・複数人", "全社"]
BUSINESS = ["止まっている", "一部できない", "支障なし"]
# 「全社」と判定してよい言葉。返事にこれがなければ、LLMが「全社」と答えても「部署・複数人」に直す
WIDE_WORDS = ["全社", "会社全体", "全拠点", "全店", "すべての店", "ほかの店", "他の店", "他店", "ほかの拠点", "他の拠点", "どの拠点も"]
# 「業務が止まっている」と判定してよい言葉。小さなモデルが「支障なし」と取り違えるので、返事にあれば補正する
STOP_WORDS = ["止まっ", "仕事にならない", "業務ができない", "仕事ができない", "何もできない", "業務に支障"]
NOT_STOP_WORDS = ["止まっていない", "止まってない", "止まってはいない", "止まっていません", "止まってません"]
# セキュリティに関わる言葉：検索の出来に関係なく、必ず「不審なメール」の手順に回す
SECURITY_WORDS = ["不審", "怪しい", "ウイルス", "フィッシング", "乗っ取", "身に覚えのない", "添付を開", "添付ファイルを開", "リンクを開"]

TICKET_SCHEMA = """
CREATE TABLE IF NOT EXISTS helpdesk_tickets (
    ticket_id  TEXT PRIMARY KEY,
    created_at TEXT NOT NULL,
    category   TEXT NOT NULL,
    priority   TEXT NOT NULL,
    payload    TEXT NOT NULL
);
"""


class State(TypedDict, total=False):
    account: str
    requester: dict
    problem: str
    kb: dict | None
    slots: dict
    clarify_rounds: int
    tried: list[str]
    sources: list[dict]
    message: str
    resolved: bool
    urgent: bool
    notice: str
    outage: dict
    ticket: dict
    result: str


def _read_csv(path: Path) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return list(csv.DictReader(f))


def missing_slots(slots: dict) -> list[str]:
    return [k for k in SLOT_LABEL if slots.get(k) in (None, "", "不明")]


def calc_priority(slots: dict, urgent: bool) -> str:
    if urgent:
        return "高"
    scope, biz = slots.get("scope", "不明"), slots.get("business", "不明")
    if scope == "全社" or (scope == "部署・複数人" and biz == "止まっている"):
        return "高"
    if scope == "自分だけ" and biz == "支障なし":
        return "低"
    return "中"


def extract_slots(llm: ChatModel, problem: str, answer: str) -> dict:
    """聞き返しへの返事から票の項目を取り出す。決まった値以外は「不明」、範囲はルールで補正する。"""
    data = ask_json(llm, (
        "次の問い合わせと返事から項目を取り出し、JSONで返してください。わからない項目は「不明」にしてください。\n"
        f'形式: {{"device": 対象の機器やシステム, "location": 場所, "scope": {"・".join(SCOPES)}・不明 のどれか, '
        f'"business": {"・".join(BUSINESS)}・不明 のどれか}}\n'
        "影響の範囲：本人1人なら「自分だけ」。1つの部署・フロア・店の中で複数人なら「部署・複数人」。"
        "複数の拠点や会社全体に及ぶときだけ「全社」。\n\n"
        f"問い合わせ: {problem}\n返事: {answer}"
    ))
    out = {k: str(data.get(k, "不明")).strip() or "不明" for k in SLOT_LABEL}
    if out["scope"] not in SCOPES:
        out["scope"] = "不明"
    if out["business"] not in BUSINESS:
        out["business"] = "不明"
    # 小さなモデルは「全員」を「全社」と取りがちなので、決まったルールで補正する
    if out["scope"] == "全社" and not any(w in answer for w in WIDE_WORDS):
        out["scope"] = "部署・複数人"
    if (out["business"] != "止まっている" and any(w in answer for w in STOP_WORDS)
            and not any(w in answer for w in NOT_STOP_WORDS)):
        out["business"] = "止まっている"
    return out


def build_app(store: Store, llm: ChatModel, data_dir: Path, top_k: int = 5, checkpointer=None):
    """ヘルプデスクのグラフを作る。store は取り込み済みの社内文書と人事データを持つ保存先。

    checkpointer は会話の途中の状態の保存先。省略時はメモリ（CLI 用）。API では SQLite に保存する。
    """
    kb_rows = _read_csv(data_dir / "kb.csv")
    accounts = {r["account"]: r for r in _read_csv(data_dir / "accounts.csv")}
    outages = [r for r in _read_csv(data_dir / "outages.csv") if r["active"] == "yes"]
    kb_chunks = [StoredChunk(i, str(i), r["category"], "", r["question"]) for i, r in enumerate(kb_rows)]
    store.db.executescript(TICKET_SCHEMA)
    hr.ensure_schema(store.db)

    # ------------------------------------------------------------ ノード

    def identify(s: State) -> State:
        acc = accounts.get(s.get("account", ""))
        acc_type = acc["type"] if acc else "不明"
        base = {"account": s.get("account", ""), "account_type": acc_type}

        if acc and acc_type == "personal":
            p = hr.lookup(store.db, acc["employee_no"])
            if p and is_yes(interrupt(f"{p['dept']}の{p['name']}さんでよろしいですか？（はい／いいえ）")):
                return {"requester": {**p, **base, "verified": "SSO"}}
            base["account_type"] = "personal（本人以外が使用）"

        no = interrupt("従業員番号を教えてください。")
        name = interrupt("お名前（フルネーム）を教えてください。")
        p = hr.verify(store.db, no, name)   # 番号と氏名の両方が在籍中の人と一致したときだけ
        if p:
            return {"requester": {**p, **base, "verified": "人事データと一致"}}
        dept = interrupt("所属部署を教えてください。")
        return {"requester": {"employee_no": norm(no), "name": name, "dept": dept, "dept_code": "",
                              **base, "verified": "照合できず"}}

    def intake(s: State) -> State:
        problem = interrupt(f"{s['requester']['name']}さん、ありがとうございます。どのようなことでお困りですか？")
        return {"problem": problem, "slots": {}, "clarify_rounds": 0, "tried": [], "sources": []}

    def classify(s: State) -> State:
        if any(w in s["problem"] for w in SECURITY_WORDS):
            row = next(r for r in kb_rows if "不審" in r["question"])
            return {"kb": row, "urgent": True}
        hits = bm25(s["problem"], kb_chunks, 1)
        row = kb_rows[int(hits[0].doc_id)] if hits and hits[0].score >= KB_MIN_SCORE else None
        return {"kb": row}

    def clarify(s: State) -> State:
        rounds = s.get("clarify_rounds", 0)
        slots = dict(s.get("slots") or {})
        if rounds == 0 and s.get("kb"):
            q = (f"状況を確認させてください。{s['kb']['clarify']}。"
                 "あわせて、周りの方も同じ状況か、業務が止まっているかも教えてください。")
        else:
            q = "あと少し教えてください。\n" + "\n".join(f"・{SLOT_LABEL[k]}" for k in missing_slots(slots))
        answer = interrupt(q)
        for k, v in extract_slots(llm, s["problem"], answer).items():
            if v and v != "不明":
                slots[k] = v
        return {"slots": slots, "clarify_rounds": rounds + 1}

    def outage_check(s: State) -> State:
        if not s.get("kb"):
            return {}
        location = s["slots"].get("location", "")
        for o in outages:
            if o["category"] == s["kb"]["category"] and (o["location"] == "全社" or o["location"] in location):
                return {"outage": o, "result": f"現在、{o['message']} 復旧までお待ちください。個別の対応は不要です。"}
        return {}

    def guide(s: State) -> State:
        row = s.get("kb")
        tried = list(s.get("tried", []))
        if s.get("urgent"):
            # 緊急時は社内文書の検索を待たず、決まった手順を先に出して必ず引き継ぐ
            tried.append(f"案内した手順：{row['question']}")
            return {"tried": tried, "notice": row["answer"]}

        # 1. 社内文書から出典つきで答える（照合できた人は部署の権限で、できない人は全社の文書だけ）
        dept = s["requester"]["dept"] if s["requester"]["verified"] != "照合できず" else ""
        ans = answer_question(store, llm, s["problem"], dept, top_k)
        if not ans.refused:
            titles = "、".join(src["title"] for src in ans.sources)
            message = f"{ans.answer}\n（出典：{titles}）"
            if row:
                # 小さなモデルの答えは短くなりがちなので、決まった手順も並べて出す
                message += f"\n\nあわせて、一般的な手順：{row['answer']}"
            tried.append(f"社内文書で案内：{titles}")
            sources = ans.sources
        elif row:
            # 2. 社内文書に答えがなければ、一般的な手順で補う
            message = row["answer"]
            tried.append(f"案内した手順：{row['question']}")
            sources = []
        else:
            # 3. どちらにもなければ、聞き取った内容で票を作る
            return {"tried": tried, "resolved": False}

        if row and row["escalate_when"].startswith("常に"):
            return {"tried": tried, "sources": sources, "notice": message}
        return {"tried": tried, "sources": sources, "message": message}

    def confirm(s: State) -> State:
        # 案内（LLMの呼び出し）と返事待ちを別のノードにする。
        # 同じノードにすると、返事を受けて再開したときにLLMがもう一度呼ばれてしまうため。
        ok = is_yes(interrupt(f"{s['message']}\n\nこの手順で解決しましたか？（はい／いいえ）"))
        return {"resolved": ok}

    def done(s: State) -> State:
        return {"result": "解決してよかったです。ほかにお困りのことがあれば、また声をかけてください。"}

    def ticket(s: State) -> State:
        notice = s.get("notice")
        contact = interrupt((f"{notice}\n\n" if notice else "")
                            + "情報システム部に引き継ぎます。折り返しの連絡先（内線・携帯など）と、連絡のつく時間帯を教えてください。")
        details = ""
        if s.get("urgent") and s.get("kb") and s["kb"]["clarify"].strip():
            # 連絡先と状況を1つの質問にすると、票の「連絡先」に状況が混ざるので分けて聞く
            details = interrupt(f"わかる範囲で教えてください：{s['kb']['clarify']}。")
        now = datetime.now().astimezone()
        n = store.db.execute("SELECT COUNT(*) FROM helpdesk_tickets").fetchone()[0] + 1
        tid = f"HD-{now:%Y%m%d}-{n:04d}"
        t = {
            "ticket_id": tid,
            "created_at": now.isoformat(timespec="seconds"),
            "requester": s["requester"],
            "contact": contact,
            "details": details,
            "category": s["kb"]["category"] if s.get("kb") else "その他",
            "problem": s["problem"],
            "slots": s.get("slots", {}),
            "tried": s.get("tried", []),
            "sources": s.get("sources", []),
            "priority": calc_priority(s.get("slots", {}), s.get("urgent", False)),
            # 外部のAIに問い合わせるときに除く項目（個人を特定できる情報）
            "exclude_when_external": ["requester", "contact"],
        }
        with store.db:
            store.db.execute("INSERT INTO helpdesk_tickets VALUES (?, ?, ?, ?, ?)",
                             (tid, t["created_at"], t["category"], t["priority"], json.dumps(t, ensure_ascii=False)))
        return {"ticket": t, "result": f"受付番号 {tid} で引き継ぎました（優先度：{t['priority']}）。担当者から連絡します。"}

    # ------------------------------------------------------------ 分岐

    def after_classify(s: State) -> str:
        if s.get("urgent"):
            return "guide"   # 聞き返しより先に案内を出す
        if s.get("kb") and not s["kb"]["clarify"].strip():
            return "guide"   # 聞き返しの欄が空の手順（パスワード忘れなど）
        return "clarify"

    def after_clarify(s: State) -> str:
        if missing_slots(s["slots"]) and s["clarify_rounds"] < MAX_CLARIFY:
            return "clarify"
        return "outage_check"

    def after_outage(s: State) -> str:
        return END if s.get("outage") else "guide"

    def after_guide(s: State) -> str:
        return "confirm" if s.get("message") and not s.get("notice") else "ticket"

    def after_confirm(s: State) -> str:
        return "done" if s.get("resolved") else "ticket"

    g = StateGraph(State)
    for name, fn in [("identify", identify), ("intake", intake), ("classify", classify), ("clarify", clarify),
                     ("outage_check", outage_check), ("guide", guide), ("confirm", confirm), ("done", done),
                     ("ticket", ticket)]:
        g.add_node(name, fn)
    g.add_edge(START, "identify")
    g.add_edge("identify", "intake")
    g.add_edge("intake", "classify")
    g.add_conditional_edges("classify", after_classify, ["guide", "clarify"])
    g.add_conditional_edges("clarify", after_clarify, ["clarify", "outage_check"])
    g.add_conditional_edges("outage_check", after_outage, [END, "guide"])
    g.add_conditional_edges("guide", after_guide, ["confirm", "ticket"])
    g.add_conditional_edges("confirm", after_confirm, ["done", "ticket"])
    g.add_edge("done", END)
    g.add_edge("ticket", END)
    return g.compile(checkpointer=checkpointer or InMemorySaver())
