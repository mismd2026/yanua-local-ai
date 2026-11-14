"""打ち合わせの記録と、通信の方針・モデルの入手のテスト。

文字起こしのモデル（faster-whisper）は CI では入手しないので、文字起こしは台本の発言（demo_zoom_segments.json）か
偽の文字起こしで代える。話し手の読み取り（画面のOCR）は、デモの動画と、会議ツール風の画面で確かめる。
"""

import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "demo" / "company" / "meeting"))

from yanua import hr, models  # noqa: E402
from yanua.meeting import run as meeting_run  # noqa: E402
from yanua.meeting.speakers import PRESETS, clean_label, label_segments, match_name, speaker_from_frame  # noqa: E402
from yanua.meeting.summarize import chunks, summarize  # noqa: E402
from yanua.meeting.transcribe import Segment, build_prompt  # noqa: E402
from yanua.netpolicy import NetworkBlocked, Policy, load_policy  # noqa: E402
from yanua.store import Store  # noqa: E402

MEETING = ROOT / "demo" / "company" / "meeting"
VIDEO = MEETING / "demo_zoom.mp4"
SEGMENTS = MEETING / "demo_zoom_segments.json"
TEMPLATE = MEETING / "templates" / "議事録.docx"
HR_DIR = ROOT / "demo" / "company" / "hr"
PEOPLE = ["佐藤 花子", "田中 健", "山田 太郎", "本社5F会議室"]


def has_ocr_and_font() -> bool:
    try:
        import fakeui

        fakeui.font(10)
        out = subprocess.run(["tesseract", "--list-langs"], capture_output=True, text=True)
        return "jpn" in out.stdout
    except (FileNotFoundError, OSError):
        return False


needs_ocr = pytest.mark.skipif(not has_ocr_and_font(), reason="tesseract（日本語）か日本語フォントがない環境")


class FakeLLM:
    """会議の読み取りには決まったJSONを返す（担当者に、会議に出てこない名前を1つ混ぜる）。"""

    name = "fake"

    def __init__(self):
        self.prompts = []

    def chat(self, messages):
        text = messages[-1]["content"]
        self.prompts.append(text)
        if '"points"' in text:
            return json.dumps({
                "points": ["ネットワーク工事の日程", "複合機の移設"],
                "decisions": ["工事は12月第2週の土曜日に行う"],
                "todos": [{"who": "田中", "what": "当日の作業手順をまとめる", "due": "来週の金曜日"},
                          {"who": "鈴木 次郎", "what": "予算を確認する", "due": ""},
                          {"who": "佐藤", "what": "リース会社に連絡する", "due": ""}],
            }, ensure_ascii=False)
        return json.dumps({"summary": "本社移転の工事日程と複合機の移設を話し合い、工事日を決めた。"}, ensure_ascii=False)


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "index.db")
    hr.import_employees(s.db, HR_DIR / "hr_export_sample.csv", hr.load_mapping(HR_DIR / "mapping_sample.json"))
    yield s
    s.close()


# ---------------------------------------------------------------- 通信の方針


def test_policy_allows_only_listed_https_hosts():
    p = Policy(["huggingface.co", "*.hf.co"])
    assert p.allows("https://huggingface.co/Systran/faster-whisper-small")
    assert p.allows("https://cdn-lfs.hf.co/x")
    assert not p.allows("http://huggingface.co/x")                 # https だけ
    assert not p.allows("https://huggingface.co.evil.example/x")    # 名前の後ろに足した偽物
    assert not p.allows("https://evilhuggingface.co/x")
    assert not p.allows("https://hf.co/x")                          # *.hf.co は hf.co そのものを含まない
    with pytest.raises(NetworkBlocked):
        p.check("https://example.com/model.bin")


def test_default_policy_file_is_valid():
    p = load_policy()
    assert "whisper-small" in p.models and p.allows("https://huggingface.co/")


def test_hf_session_is_guarded_including_redirects():
    import huggingface_hub
    from huggingface_hub.utils._http import default_client_factory, get_session

    from yanua.netpolicy import guarded_hf_session

    guarded_hf_session(Policy(["huggingface.co"]))
    try:
        client = get_session()
        hook = client.event_hooks["request"][-1]
        hook(client.build_request("GET", "https://huggingface.co/api/models/x"))
        with pytest.raises(NetworkBlocked):   # リダイレクト先も、1件ずつ同じ確認を通る
            hook(client.build_request("GET", "https://cdn.evil.example/model.bin"))
    finally:
        huggingface_hub.set_client_factory(default_client_factory)


# ---------------------------------------------------------------- モデルの入手と照合


def fake_downloader(content=b"weights"):
    def download(repo_id, revision, local_dir):
        d = Path(local_dir)
        (d / ".cache").mkdir(parents=True, exist_ok=True)
        (d / "model.bin").write_bytes(content)
        (d / "config.json").write_text("{}", encoding="utf-8")
        (d / ".cache" / "tmp").write_text("x", encoding="utf-8")
    return download


def test_pull_records_hashes_and_verify_detects_tampering(tmp_path):
    policy = Policy(["huggingface.co"], {"whisper-small": {"repo": "Systran/faster-whisper-small", "revision": "abc123"}})
    entry = models.pull("whisper-small", policy, tmp_path, downloader=fake_downloader())
    assert entry["revision"] == "abc123" and set(entry["files"]) == {"model.bin", "config.json"}
    assert models.verify("whisper-small", tmp_path) == tmp_path / "whisper-small"
    (tmp_path / "whisper-small" / "model.bin").write_bytes(b"tampered")
    with pytest.raises(models.ModelError, match="すり替え"):
        models.verify("whisper-small", tmp_path)


def test_pull_is_refused_for_unknown_model_or_blocked_host(tmp_path):
    with pytest.raises(models.ModelError):
        models.pull("whisper-huge", Policy(["huggingface.co"], {}), tmp_path, downloader=fake_downloader())
    blocked = Policy(["pypi.org"], {"whisper-small": {"repo": "Systran/faster-whisper-small", "revision": "a"}})
    with pytest.raises(NetworkBlocked):
        models.pull("whisper-small", blocked, tmp_path, downloader=fake_downloader())
    with pytest.raises(models.ModelError, match="models pull"):
        models.verify("whisper-small", tmp_path)


def test_locked_revision_is_reused(tmp_path):
    policy = Policy(["huggingface.co"], {"m": {"repo": "a/b", "revision": ""}})
    models.write_lock(tmp_path, {"m": {"repo": "a/b", "revision": "first", "path": "m", "files": {}}})
    seen = {}

    def download(repo_id, revision, local_dir):
        seen["revision"] = revision
        fake_downloader()(repo_id, revision, local_dir)

    models.pull("m", policy, tmp_path, downloader=download)
    assert seen["revision"] == "first"   # 記録した版を使う（勝手に新しい版にしない）


# ---------------------------------------------------------------- 文字起こしのヒント


def test_prompt_uses_glossary_departments_and_names_within_limit():
    p = build_prompt(["稟議", "SASE"], ["佐藤 花子"] * 3 + [f"社員{i}" for i in range(100)], ["総務部", "営業部"])
    assert p.startswith("稟議、SASE、総務部、営業部、佐藤 花子") and len(p) <= 220
    assert p.count("佐藤 花子") == 1


# ---------------------------------------------------------------- 話し手（画面の名前）


def test_clean_and_match_names():
    zoom = PRESETS["zoom"]
    assert clean_label("| 高橋 美咲 (ホスト)\n", zoom) == "高橋 美咲"
    assert match_name("高橋美咲", ["高橋 美咲"]) == "高橋 美咲"
    assert match_name("高稿 美咲", ["高橋 美咲", "田中 健"]) == "高橋 美咲"     # OCRの小さな読み違いを直す
    assert match_name("本社5F会議室", ["高橋 美咲"]) == "本社5F会議室"         # 人事データにない名前はそのまま
    assert match_name("", ["高橋 美咲"]) == "不明"


@needs_ocr
@pytest.mark.parametrize("tool", ["zoom", "teams", "meet"])
@pytest.mark.parametrize("layout", ["speaker", "gallery"])
def test_speaker_from_meeting_screens(tool, layout):
    import fakeui

    for active, name in enumerate(PEOPLE):
        image = fakeui.render(tool, layout, PEOPLE, active)
        assert speaker_from_frame(image, PRESETS[tool], PEOPLE) == name, (tool, layout, name)


@needs_ocr
def test_speakers_from_demo_video():
    data = json.loads(SEGMENTS.read_text(encoding="utf-8"))
    segs = [Segment(s["start"], s["end"], s["text"]) for s in data]
    found = label_segments(VIDEO, segs, "zoom", ["佐藤 花子", "田中 健", "山田 太郎", "高橋 美咲", "本社5F会議室"])
    assert found == len(segs)
    assert [s.speaker for s in segs] == [s["expected"] for s in data]


# ---------------------------------------------------------------- 要約と議事録


def test_chunks_split_long_meetings():
    segs = [Segment(i * 10, i * 10 + 9, "あ" * 300, "佐藤 花子") for i in range(20)]
    parts = chunks(segs, size=2400)
    assert len(parts) > 1 and all(len(p) <= 2400 + 400 for p in parts)
    assert "".join(parts).count("佐藤 花子") == 20


def test_summary_drops_invented_assignees():
    segs = meeting_run.load_segments(SEGMENTS)
    for s, name in zip(segs, ["佐藤 花子", "田中 健", "山田 太郎", "田中 健", "佐藤 花子", "田中 健", "本社5F会議室", "佐藤 花子"]):
        s.speaker = name
    result = summarize(FakeLLM(), segs, "本社移転の定例会", ["佐藤 花子", "田中 健"])
    whos = {t["what"]: t["who"] for t in result.todos}
    assert whos["当日の作業手順をまとめる"] == "田中 健"   # 「田中」を出席者の表記にそろえる
    assert whos["予算を確認する"] == "（未定）"           # 会議に出てこない名前は担当者にしない
    assert whos["リース会社に連絡する"] == "佐藤 花子"
    assert result.decisions == ["工事は12月第2週の土曜日に行う"] and result.summary   # 同じ決定は重ねない
    assert result.next_meeting == "十一月二十六日"


class SilentLLM:
    """小さなモデルが「決まったこと」や「私が〜します」を落としたときの代わり。"""

    def chat(self, messages):
        if '"points"' in messages[-1]["content"]:
            return json.dumps({"points": ["工事の日程"], "decisions": [],
                               "todos": [{"who": "田中 健", "what": "当日の作業手順をまとめる", "due": ""}]}, ensure_ascii=False)
        return json.dumps({"summary": "工事の日程を話し合った。"}, ensure_ascii=False)


def test_rules_fill_what_small_models_miss():
    segs = meeting_run.load_segments(SEGMENTS)
    for s, name in zip(segs, ["佐藤 花子", "田中 健", "山田 太郎", "田中 健", "佐藤 花子", "田中 健", "本社5F会議室", "佐藤 花子"]):
        s.speaker = name
    result = summarize(SilentLLM(), segs, "本社移転の定例会", ["佐藤 花子", "田中 健", "山田 太郎"])
    assert result.decisions == ["工事は十二月の第二週の土曜日で決定します"]
    todos = {t["what"]: (t["who"], t["due"]) for t in result.todos}
    assert todos["当日の作業手順をまとめる"] == ("田中 健", "来週の金曜日")   # 期限は担当者のその後の発言から
    assert todos["リース会社に連絡する"] == ("佐藤 花子", "")               # 「私が〜します」は話し手が担当


@needs_ocr
def test_meeting_end_to_end_with_template(store, tmp_path):
    from docx import Document

    result = meeting_run.run(
        VIDEO, title="本社移転の定例会", out_dir=tmp_path, llm=FakeLLM(), segments=meeting_run.load_segments(SEGMENTS),
        tool="zoom", names=meeting_run.company_words(store.db)[0], participants=["本社5F会議室"], template=TEMPLATE)
    md = next(f for f in result.files if f.suffix == ".md").read_text(encoding="utf-8")
    assert "佐藤 花子、田中 健、山田 太郎、本社5F会議室" in md        # 人事データの表記にそろう（発言の多い順）
    assert "[00:06] 田中 健：ネットワーク工事" in md
    doc = Document(next(f for f in result.files if f.suffix == ".docx"))
    tables = [[[c.text for c in r.cells] for r in t.rows] for t in doc.tables]
    assert tables[0][3][1].startswith("佐藤 花子")
    assert tables[1][1] == ["田中 健", "当日の作業手順をまとめる", "来週の金曜日"]
    assert tables[0][4] == ["次回", "十一月二十六日"] and "- 次回：十一月二十六日" in md
    assert any("・工事は12月第2週の土曜日に行う" in p.text for p in doc.paragraphs)


def test_transcriber_receives_company_words(tmp_path):
    class FakeTranscriber:
        def transcribe(self, media, prompt):
            self.prompt = prompt
            return [Segment(0, 2, "テスト")], {"duration": 2, "elapsed": 1}

    t = FakeTranscriber()
    meeting_run.run(VIDEO, title="t", out_dir=tmp_path, transcriber=t, tool="none", names=["高橋 美咲"], depts=["営業部"],
                    glossary=["稟議"])
    assert t.prompt == "稟議、営業部、高橋 美咲"
