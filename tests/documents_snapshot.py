"""文書の「前後で同じ」テスト（S-50）のための書き出し。

文書の公開先を「部署だけ」から「公開先（部署・グループ・全社）× 要る権限」に移し替える前に、デモの7人について
「見られる文書（一覧）・開ける文書・検索で出る文書・質問の出典と、部署の権限で見せなかった文書の件数」を書き出す。
ログインしない使い方（コマンドの search・照合できなかったヘルプデスク・社外向けの受付）も、部署ごとに書き出す。

    python tests/documents_snapshot.py      # tests/golden/documents_before.json を書く（移し替える前のコードで1回だけ）
"""

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

GOLDEN = Path(__file__).resolve().parent / "golden" / "documents_before.json"
EMPLOYEES = ["10001", "10002", "10003", "10004", "10005", "10006", "10007"]
SEARCHES = ["採用面接の進め方", "月次決算", "障害対応", "在宅勤務", "備品の購入", "バックアップ", "支払処理", "人事評価",
            "会社案内", "個人情報の取り扱い", "就業規則", "管理者アカウント", "パスワード"]
QUESTIONS = ["在宅勤務は週に何日までできますか？", "月次決算の手順を教えてください", "パスワードを忘れたときは？", "人事評価の時期は？"]
DEPARTMENTS = ["", "経理部", "総務部", "営業部", "情報システム部", "人事部"]


def build_settings(tmp: Path):
    from test_api import DEMO
    from yanua import hr
    from yanua.api import Settings
    from yanua.store import Store

    db = tmp / "index.db"
    s = Store(db)
    assert s.ingest(DEMO / "docs").errors == []
    assert s.ingest(DEMO / "public").errors == []
    hr.import_employees(s.db, DEMO / "hr" / "hr_export_sample.csv", hr.load_mapping(DEMO / "hr" / "mapping_sample.json"))
    s.close()
    return Settings(db_path=db, chat_db_path=tmp / "chat.db", helpdesk_data=DEMO / "flows" / "helpdesk",
                    roles_path=ROOT / "config" / "roles.json", dev_login=True, ingest_roots=[DEMO], web_dist=None,
                    settings_dir=tmp / "settings", models_dir=tmp / "models", work_dir=tmp / "work",
                    paper_out=tmp / "sorted")


def snapshot(tmp: Path) -> dict:
    from fastapi.testclient import TestClient
    from test_api import H, FakeLLM

    from yanua.api import create_app
    from yanua.search import search, search_public
    from yanua.store import Store

    settings = build_settings(tmp)
    all_docs = sorted(d["doc_id"] for d in Store(settings.db_path).documents())
    people: dict = {}
    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])) as c:
        for no in EMPLOYEES:
            c.cookies.clear()
            r = c.post("/api/auth/dev-login", json={"employee_no": no}, headers=H)
            row: dict = {"login": r.status_code}
            if r.status_code != 200:
                people[no] = row
                continue
            listed = c.get("/api/documents")
            row["listed"] = sorted(d["doc_id"] for d in listed.json()["documents"]) if listed.status_code == 200 else listed.status_code
            row["open"] = {d: c.get(f"/api/documents/{d}").status_code for d in all_docs}
            row["search"] = {q: [h["doc_id"] for h in c.get("/api/search", params={"q": q}).json().get("hits", [])] for q in SEARCHES}
            asks = {}
            for q in QUESTIONS:
                a = c.post("/api/ask", json={"question": q}, headers=H).json()
                asks[q] = {"refused": a["refused"], "sources": [s["doc_id"] for s in a["sources"]]}
            row["ask"] = asks
            people[no] = row
        # 質問の監査ログ（部署の権限で見せなかった文書の件数・結果）。管理者で読む
        c.cookies.clear()
        c.post("/api/auth/dev-login", json={"employee_no": "10005"}, headers=H)
        entries = c.get("/api/audit", params={"action": "ask.chat", "per_page": 200}).json()["entries"]
        audit_rows = sorted([e["actor"], e["result"], e["target"], e["detail"]] for e in entries)
    store = Store(settings.db_path)
    try:
        by_dept = {d or "（部署なし）": {q: [h.doc_id for h in search(store, q, d, 5)] for q in SEARCHES} for d in DEPARTMENTS}
        public = {q: [h.doc_id for h in search_public(store, q, 5)] for q in SEARCHES}
    finally:
        store.close()
    return {"people": people, "audit": audit_rows, "by_department": by_dept, "public": public}


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        data = snapshot(Path(d))
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{GOLDEN} を書きました（{len(data['people'])}人）")
