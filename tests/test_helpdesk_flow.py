"""社内向けヘルプデスクの会話の流れのテスト。Ollama は使わず、決まった返事をする偽のモデルで確かめる。"""

import json
import re
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yanua import hr  # noqa: E402
from yanua.flows.internal.helpdesk import build_app  # noqa: E402
from yanua.flows.internal.helpdesk.graph import extract_slots  # noqa: E402
from yanua.flows.internal.helpdesk.cli import demo_answers, run  # noqa: E402
from yanua.store import Store  # noqa: E402

DEMO_DOCS = ROOT / "demo" / "company" / "docs"
HELPDESK_DATA = ROOT / "demo" / "company" / "flows" / "helpdesk"
HR_DIR = ROOT / "demo" / "company" / "hr"


class FakeLLM:
    """項目の取り出しにはJSONを、社内文書の回答には資料の番号つきの文を返す。

    項目の取り出しでは、わざと「全社」を返す（ルールでの補正を確かめるため）。
    """

    name = "fake"

    def __init__(self):
        self.answer_prompts: list[str] = []
        self.json_prompts: list[str] = []

    def chat(self, messages):
        text = messages[-1]["content"]
        if "JSONだけを返して" in messages[0]["content"]:
            self.json_prompts.append(text)
            reply = text.split("返事:")[-1]
            loc = re.search(r"本社\d階", reply)
            return json.dumps({
                "device": "複合機" if "複合機" in reply else ("有線LAN" if "有線" in reply else "不明"),
                "location": loc.group(0) if loc else "不明",
                "scope": "全社" if ("全員" in reply or "周り" in reply) else "不明",
                "business": "止まっている" if "止まって" in reply else "不明",
            }, ensure_ascii=False)
        self.answer_prompts.append(text)
        if "HD-PRINT" in text:
            return "PCから「HD-PRINT」を選んで印刷し、複合機に社員証をかざしてください[1]。"
        if "パスワードを忘れた" in text:
            return "パスワード再設定の手順に従って再設定してください[1]。"
        return "わかりません"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "index.db")
    assert s.ingest(DEMO_DOCS).errors == []
    hr.import_employees(s.db, HR_DIR / "hr_export_sample.csv", hr.load_mapping(HR_DIR / "mapping_sample.json"))
    yield s
    s.close()


def run_demo(store, key):
    llm = FakeLLM()
    app = build_app(store, llm, HELPDESK_DATA)
    account, _, answer_for = demo_answers(key)
    lines: list[str] = []
    state = run(app, account, answer_for, out=lines.append)
    return state, llm, lines


def tickets(store):
    return [json.loads(r[0]) for r in store.db.execute("SELECT payload FROM helpdesk_tickets")]


def test_shared_account_printer_not_resolved_creates_ticket(store):
    state, llm, lines = run_demo(store, "1")
    t = state["ticket"]
    assert t["requester"]["verified"] == "人事データと一致"
    assert t["requester"]["matched"] == ["従業員番号", "名前", "部署"]
    assert t["requester"]["dept"] == "営業部" and t["requester"]["account_type"] == "shared"
    # 社内文書（A06）から出典つきで案内した
    assert [s["doc_id"] for s in t["sources"]] == ["A06"]
    assert any("HD-PRINT" in line for line in lines)
    # LLMが「全社」と答えても、広い範囲を示す言葉がなければ「部署・複数人」に直す
    assert t["slots"]["scope"] == "部署・複数人"
    assert t["priority"] == "高"
    assert tickets(store)[0]["ticket_id"] == t["ticket_id"]
    # 返事を受けて再開しても、案内のためのLLM呼び出しは1回だけ
    assert len(llm.answer_prompts) == 1


def test_personal_account_network_outage(store):
    state, llm, _ = run_demo(store, "2")
    assert "通信障害" in state["result"]
    assert "ticket" not in state and tickets(store) == []
    assert llm.answer_prompts == []   # 障害中なので個別の案内はしない


def test_suspicious_mail_notice_comes_first_and_always_escalates(store):
    state, llm, lines = run_demo(store, "3")
    questions = [line for line in lines if line.startswith("\nAI  >")]
    # 「お困りごと」の次の質問に、切り離しの案内が入っている（聞き返しより先）
    i = next(n for n, q in enumerate(questions) if "お困り" in q)
    assert "LANケーブルを抜く" in questions[i + 1]
    t = state["ticket"]
    assert t["priority"] == "高" and t["category"] == "メール"
    assert t["requester"]["verified"] == "照合できず"
    assert "未登録" in t["requester"]["note"] and t["requester"]["declared"]["dept"] == "総務部"
    # 連絡先と状況は別々に聞くので、票の「連絡先」に状況が混ざらない
    assert t["contact"] == "内線2201、終日" and "9時" in t["details"]
    assert llm.answer_prompts == [] and llm.json_prompts == []   # 緊急時はLLMを待たない


def test_personal_account_used_by_someone_else_password(store):
    """番号がわからなくても、名前と部署（「総務」→総務部）で本人と確定する。"""
    state, llm, lines = run_demo(store, "4")
    assert any("佐藤 花子さん、ありがとうございます" in line for line in lines)
    assert "解決してよかった" in state["result"]
    # 社内文書の答えに加えて、決まった手順（セルフサービスの案内）も出す
    assert any("出典" in line and "セルフサービス" in line for line in lines)
    assert "ticket" not in state
    assert len(llm.answer_prompts) == 1 and "パスワードを忘れた" in llm.answer_prompts[0]


def test_unverified_person_sees_only_company_wide_documents(store):
    """照合できない人が「人事部」と名乗っても、人事部だけの文書（H01など）は検索の候補に入らない。"""
    llm = FakeLLM()
    app = build_app(store, llm, HELPDESK_DATA)
    answers = {"本人確認を行う都合上": "人事部の架空太郎です。番号は88888です。",
               "お困り": "人事評価の面談シートの書き方を教えて", "状況": "自分だけです。業務は一部できません。本社3階のPCです。",
               "あと少し": "本社3階のPC、自分だけ、一部できない", "連絡先": "内線1000"}
    run(app, "soumu-common", lambda q: next(v for k, v in answers.items() if k in q), out=lambda *_: None)
    h01 = (DEMO_DOCS / "H01_人事評価.md").read_text(encoding="utf-8").split("---", 2)[2]
    body_line = next(line for line in h01.splitlines() if len(line.strip()) > 15)
    assert all(body_line.strip() not in p for p in llm.answer_prompts)


def test_retired_employee_is_not_verified(store):
    llm = FakeLLM()
    app = build_app(store, llm, HELPDESK_DATA)
    answers = {"本人確認を行う都合上": "営業部の渡辺剛です。10007です。",
               "お困り": "パスワードを忘れました", "解決しましたか": "いいえ", "連絡先": "携帯 090-0000-0000"}
    state = run(app, "eigyo-common", lambda q: next(v for k, v in answers.items() if k in q), out=lambda *_: None)
    r = state["ticket"]["requester"]
    assert r["verified"] == "照合できず"
    # 言われた内容は、照合できなくても票に残す
    assert r["declared"]["employee_no"] == "10007" and r["declared"]["name"] == "渡辺剛"


class SaysNoImpact:
    name = "fake"

    def chat(self, messages):
        return json.dumps({"device": "複合機", "location": "本社2階", "scope": "部署・複数人", "business": "支障なし"},
                          ensure_ascii=False)


def test_business_impact_is_corrected_by_rule():
    """小さなモデルが「業務が止まっています」を「支障なし」と取っても、ルールで「止まっている」に直す。"""
    out = extract_slots(SaysNoImpact(), "印刷できない", "部署の全員が印刷できず、業務が止まっています。")
    assert out["business"] == "止まっている"
    out = extract_slots(SaysNoImpact(), "印刷できない", "自分だけです。業務は止まっていません。")
    assert out["business"] == "支障なし"


class Says:
    """範囲と業務への影響を決まった値で返す偽のモデル。"""

    name = "fake"

    def __init__(self, scope, business="不明"):
        self.scope, self.business = scope, business

    def chat(self, messages):
        return json.dumps({"device": "複合機", "location": "本社2階", "scope": self.scope, "business": self.business},
                          ensure_ascii=False)


@pytest.mark.parametrize("model_scope, answer, expected", [
    # モデルが取りこぼした「全員」は、ルールで「部署・複数人」に補う（実機の3bで起きた）
    ("不明", "部署の全員", "部署・複数人"),
    ("不明", "本社2階の営業部の複合機です。部署の全員が印刷できず、業務が止まっています。", "部署・複数人"),
    ("自分だけ", "周りの人も印刷できません", "部署・複数人"),
    ("不明", "他の店でも同じです", "全社"),
    ("不明", "自分だけです", "自分だけ"),
    ("不明", "ほかの人は使えています", "自分だけ"),
    # 言葉がなければ、モデルの答えのまま
    ("不明", "複合機です", "不明"),
    ("自分だけ", "複合機です", "自分だけ"),
    ("不明", "部署の複合機です", "不明"),
    ("不明", "部署の複合機ですが、使えないのは自分だけです", "自分だけ"),
    # 「全員」だけで「全社」にはしない（今までのルール）
    ("全社", "部署の全員が使えません", "部署・複数人"),
])
def test_scope_is_completed_by_rule(model_scope, answer, expected):
    assert extract_slots(Says(model_scope), "印刷できない", answer)["scope"] == expected


def test_priority_high_when_department_stops():
    """「部署の全員が止まっている」は、モデルが範囲を取りこぼしても優先度「高」になる。"""
    from yanua.flows.internal.helpdesk.graph import calc_priority

    slots = extract_slots(Says("不明", "止まっている"), "印刷できない", "部署の全員が印刷できず、業務が止まっています。")
    assert calc_priority(slots, urgent=False) == "高"


def test_identity_follow_ups_in_conversation(store):
    """部署だけ → 「お名前か従業員番号」→ 名前がヒットしない → 「従業員番号」→ 番号と部署で確定。"""
    app = build_app(store, FakeLLM(), HELPDESK_DATA)
    asked = []
    answers = iter(["営業部です", "石母田です", "10004", "パスワードを忘れました", "はい"])

    def ask(q):
        asked.append(q)
        return next(answers)

    state = run(app, "eigyo-common", ask, out=lambda *_: None)
    assert "本人確認を行う都合上" in asked[0]
    assert "お名前か従業員番号" in asked[1]
    assert "従業員番号を教えて" in asked[2] and "お名前" not in asked[2]
    assert asked[3].startswith("高橋 美咲さん、ありがとうございます")
    r = state["requester"]
    assert r["verified"] == "人事データと一致" and r["matched"] == ["従業員番号", "部署"]
    assert r["declared"]["name"] == "石母田"


def test_identity_gives_up_but_takes_request(store):
    """聞き返しても確定しなければ、申告内容を控えて依頼は受ける（聞き返しは方針の回数まで）。"""
    app = build_app(store, FakeLLM(), HELPDESK_DATA)
    asked = []
    answers = iter(["私ですが", "わからない", "パスワードを忘れました", "いいえ", "内線1234"])

    def ask(q):
        asked.append(q)
        return next(answers)

    state = run(app, "eigyo-common", ask, out=lambda *_: None)
    # 何も聞き取れなければ3つとも聞き直し、「わからない」と言われたらそれ以上は聞かない
    assert "所属部署とお名前と従業員番号" in asked[1]
    assert sum("恐れ入りますが" in q for q in asked) == 1
    assert asked[2].startswith("ありがとうございます。どのようなことでお困りですか")
    assert state["ticket"]["requester"]["verified"] == "照合できず"
    assert state["ticket"]["requester"]["declared"]["said"] == "私ですが / わからない"
