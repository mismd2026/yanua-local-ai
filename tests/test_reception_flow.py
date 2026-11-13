"""社外向けの受付の流れのテスト。Ollama は使わず、決まった返事をする偽のモデルで確かめる。"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yanua import hr  # noqa: E402
from yanua.answer import answer_question  # noqa: E402
from yanua.flows.external.reception import build_app  # noqa: E402
from yanua.flows.external.reception.cli import DEMO_TODAY, demo_answers, run  # noqa: E402
from yanua.search import search, search_public  # noqa: E402
from yanua.store import Store  # noqa: E402

DEMO_DOCS = ROOT / "demo" / "company" / "docs"
PUBLIC_DOCS = ROOT / "demo" / "company" / "public"
RECEPTION_DATA = ROOT / "demo" / "company" / "flows" / "reception"
HR_DIR = ROOT / "demo" / "company" / "hr"
TODAY = date.fromisoformat(DEMO_TODAY)


class FakeLLM:
    """種類の判定には「問い合わせ」を、回答には資料の番号つきの文か「わかりません」を返す。"""

    name = "fake"

    def __init__(self):
        self.prompts: list[str] = []
        self.answer_prompts: list[str] = []

    def chat(self, messages):
        text = messages[-1]["content"]
        self.prompts.append(text)
        if "JSONだけを返して" in messages[0]["content"]:
            return json.dumps({"category": "問い合わせ"}, ensure_ascii=False)
        self.answer_prompts.append(text)
        if "9:30" in text and "中央駅" in text:
            return "営業時間は平日の9:30〜17:00です[1]。架空線「中央駅」の北口から徒歩5分です[1]。"
        return "わかりません"


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "index.db")
    assert s.ingest(DEMO_DOCS).errors == []
    assert s.ingest(PUBLIC_DOCS).errors == []
    report = hr.import_employees(s.db, HR_DIR / "hr_export_sample.csv", hr.load_mapping(HR_DIR / "mapping_sample.json"))
    assert report.errors == []
    yield s
    s.close()


def run_demo(store, key):
    llm = FakeLLM()
    app = build_app(store, llm, RECEPTION_DATA, TODAY)
    channel, _, answer_for = demo_answers(key)
    lines: list[str] = []
    state = run(app, channel, answer_for, out=lines.append)
    return state, llm, lines


def customer_lines(lines):
    """お客様に見せた文だけ（社内向けの記録は除く）。"""
    return "\n".join(line for line in lines if line.startswith("\nAI"))


# ---------------------------------------------------------------- 社外公開の文書だけを使う


def test_public_search_never_returns_internal_documents(store):
    public_ids = {"P01", "P02", "P03", "P04"}
    for q in ["在宅勤務は週に何日まで？", "人事評価の基準", "会議室の予約", "問い合わせ窓口の内線", "営業時間"]:
        assert {h.doc_id for h in search_public(store, q, 10)} <= public_ids


def test_internal_users_can_also_read_public_documents(store):
    assert "P01" in {h.doc_id for h in search(store, "代表電話の番号", "営業部", 10)}


def test_public_answer_gives_llm_only_public_documents(store):
    llm = FakeLLM()
    result = answer_question(store, llm, "問い合わせ窓口の内線番号を教えて", "", public=True)
    assert result.refused
    # 社内向けの窓口一覧（A10 の内線 2100 など）は、LLMに渡っていない
    assert all("2100" not in p and "it-help@" not in p for p in llm.answer_prompts)


# ---------------------------------------------------------------- デモ


def test_visitor_with_appointment_is_guided_to_room(store):
    state, llm, lines = run_demo(store, "1")
    r = state["record"]
    assert r["category"] == "取次" and r["host"]["employee_no"] == "10004"
    assert r["appointment"]["room"] == "5階 第2会議室"
    assert "5階 第2会議室" in state["result"]
    assert r["route"] == "営業部"
    assert llm.prompts == []   # 取次はLLMを使わない


def test_inquiry_answered_from_public_document(store):
    state, llm, lines = run_demo(store, "2")
    assert "ありがとうございました" in state["result"] and "record" not in state
    assert "中央駅" in customer_lines(lines)


def test_claim_uses_fixed_text_and_high_priority(store):
    state, llm, lines = run_demo(store, "3")
    r = state["record"]
    assert r["category"] == "クレーム" and r["priority"] == "高" and r["route"] == "お客様相談室"
    assert "A-1234" in r["details"] and "090-1111-2222" in r["contact"]
    assert llm.prompts == []   # おわびや約束をLLMに作らせない


def test_sales_without_appointment_is_declined_politely(store):
    state, llm, _ = run_demo(store, "4")
    r = state["record"]
    assert r["category"] == "営業" and r["priority"] == "低"
    assert "お受けしておりません" in state["result"]
    assert r["visitor"] == {"company": "XYZ事務機", "name": "中村 翔"}


def test_internal_information_is_not_answered(store):
    state, llm, lines = run_demo(store, "5")
    r = state["record"]
    assert r["answer"]["refused"] and r["route"] == "総務部"
    # 人事評価（H01、人事部だけ）も全社向けの文書も、LLMに渡っていない
    h01 = (DEMO_DOCS / "H01_人事評価.md").read_text(encoding="utf-8").split("---", 2)[2]
    body_line = next(line for line in h01.splitlines() if len(line.strip()) > 15)
    assert all(body_line.strip() not in p for p in llm.prompts)


# ---------------------------------------------------------------- 社員の情報を出さない


def answers(mapping):
    # 「どの者をお訪ねでしょうか…お名前」は「お名前」も含むので、長い・特定の言葉から先に照合する
    keys = sorted(mapping, key=lambda k: k != "どの者")
    return lambda q: next(mapping[k] for k in keys if k in q)


def test_phone_relay_does_not_reveal_employee_details(store):
    llm = FakeLLM()
    app = build_app(store, llm, RECEPTION_DATA, TODAY)
    lines: list[str] = []
    state = run(app, "電話", answers({"ご用件": "田中さんをお願いします。", "会社名": "DEF工業", "お名前": "吉田",
                                    "どの者": "田中 健", "折り返し": "03-4444-5555"}), out=lines.append)
    r = state["record"]
    assert r["host"]["employee_no"] == "10005" and r["route"] == "情報システム部"
    shown = customer_lines(lines)
    assert "情報システム部" not in shown and "10005" not in shown


def test_unknown_or_retired_employee_gets_same_reply(store):
    """退職した人や、いない人を訪ねられても、在籍しているかどうかが返事からわからない。"""
    results = []
    for name in ["渡辺 剛", "存在 しない"]:
        app = build_app(store, FakeLLM(), RECEPTION_DATA, TODAY)
        state = run(app, "来訪", answers({"ご用件": f"{name}さんにお約束しています", "会社名": "GHI社", "お名前": "林",
                                        "どの者": name}), out=lambda *_: None)
        assert state["record"]["host"] is None and state["record"]["route"] == "総務部"
        results.append(state["result"])
    assert results[0] == results[1]


def test_same_surname_asks_full_name(store, tmp_path):
    # 同じ姓の社員を足して、姓だけでは決めない（フルネームを聞き直す）ことを確かめる
    csv_path = tmp_path / "hr.csv"
    rows = (HR_DIR / "hr_export_sample.csv").read_text(encoding="utf-8-sig").rstrip("\n")
    csv_path.write_text(rows + "\n10008,高橋 誠,タカハシ マコト,経理部,300,在籍,1990-01-01,2020-04-01\n", encoding="utf-8")
    hr.import_employees(store.db, csv_path, hr.load_mapping(HR_DIR / "mapping_sample.json"))
    app = build_app(store, FakeLLM(), RECEPTION_DATA, TODAY)
    asked: list[str] = []

    def answer_for(q):
        asked.append(q)
        return answers({"ご用件": "高橋さんと打ち合わせです", "会社名": "JKL社", "お名前": "森",
                        "どの者": "高橋 誠"})(q)

    state = run(app, "来訪", answer_for, out=lambda *_: None)
    assert any("フルネーム" in q for q in asked)
    assert state["record"]["host"]["employee_no"] == "10008"


def test_unclear_purpose_falls_back_to_inquiry(store):
    class Broken(FakeLLM):
        def chat(self, messages):
            if "JSONだけを返して" in messages[0]["content"]:
                return "ごめんなさい"
            return super().chat(messages)

    app = build_app(store, Broken(), RECEPTION_DATA, TODAY)
    state = run(app, "Web", answers({"ご用件": "ちょっと聞きたいことがあって", "お名前と": "佐々木、sasaki@example.com"}),
                out=lambda *_: None)
    assert state["record"]["category"] == "問い合わせ"
