"""回答とAPIのテスト。Ollama は使わず、決まった返事をする偽のモデルで確かめる。"""

import json
import sys
import threading
import urllib.error
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yanua.answer import NOT_FOUND_MESSAGE, answer_question, build_messages, cited_hits  # noqa: E402
from yanua.llm import LLMError, OllamaChat  # noqa: E402
from yanua.search import search  # noqa: E402
from yanua.server import serve  # noqa: E402
from yanua.store import Store  # noqa: E402

DEMO_DOCS = ROOT / "demo" / "company" / "docs"


class FakeLLM:
    """渡された資料を記録し、決まった返事をする。"""

    name = "fake"

    def __init__(self, reply="在宅勤務は週3日までです[1]。"):
        self.reply = reply
        self.seen: list[list[dict]] = []

    def chat(self, messages):
        self.seen.append(messages)
        return self.reply(messages) if callable(self.reply) else self.reply


class BrokenLLM:
    name = "broken"

    def chat(self, messages):
        raise LLMError("接続できません")


@pytest.fixture
def db(tmp_path):
    path = tmp_path / "index.db"
    store = Store(path)
    assert store.ingest(DEMO_DOCS).errors == []
    store.close()
    return path


@pytest.fixture
def store(db):
    s = Store(db)
    yield s
    s.close()


# ---------------------------------------------------------------- 回答


def test_answer_with_citation(store):
    llm = FakeLLM()
    result = answer_question(store, llm, "在宅勤務は週に何日までできますか？", "営業部")
    assert not result.refused
    assert result.sources[0]["doc_id"] == "A01"
    assert result.sources[0]["section"] == "在宅勤務"


def test_llm_sees_only_documents_the_department_can_view(store):
    """人事部だけの資料の中身を聞いても、営業部の質問ではLLMに渡らない。"""
    llm = FakeLLM("わかりません")
    answer_question(store, llm, "人事評価の入力期限と、SとDの評価に必要なものは？", "営業部")
    prompt = llm.seen[0][1]["content"]
    assert "人事評価の運用手順" not in prompt
    assert "部長のコメント" not in prompt


def test_department_gets_its_own_restricted_document(store):
    llm = FakeLLM("評価月の20日です[1]。")
    result = answer_question(store, llm, "人事評価の入力期限はいつですか？", "人事部")
    assert result.sources[0]["doc_id"] == "H01"


def test_refusal_from_llm_returns_not_found_without_sources(store):
    result = answer_question(store, FakeLLM("わかりません"), "社員食堂の今日のメニューは？", "営業部")
    assert result.refused and result.sources == []
    assert result.answer == NOT_FOUND_MESSAGE


@pytest.mark.parametrize(
    "text, refused",
    [
        ("わかりません", True),
        ("「わかりません」", True),
        ("資料からはわかりません。", True),
        ("送れるかどうかは資料からはわかりませんが、社外には送れません[1]。", False),
        ("週3日までです[1]。", False),
    ],
)
def test_is_refusal(text, refused):
    from yanua.answer import is_refusal

    assert is_refusal(text) is refused


def test_cited_answer_mentioning_unknown_is_kept(store):
    reply = "細かい条件はわかりませんが、在宅勤務は週3日までです[1]。"
    result = answer_question(store, FakeLLM(reply), "在宅勤務は週に何日までできますか？", "営業部")
    assert not result.refused and result.sources[0]["doc_id"] == "A01"


def test_model_text_only_in_debug(store):
    result = answer_question(store, FakeLLM("わかりません"), "在宅勤務は？", "営業部")
    assert "model_text" not in result.as_dict()
    assert result.as_dict(debug=True)["model_text"] == "わかりません"


def test_no_search_hits_refuses_without_calling_llm(store):
    llm = FakeLLM()
    result = answer_question(store, llm, "zzzz", "営業部")
    assert result.refused and llm.seen == []


def test_answer_without_citation_falls_back_to_top_hit(store):
    result = answer_question(store, FakeLLM("週3日までです。"), "在宅勤務は週に何日までできますか？", "営業部")
    assert [s["doc_id"] for s in result.sources] == ["A01"]


def test_out_of_range_citation_is_ignored(store):
    hits = search(store, "在宅勤務は週に何日まで", "営業部", 2)
    assert cited_hits("[9] と [0]", hits) == []
    assert [h.doc_id for h in cited_hits("[1][1][2]", hits)] == ["A01"]


def test_prompt_numbers_each_hit(store):
    hits = search(store, "在宅勤務の申請と経費精算", "営業部", 3)
    assert len(hits) == 3
    content = build_messages("在宅勤務は？", hits)[1]["content"]
    assert "[1]" in content and "[3]" in content and "[4]" not in content and "質問: 在宅勤務は？" in content


def test_empty_question_is_rejected(store):
    with pytest.raises(ValueError):
        answer_question(store, FakeLLM(), "  ", "営業部")


def test_ollama_unreachable_raises_llm_error():
    llm = OllamaChat("qwen2.5:3b", "http://127.0.0.1:9", timeout=2)
    with pytest.raises(LLMError):
        llm.chat([{"role": "user", "content": "test"}])


# ---------------------------------------------------------------- HTTP API


@pytest.fixture
def api(db):
    def start(llm):
        server = serve(str(db), llm, "127.0.0.1", 0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        servers.append(server)
        return f"http://127.0.0.1:{server.server_address[1]}"

    servers = []
    yield start
    for s in servers:
        s.shutdown()
        s.server_close()


def post(url, payload):
    req = urllib.request.Request(url, data=json.dumps(payload).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=10) as res:
            return res.status, json.loads(res.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


def test_api_ask(api):
    base = api(FakeLLM())
    status, body = post(f"{base}/ask", {"question": "在宅勤務は週に何日までできますか？", "department": "営業部"})
    assert status == 200
    assert body["sources"][0]["doc_id"] == "A01" and "elapsed_sec" in body


def test_api_health(api):
    base = api(FakeLLM())
    with urllib.request.urlopen(f"{base}/health", timeout=10) as res:
        assert json.loads(res.read()) == {"status": "ok", "model": "fake", "documents": 20}


def test_api_rejects_bad_requests(api):
    base = api(FakeLLM())
    assert post(f"{base}/ask", {"question": "", "department": "営業部"})[0] == 400
    req = urllib.request.Request(f"{base}/ask", data=b"not json", headers={"Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as e:
        urllib.request.urlopen(req, timeout=10)
    assert e.value.code == 400
    assert post(f"{base}/other", {})[0] == 404


def test_api_reports_llm_failure(api):
    base = api(BrokenLLM())
    status, body = post(f"{base}/ask", {"question": "在宅勤務は週に何日まで？", "department": "営業部"})
    assert status == 502 and "接続できません" in body["error"]


def test_eval_shows_error_reason_and_stops(api, tmp_path, capsys):
    sys.path.insert(0, str(ROOT / "eval"))
    import run_eval

    base = api(BrokenLLM())
    code = run_eval.main(["--endpoint", f"{base}/ask", "--only", "Q01,Q02,Q03,Q04,Q05", "--out", str(tmp_path / "r.json")])
    out = capsys.readouterr().out
    assert code == 2
    assert "HTTP 502: 接続できません" in out
    assert "Q04" not in out  # 3回続けて失敗した時点で止まる


class FakeOllama:
    """/api/tags と /api/chat だけを返す、Ollama の代わりの小さなサーバー。"""

    def __init__(self, models, chat_status=200):
        from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

        outer = self

        class H(BaseHTTPRequestHandler):
            def _json(self, status, payload):
                body = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                self._json(200, {"models": [{"name": m} for m in outer.models]})

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                if outer.chat_status == 200:
                    self._json(200, {"message": {"content": "OK"}})
                else:
                    self._json(outer.chat_status, {"error": 'model "x" not found, try pulling it first'})

            def log_message(self, *a):
                pass

        self.models = models
        self.chat_status = chat_status
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def test_ollama_check_finds_missing_model():
    fake = FakeOllama(["qwen2.5:7b"])
    try:
        OllamaChat("qwen2.5:7b", fake.url).check()
        with pytest.raises(LLMError, match="qwen2.5:3b がありません.*qwen2.5:7b"):
            OllamaChat("qwen2.5:3b", fake.url).check()
    finally:
        fake.close()


def test_ollama_404_explains_missing_model():
    fake = FakeOllama(["qwen2.5:3b"], chat_status=404)
    try:
        with pytest.raises(LLMError, match="ollama pull"):
            OllamaChat("qwen2.5:3b", fake.url).chat([{"role": "user", "content": "x"}])
    finally:
        fake.close()


def test_ollama_chat_success():
    fake = FakeOllama(["qwen2.5:3b"])
    try:
        assert OllamaChat("qwen2.5:3b", fake.url).chat([{"role": "user", "content": "x"}]) == "OK"
    finally:
        fake.close()


def test_eval_script_against_api(api, tmp_path):
    """評価スクリプトがこのAPIの形をそのまま採点できること。"""
    sys.path.insert(0, str(ROOT / "eval"))
    import run_eval

    def reply(messages):
        return "週3日までです[1]。" if "在宅勤務" in messages[1]["content"].split("質問:")[-1] else "わかりません"

    base = api(FakeLLM(reply))
    out = tmp_path / "r.json"
    code = run_eval.main(["--endpoint", f"{base}/ask", "--only", "Q01,D01,U01", "--out", str(out)])
    summary = json.loads(out.read_text(encoding="utf-8"))["summary"]
    assert code == 0 and summary["passed"] == 3 and summary["permission_violations"] == 0
