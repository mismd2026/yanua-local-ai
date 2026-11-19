"""「前後で同じことができる」を確かめるための、権限の結果の写し（S-54 の移し替え。docs/access-model.md）。

デモの人事データの全員について、次を集める（HTTP の API を呼ぶだけ。中の仕組みには触らない）：
- login      ログインできるか
- permissions /api/me の権限（名前は今の名前にそろえる。RENAMES）
- api        代表の API の返事の番号（権限で断られたら 403。中身は見ない）
- documents  見られる文書の番号、文書を開けるか、検索で出る文書

移し替える前のコードで作ったもの（tests/golden/access_before.json）と、今のコードで作ったものを比べる。
ゴールデンを作り直すとき：python tests/access_snapshot.py （移し替える前のコードで1回だけ作った）
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

GOLDEN = Path(__file__).resolve().parent / "golden" / "access_before.json"
EMPLOYEES = ["10001", "10002", "10003", "10004", "10005", "10006", "10007"]
# 移し替えで名前を変えた権限（docs/access-model.md の表にそろえた）：新しい名前 → 前の名前
RENAMES = {"documents.view": "documents.list", "documents.manage": "documents.ingest", "helpdesk.handle": "tickets"}
SEARCHES = ["採用面接の進め方", "月次決算", "障害対応", "在宅勤務", "備品の購入", "バックアップ", "支払処理", "人事評価"]

# 代表の API（それぞれの require(...) と、ほかの確かめを通る場所）。中身を変えない呼び方にしてある
PROBES: list[tuple[str, str, object]] = [
    ("GET", "/api/me", None),
    ("POST", "/api/ask", {}),
    ("POST", "/api/ask/stream", {}),
    ("GET", "/api/search?q=x", None),
    ("GET", "/api/documents", None),
    ("GET", "/api/documents/x", None),
    ("GET", "/api/documents/ingest/sources", None),
    ("POST", "/api/documents/ingest", {}),
    ("POST", "/api/chat/sessions", {"flow": "x"}),
    ("POST", "/api/chat/sessions/x/messages", {}),
    ("DELETE", "/api/chat/sessions/x", None),
    ("GET", "/api/jobs", None),
    ("GET", "/api/jobs?all=true", None),
    ("GET", "/api/jobs/x", None),
    ("DELETE", "/api/jobs/x", None),
    ("GET", "/api/notifications", None),
    ("POST", "/api/notifications/read", {}),
    ("GET", "/api/visits", None),
    ("GET", "/api/visits?host=all", None),
    ("GET", "/api/visits?date=2026-11-16", None),
    ("POST", "/api/visits", {}),
    ("DELETE", "/api/visits/x", None),
    ("POST", "/api/visits/import", {}),
    ("GET", "/api/reception/records", None),
    ("POST", "/api/reception/records/x/done", {}),
    ("GET", "/api/tickets", None),
    ("GET", "/api/tickets/x", None),
    ("PATCH", "/api/tickets/x", {}),
    ("GET", "/api/admin/kiosk-devices", None),
    ("POST", "/api/admin/kiosk-devices", {}),
    ("DELETE", "/api/admin/kiosk-devices/x", None),
    ("POST", "/api/uploads?kind=pdf&name=a.pdf", None),
    ("POST", "/api/uploads?kind=recording&name=a.mp4", None),
    ("GET", "/api/files/x", None),
    ("GET", "/api/paper/inbox", None),
    ("GET", "/api/paper/rules", None),
    ("GET", "/api/paper/plans/x", None),
    ("PUT", "/api/paper/plans/x/items/0", {}),
    ("POST", "/api/paper/plans/x/execute", {}),
    ("GET", "/api/paper/history", None),
    ("POST", "/api/paper/rule-drafts", {}),
    ("POST", "/api/paper/rules/validate", {}),
    ("POST", "/api/paper/rules", {}),
    ("GET", "/api/paper/rules/status", None),
    ("GET", "/api/paper/rules/history", None),
    ("GET", "/api/paper/rules/history/99", None),
    ("POST", "/api/paper/rules/history/99/restore", {}),
    ("POST", "/api/paper/convert", {}),
    ("GET", "/api/meetings", None),
    ("POST", "/api/meetings", {}),
    ("GET", "/api/meetings/x", None),
    ("PUT", "/api/meetings/x", {}),
    ("GET", "/api/meetings/x/minutes.docx", None),
    ("GET", "/api/admin/settings", None),
    ("GET", "/api/admin/settings/audit", None),
    ("POST", "/api/admin/settings/audit/validate", {"values": {}}),
    ("PUT", "/api/admin/settings/audit", {"values": {}}),
    ("POST", "/api/admin/settings/audit/history/99/restore", {}),
    ("POST", "/api/admin/settings/identity/try", {}),
    ("GET", "/api/admin/network", None),
    ("POST", "/api/admin/network/validate", {"values": {"added": [{"host": "x"}]}}),
    ("PUT", "/api/admin/network", {"values": {"added": [{"host": "x"}]}}),
    ("POST", "/api/admin/network/history/99/restore", {}),
    ("GET", "/api/admin/models", None),
    ("POST", "/api/admin/models/x/replace", {}),
    ("GET", "/api/audit", None),
]


def build_settings(tmp: Path):
    from test_api import DEMO
    from yanua import hr
    from yanua.api import Settings
    from yanua.store import Store

    db = tmp / "index.db"
    s = Store(db)
    assert s.ingest(DEMO / "docs").errors == []
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
    from yanua.store import Store

    settings = build_settings(tmp)
    all_docs = sorted(d["doc_id"] for d in Store(settings.db_path).documents())
    out = {}
    with TestClient(create_app(settings, FakeLLM(), ollama_tags=lambda: {}, model_catalog=lambda: [])) as c:
        for no in EMPLOYEES:
            c.cookies.clear()
            r = c.post("/api/auth/dev-login", json={"employee_no": no}, headers=H)
            row: dict = {"login": r.status_code}
            if r.status_code != 200:
                out[no] = row
                continue
            me = c.get("/api/me").json()
            row["permissions"] = sorted({RENAMES.get(p, p) for p in me["permissions"]})
            api = {}
            for method, path, body in PROBES:
                kw = {"headers": H}
                if body is not None:
                    kw["json"] = body
                elif method in ("POST", "PUT", "PATCH"):
                    kw["content"] = b""
                api[f"{method} {path}"] = c.request(method, path, **kw).status_code
            row["api"] = api
            listed = c.get("/api/documents")
            row["documents"] = {
                "listed": sorted(d["doc_id"] for d in listed.json()["documents"]) if listed.status_code == 200 else listed.status_code,
                "open": {d: c.get(f"/api/documents/{d}").status_code for d in all_docs},
                "search": {q: [h["doc_id"] for h in c.get("/api/search", params={"q": q}).json().get("hits", [])]
                           for q in SEARCHES},
            }
            out[no] = row
    return out


if __name__ == "__main__":
    import tempfile

    with tempfile.TemporaryDirectory() as d:
        data = snapshot(Path(d))
    GOLDEN.parent.mkdir(parents=True, exist_ok=True)
    GOLDEN.write_text(json.dumps(data, ensure_ascii=False, indent=1, sort_keys=True) + "\n", encoding="utf-8")
    print(f"{GOLDEN} を書きました（{len(data)}人）")
