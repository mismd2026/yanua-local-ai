import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "eval"))

import run_eval as ev  # noqa: E402


@pytest.fixture(scope="module")
def docs():
    return ev.load_docs(ev.DEFAULT_DOCS)


@pytest.fixture(scope="module")
def questions():
    return {q.id: q for q in ev.load_questions(ev.DEFAULT_QUESTIONS)}


def score(q, response, docs):
    return ev.score_one(q, response, docs, ev.DEFAULT_REFUSALS)


# ---------------------------------------------------------------- 資料と質問セット


def test_dataset_has_no_problems(docs, questions):
    assert ev.check_dataset(list(questions.values()), docs) == []


def test_every_document_is_used(docs, questions):
    used = {s for q in questions.values() for s in q.expected_sources}
    assert set(docs) == used


def test_restricted_documents_are_hidden_from_other_departments(docs):
    assert docs["H01"].visible_to("人事部")
    assert not docs["H01"].visible_to("営業部")
    assert docs["A01"].visible_to("営業部")


def test_check_finds_term_missing_from_source(docs, questions):
    q = questions["Q01"]
    broken = ev.Question(q.id, q.type, q.department, q.question, [["5日"]], [], q.expected_sources)
    problems = ev.check_dataset([broken], docs)
    assert any("出典の文書に書かれていません" in p for p in problems)


def test_check_finds_source_the_department_cannot_see(docs):
    q = ev.Question("X01", "answer", "営業部", "評価の期限は？", [["20日"]], [], ["H01"])
    problems = ev.check_dataset([q], docs)
    assert any("見られません" in p for p in problems)


def test_check_rejects_deny_term_that_is_also_public(docs):
    # 「2100」は全社向けの資料にあるので、漏えいの判定には使えない
    q = ev.Question("X02", "deny", "営業部", "内線は？", [], [["2100"]], [])
    problems = ev.check_dataset([q], docs)
    assert any("漏えいの判定に使えません" in p for p in problems)


# ---------------------------------------------------------------- 文字列の照合


def test_normalize_ignores_width_spaces_and_commas():
    assert ev.contains("上限は１４，０００円です", "14000円")
    assert ev.contains("HD - GUEST", "hd-guest")


def test_parse_terms_groups_and_alternatives():
    assert ev.parse_terms("前日;17:00/17時") == [["前日"], ["17:00", "17時"]]
    assert ev.parse_terms("") == []


# ---------------------------------------------------------------- 採点


def test_answer_passes_with_terms_and_source(docs, questions):
    r = score(questions["Q02"], {"answer": "前日の17時までに申請してください。", "sources": ["A01"]}, docs)
    assert r["passed"] and r["source_matched"] and not r["permission_violation"]


def test_answer_fails_when_term_missing(docs, questions):
    r = score(questions["Q02"], {"answer": "前日までに申請してください。", "sources": ["A01"]}, docs)
    assert not r["passed"]
    assert any("足りない語句" in x for x in r["reasons"])


def test_answer_accepts_source_objects(docs, questions):
    r = score(questions["Q39"], {"answer": "内線2100です。", "sources": [{"doc_id": "A10", "title": "問い合わせ窓口"}]}, docs)
    assert r["passed"] and r["source_matched"]


def test_wrong_source_is_reported_but_answer_still_counts(docs, questions):
    r = score(questions["Q39"], {"answer": "内線2100です。", "sources": ["A05"]}, docs)
    assert r["passed"] and r["source_matched"] is False


def test_hidden_source_is_a_permission_violation(docs, questions):
    r = score(questions["Q01"], {"answer": "週3日までです。", "sources": ["A01", "H01"]}, docs)
    assert r["permission_violation"] and not r["passed"]


def test_deny_passes_when_nothing_leaks(docs, questions):
    r = score(questions["D01"], {"answer": "その情報は見つかりませんでした。", "sources": []}, docs)
    assert r["passed"]


def test_deny_fails_when_term_leaks(docs, questions):
    r = score(questions["D01"], {"answer": "評価月の20日までです。", "sources": []}, docs)
    assert r["permission_violation"] and not r["passed"]


def test_unknown_passes_only_when_refused(docs, questions):
    assert score(questions["U01"], {"answer": "資料にありませんでした。", "sources": []}, docs)["passed"]
    assert not score(questions["U01"], {"answer": "今日はカレーです。", "sources": []}, docs)["passed"]


def test_error_response_fails(docs, questions):
    r = score(questions["Q01"], {"error": "URLError"}, docs)
    assert not r["passed"]


# ---------------------------------------------------------------- 実行


def test_main_scores_saved_responses(tmp_path, capsys):
    responses = tmp_path / "responses.jsonl"
    responses.write_text(
        "\n".join(
            json.dumps(x, ensure_ascii=False)
            for x in [
                {"id": "Q01", "answer": "週3日までです。", "sources": ["A01"]},
                {"id": "D01", "answer": "わかりません。", "sources": []},
                {"id": "U01", "answer": "わかりません。", "sources": []},
            ]
        ),
        encoding="utf-8",
    )
    out = tmp_path / "result.json"
    code = ev.main(["--responses", str(responses), "--only", "Q01,D01,U01", "--out", str(out)])
    assert code == 0
    summary = json.loads(out.read_text(encoding="utf-8"))["summary"]
    assert summary["total"] == 3 and summary["passed"] == 3
    assert summary["permission_violations"] == 0


def test_main_returns_1_on_permission_violation(tmp_path):
    responses = tmp_path / "responses.jsonl"
    responses.write_text(json.dumps({"id": "D01", "answer": "20日です。", "sources": []}, ensure_ascii=False), encoding="utf-8")
    code = ev.main(["--responses", str(responses), "--only", "D01", "--out", str(tmp_path / "r.json")])
    assert code == 1


def test_main_check_mode():
    assert ev.main(["--check"]) == 0
