"""社外向けの受付の流れのテスト。Ollama は使わず、決まった返事をする偽のモデルで確かめる。"""

import json
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yanua import hr, visits  # noqa: E402
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
    visits.import_csv(s.db, RECEPTION_DATA / "visits.csv")
    yield s
    s.close()


def run_demo(store, key, passes_dir=None):
    llm = FakeLLM()
    app = build_app(store, llm, RECEPTION_DATA, TODAY, passes_dir=passes_dir)
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


def test_invitation_code_shows_visit_prints_pass_and_notifies_host(store, tmp_path):
    state, llm, lines = run_demo(store, "1", passes_dir=tmp_path)
    r = state["record"]
    assert r["category"] == "来訪" and r["host"]["employee_no"] == "10002" and r["route"] == "総務部"
    for text in ("2026-11-16 10:00", "5階 第1会議室", "佐藤 花子", "内線2200"):
        assert text in state["result"]
    page = Path(state["pass_file"]).read_text(encoding="utf-8")
    assert "5階 第1会議室" in page and "MRKT" not in page   # 入館票にコードは載せない
    notes = visits.notifications(store.db, "10002", "総務部")
    assert len(notes) == 1 and "森 さくら" in notes[0]["message"]
    assert llm.prompts == []   # コードでの受付はLLMを使わない


def test_second_check_in_does_not_notify_again(store):
    run_demo(store, "1")
    run_demo(store, "1")
    assert len(visits.notifications(store.db, "10002", "総務部")) == 1


def test_wrong_or_other_day_code_gets_same_reply_and_falls_back(store):
    """違う日のコードも間違ったコードも同じ返事。3回で用件を聞く流れに移る。"""
    asked: list[str] = []
    replies = iter(["MRKT-7Q4A", "ZZZZ-ZZZZ", "HJ6P-W9XE", "会議室の場所を教えてください", "林、03-0000-1111"])

    def answer_for(q):
        asked.append(q)
        return next(replies)

    app = build_app(store, FakeLLM(), RECEPTION_DATA, date(2026, 11, 17))   # 予定の翌日
    state = run(app, "来訪", answer_for, out=lambda *_: None)
    assert asked[1] == asked[2] and "確認できませんでした" in asked[1]
    assert "ご用件をお聞かせください" in asked[3] and "もう一度" not in asked[3]
    assert "code_visit" not in state or not state.get("code_visit")
    assert visits.notifications(store.db) == [] or all(n["kind"] != "来訪" for n in visits.notifications(store.db))


def test_visitor_without_code_is_matched_by_name_and_appointment(store):
    state, llm, lines = run_demo(store, "6")
    r = state["record"]
    assert r["category"] == "取次" and r["host"]["employee_no"] == "10004"
    assert r["visitor"] == {"company": "ABC商事", "name": "木村 誠"}   # 名乗りで照合し、予定のお名前で記録
    assert not any("会社名をお聞かせ" in line for line in lines)
    assert "5階 第2会議室" in state["result"] and r["route"] == "営業部"
    assert len(visits.notifications(store.db, "10004", "営業部")) == 1
    assert llm.prompts == []


def test_inquiry_answered_from_public_document(store):
    state, llm, lines = run_demo(store, "2")
    assert "ありがとうございました" in state["result"]
    # 解決した問い合わせも記録する（集計用）。お知らせは出さない
    assert state["record"]["resolved"] and state["record"]["priority"] == "低"
    assert visits.notifications(store.db) == []
    shown = customer_lines(lines)
    assert "中央駅" in shown


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
    assert r["visitor"] == {"company": "XYZ事務機", "name": "中村"}


def test_internal_information_is_not_answered(store):
    state, llm, lines = run_demo(store, "5")
    r = state["record"]
    assert r["answer"]["refused"] and r["route"] == "総務部"
    # 「改めてご連絡いたします」を2回言わない
    assert customer_lines(lines).count("改めてご連絡") == 1
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


def test_same_surname_is_not_asked_again_but_left_to_general_affairs(store, tmp_path):
    """同じ姓の社員が複数いても聞き直さない（聞き直すと、その姓の社員が複数いることが分かるため）。総務部が候補から確認する。"""
    csv_path = tmp_path / "hr.csv"
    rows = (HR_DIR / "hr_export_sample.csv").read_text(encoding="utf-8-sig").rstrip("\n")
    csv_path.write_text(rows + "\n10008,高橋 誠,タカハシ マコト,経理部,300,takahashi.ma@example.co.jp,在籍,1990-01-01,2020-04-01\n",
                        encoding="utf-8")
    hr.import_employees(store.db, csv_path, hr.load_mapping(HR_DIR / "mapping_sample.json"))
    app = build_app(store, FakeLLM(), RECEPTION_DATA, TODAY)
    asked: list[str] = []

    def answer_for(q):
        asked.append(q)
        return answers({"ご用件": "高橋さんと打ち合わせです", "会社名": "JKL社", "お名前": "森", "どの者": "高橋 誠"})(q)

    state = run(app, "来訪", answer_for, out=lambda *_: None)
    assert not any("どの者" in q for q in asked)
    r = state["record"]
    assert r["host"] is None and r["route"] == "総務部" and set(r["candidates"]) == {"10004", "10008"}


def test_host_is_asked_when_no_name_is_mentioned(store):
    app = build_app(store, FakeLLM(), RECEPTION_DATA, TODAY)
    asked: list[str] = []

    def answer_for(q):
        asked.append(q)
        return answers({"ご用件": "打ち合わせで参りました", "会社名": "JKL社", "お名前": "森", "どの者": "鈴木 一郎"})(q)

    state = run(app, "来訪", answer_for, out=lambda *_: None)
    assert any("どの者" in q for q in asked) and state["record"]["host"]["employee_no"] == "10003"


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


def test_claim_notifies_customer_desk(store):
    run_demo(store, "3")
    notes = visits.notifications(store.db, dept="お客様相談室")
    assert len(notes) == 1 and notes[0]["kind"] == "クレーム"


def test_self_intro_denied_asks_again(store):
    app = build_app(store, FakeLLM(), RECEPTION_DATA, TODAY)
    state = run(app, "来訪", answers({"ご用件": "XYZ事務機の中村です。ご提案に伺いました。", "いらっしゃいますね": "いいえ",
                                    "会社名": "XYZ事務機販売", "お名前": "中村 翔"}), out=lambda *_: None)
    assert state["record"]["visitor"] == {"company": "XYZ事務機販売", "name": "中村 翔"}


def test_answer_speaks_as_company():
    from yanua.flows.external.reception.graph import as_company_voice
    assert as_company_voice("御社の営業時間は9:30からです") == "当社の営業時間は9:30からです"


def test_code_is_stored_only_as_hash(store):
    visit, code = visits.create(store.db, host_employee_no="10003", visit_date="2026-11-20", time="11:00",
                                room="5階 第1会議室", visitor_company="QRS社", visitor_name="大野 進")
    assert visits.looks_like_code(code)
    dump = "\n".join(store.db.iterdump())
    assert code not in dump
    assert visits.find_by_code(store.db, code.lower(), date(2026, 11, 20))["visit_id"] == visit["visit_id"]
    assert visits.find_by_code(store.db, code, date(2026, 11, 21)) is None


def test_visit_for_retired_host_is_rejected(store):
    with pytest.raises(visits.VisitError):
        visits.create(store.db, host_employee_no="10007", visit_date="2026-11-20", time="11:00",
                      room="5階 第1会議室", visitor_company="QRS社", visitor_name="大野 進")


def test_demo_visits_can_be_imported_twice(store):
    assert visits.import_csv(store.db, RECEPTION_DATA / "visits.csv") == []
    assert len(visits.for_date(store.db, TODAY)) == 2


@pytest.mark.parametrize("text", ["山田様に用があってきました", "田中さんを呼んでいただけますか", "鈴木様宛の書類を持ってきました"])
def test_named_person_is_relay(text):
    from yanua.flows.external.reception.graph import classify_by_rule
    assert classify_by_rule(text) == "取次"


@pytest.mark.parametrize("text", ["社員の皆さんの評価を教えて", "お客様向けの窓口はどこですか", "御社は何時まで営業ですか"])
def test_not_named_person(text):
    from yanua.flows.external.reception.graph import classify_by_rule
    assert classify_by_rule(text) != "取次"


def test_unknown_name_is_relayed_the_same_way(store):
    """社員でない名前でも、社員の名前と同じ流れ（取次）になる。名前が社員かどうかを返事の違いから探れない。"""
    shown = []
    for name in ["山田", "存在しない人"]:
        app = build_app(store, FakeLLM(), RECEPTION_DATA, TODAY)
        asked: list[str] = []
        state = run(app, "来訪", answers({"ご用件": f"{name}様に用があってきました", "会社名": "UVW社", "お名前": "高田",
                                        "どの者": name}), out=asked.append)
        assert state["record"]["category"] == "取次"
        shown.append([line for line in asked if line.startswith("\nAI")])
    assert [len(x) for x in shown][0] == [len(x) for x in shown][1]


def test_visitor_unanswered_inquiry_calls_staff(store):
    app = build_app(store, FakeLLM(), RECEPTION_DATA, TODAY)
    lines: list[str] = []
    state = run(app, "来訪", answers({"ご用件": "ちょっと聞きたいことがあって", "会社名とお名前": "UVW社 高田"}), out=lines.append)
    assert "少々お待ちください" in state["result"]
    assert "改めてご連絡" not in customer_lines(lines)
