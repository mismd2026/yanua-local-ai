import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

from yanua.documents import DocumentError, load_document, split_chunks  # noqa: E402
from yanua.search import search, tokenize  # noqa: E402
from yanua.store import Store  # noqa: E402

DEMO_DOCS = ROOT / "demo" / "company" / "docs"
DEPARTMENTS = ("総務部", "人事部", "経理部", "情報システム部", "営業部")


def write_doc(folder: Path, name: str, doc_id: str, access: str, body: str, title: str = "テスト文書") -> Path:
    path = folder / name
    head = f"---\nid: {doc_id}\ntitle: {title}\n"
    if access is not None:
        head += f"access: {access}\n"
    path.write_text(head + "---\n\n" + body, encoding="utf-8")
    return path


@pytest.fixture
def demo_store(tmp_path):
    store = Store(tmp_path / "index.db")
    report = store.ingest(DEMO_DOCS)
    assert report.errors == []
    yield store
    store.close()


# ---------------------------------------------------------------- 文書の読み込み


def test_document_without_access_is_rejected(tmp_path):
    path = write_doc(tmp_path, "x.md", "X01", None, "## 見出し\n本文")
    with pytest.raises(DocumentError, match="access"):
        load_document(path)


def test_document_without_id_is_rejected(tmp_path):
    path = tmp_path / "x.md"
    path.write_text("---\naccess: 全社\n---\n本文", encoding="utf-8")
    with pytest.raises(DocumentError, match="id"):
        load_document(path)


def test_access_accepts_multiple_departments(tmp_path):
    path = write_doc(tmp_path, "x.md", "X01", "人事部, 経理部", "## 見出し\n本文")
    assert load_document(path).access == ("人事部", "経理部")


def test_chunks_follow_headings_and_carry_title():
    chunks = split_chunks("規程", "# 規程\n\n## 第1章\nあ\n\n## 第2章\nい\n")
    assert [c.section for c in chunks] == ["第1章", "第2章"]
    assert chunks[0].text.startswith("規程 / 第1章")


def test_long_section_is_split_by_paragraph():
    body = "## 長い節\n" + "\n\n".join("段落" * 100 for _ in range(5))
    chunks = split_chunks("規程", body, max_chars=450)
    assert len(chunks) > 1
    assert all(c.section == "長い節" for c in chunks)


# ---------------------------------------------------------------- 取り込み


def test_ingest_demo_documents(demo_store):
    docs = demo_store.documents()
    assert len(docs) == 20
    assert next(d for d in docs if d["doc_id"] == "H01")["access"] == ["人事部"]


def test_second_ingest_changes_nothing(demo_store):
    report = demo_store.ingest(DEMO_DOCS)
    assert len(report.unchanged) == 20
    assert report.added == report.updated == report.deleted == []


def test_only_changed_documents_are_reindexed(tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    write_doc(src, "a.md", "A", "全社", "## 節\n在宅勤務は週3日まで")
    write_doc(src, "b.md", "B", "全社", "## 節\n有給休暇は10日")
    store = Store(tmp_path / "index.db")
    assert sorted(store.ingest(src).added) == ["A", "B"]

    write_doc(src, "a.md", "A", "全社", "## 節\n在宅勤務は週2日まで")
    report = store.ingest(src)
    assert report.updated == ["A"] and report.unchanged == ["B"]

    hits = search(store, "在宅勤務は週何日", "営業部", 1)
    assert "週2日" in hits[0].text and "週3日" not in hits[0].text


def test_removed_documents_are_deleted(tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    write_doc(src, "a.md", "A", "全社", "## 節\n本文A")
    write_doc(src, "b.md", "B", "全社", "## 節\n本文B")
    store = Store(tmp_path / "index.db")
    store.ingest(src)
    (src / "b.md").unlink()
    report = store.ingest(src)
    assert report.deleted == ["B"]
    assert [d["doc_id"] for d in store.documents()] == ["A"]
    assert store.db.execute("SELECT COUNT(*) FROM chunks WHERE doc_id = 'B'").fetchone()[0] == 0


def test_access_change_takes_effect_on_reingest(tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    write_doc(src, "a.md", "A", "全社", "## 節\n合言葉はみかん")
    store = Store(tmp_path / "index.db")
    store.ingest(src)
    assert search(store, "合言葉", "営業部")

    write_doc(src, "a.md", "A", "人事部", "## 節\n合言葉はみかん")
    store.ingest(src)
    assert search(store, "合言葉", "営業部") == []
    assert search(store, "合言葉", "人事部")


def test_duplicate_id_is_reported_and_skipped(tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    write_doc(src, "a.md", "A", "全社", "## 節\n本文1")
    write_doc(src, "b.md", "A", "全社", "## 節\n本文2")
    store = Store(tmp_path / "index.db")
    report = store.ingest(src)
    assert report.added == ["A"]
    assert any("重複" in e for e in report.errors)


def test_document_without_access_is_not_ingested(tmp_path):
    src = tmp_path / "docs"
    src.mkdir()
    write_doc(src, "a.md", "A", None, "## 節\n秘密の本文")
    store = Store(tmp_path / "index.db")
    report = store.ingest(src)
    assert report.added == [] and report.errors
    assert search(store, "秘密の本文", "営業部") == []


def test_cli_ingest_and_search(tmp_path, capsys):
    from yanua.__main__ import main

    db = tmp_path / "index.db"
    assert main(["--db", str(db), "ingest", str(DEMO_DOCS)]) == 0
    capsys.readouterr()
    assert main(["--db", str(db), "search", "在宅勤務は週に何日まで？", "--department", "営業部", "--top-k", "1"]) == 0
    assert '"doc_id": "A01"' in capsys.readouterr().out


# ---------------------------------------------------------------- 検索と権限


def test_tokenize_handles_japanese_and_ascii():
    tokens = tokenize("HD-GUESTに接続")
    assert "hd-guest" in tokens and "接続" in tokens


def test_search_finds_the_right_document(demo_store):
    hits = search(demo_store, "在宅勤務は週に何日までできますか？", "営業部", 1)
    assert hits[0].doc_id == "A01" and hits[0].section == "在宅勤務"


def test_department_sees_its_own_restricted_documents(demo_store):
    hits = search(demo_store, "人事評価の入力期限はいつですか？", "人事部", 1)
    assert hits[0].doc_id == "H01"


@pytest.mark.parametrize("department", DEPARTMENTS + ("", "存在しない部署"))
def test_hidden_documents_never_appear(demo_store, department):
    """部署限定の文書の中身そのものを質問にしても、見られない部署には出てこない。"""
    restricted = {d["doc_id"]: d["access"] for d in demo_store.documents() if d["access"] != ["全社"]}
    for doc_id, access in restricted.items():
        if department in access:
            continue
        text = (next(DEMO_DOCS.glob(f"{doc_id}_*.md"))).read_text(encoding="utf-8")
        hits = search(demo_store, text, department, 20)
        assert all(h.doc_id not in restricted or department in restricted[h.doc_id] for h in hits)


def test_unknown_department_sees_only_company_wide_documents(demo_store):
    ids = {c.doc_id for c in demo_store.chunks_for("存在しない部署")}
    assert ids and all(i.startswith("A") for i in ids)


def test_can_view(demo_store):
    assert demo_store.can_view("H01", "人事部")
    assert not demo_store.can_view("H01", "営業部")
    assert demo_store.can_view("A01", "営業部")


# ---------------------------------------------------------------- 質問セットでの評価


def test_retrieval_quality_on_question_set():
    import retrieval_eval

    summary = retrieval_eval.evaluate(top_k=5)["summary"]
    assert summary["permission_violations"] == 0
    # 品質が下がったことに気づくための下限。改善したら引き上げる。
    assert summary["source_hit_rate"] >= 0.95
    assert summary["context_answer_rate"] >= 0.95
