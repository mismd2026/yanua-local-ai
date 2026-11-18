"""設定の画面（S-52。K-042）の API のテスト。Ollama は使わない（入っているモデルの一覧は偽のものを渡す）。

- 管理者だけ（ほかは 403）
- 保存の前に検証し、誤りは項目の位置つきで返す。誤りがあれば保存しない（壊れた設定が残らない）
- 保存・版に戻す・既定に戻すは、再起動なしで効き、監査ログに残る
- 起動の指定（--audit-body など）が勝つ項目は、画面では変えられない
"""

import json
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api import H, FakeLLM, login  # noqa: E402
from test_api import settings as base_settings  # noqa: E402,F401  （fixture を使う）

from yanua import identity, models  # noqa: E402
from yanua.api import Settings, adminsettings, create_app  # noqa: E402
from yanua.netpolicy import Policy  # noqa: E402

ADMIN, GA, EMP = "10005", "10002", "10004"
# 目録（config/network.json の models にあたるもの）と、手元の Ollama に入っているもの（名前 → digest）
CATALOG = {
    "whisper-small": {"repo": "Systran/faster-whisper-small", "revision": ""},
    "qwen2.5:3b-instruct": {"kind": "ollama", "use": ["chat", "summary"]},
    "qwen2.5:7b": {"kind": "ollama", "use": ["chat", "summary"]},
    "llama3.2:3b": {"kind": "ollama", "use": ["chat"]},              # 目録にあるが、Ollama に入っていない
    "gemma2:2b": {"kind": "ollama", "use": ["chat", "summary"]},     # 入っているが、まだ登録していない
    "mistral:7b": {"kind": "ollama", "use": ["chat", "summary"]},    # 登録したあとに入れ替わった
    "nomic-embed-text": {"kind": "ollama", "use": ["embedding"]},   # 埋め込み用
}
TAGS = {"qwen2.5:3b-instruct": "a" * 64, "qwen2.5:7b": "b" * 64, "gemma2:2b": "c" * 64, "mistral:7b": "d" * 64,
        "nomic-embed-text:latest": "e" * 64, "complaint-agent:latest": "f" * 64}   # complaint-agent は目録にない


@pytest.fixture
def settings(base_settings, tmp_path):
    base_settings.models_dir = tmp_path / "models"
    for name in ("qwen2.5:3b-instruct", "qwen2.5:7b", "mistral:7b", "nomic-embed-text"):
        tags = dict(TAGS, **{"mistral:7b": "0" * 64}) if name == "mistral:7b" else TAGS
        models.register_ollama(name, base_settings.models_dir, tags)
    return base_settings


def catalog_of(settings):
    return lambda: models.catalog(Policy([], CATALOG), settings.models_dir, TAGS)


def make(settings, catalog=None):
    return TestClient(create_app(settings, FakeLLM(), model_catalog=catalog or catalog_of(settings)))


@pytest.fixture
def client(settings):
    with make(settings) as c:
        login(c, ADMIN)
        yield c


def section(client, key) -> dict:
    r = client.get(f"/api/admin/settings/{key}")
    assert r.status_code == 200, r.text
    return r.json()


def put(client, key, values):
    return client.put(f"/api/admin/settings/{key}", json={"values": values}, headers=H)


def where(r) -> dict:
    assert r.status_code == 400, r.text
    return {d["where"]: d["message"] for d in r.json()["error"]["details"]}


def audit_actions(client, prefix="settings") -> list[str]:
    return [e["action"] for e in client.get("/api/audit", params={"action": prefix, "per_page": 200}).json()["entries"]]


# ---------------------------------------------------------------- 権限・一覧


def test_only_admins(settings):
    with make(settings) as c:
        for who in (EMP, GA):
            login(c, who)
            for method, path, body in (("GET", "/api/admin/settings", None), ("GET", "/api/admin/settings/audit", None),
                                       ("PUT", "/api/admin/settings/audit", {"values": {}}),
                                       ("POST", "/api/admin/settings/audit/reset", {}),
                                       ("POST", "/api/admin/settings/identity/try", {"values": {}, "said": "x"})):
                assert c.request(method, path, json=body, headers=H).status_code == 403, (who, path)
        login(c, ADMIN)
        assert c.get("/api/admin/settings").status_code == 200
        assert c.get("/api/admin/settings/roles").status_code == 404          # 扱わない設定はない
        assert "settings.view" in c.get("/api/me").json()["permissions"]


def test_list_shows_four_sections_using_defaults(client):
    r = client.get("/api/admin/settings").json()
    assert [s["key"] for s in r["sections"]] == ["identity", "audit", "models", "files"]
    assert all(s["status"]["using"] == "default" and s["applied"] for s in r["sections"])
    assert r["pending_restart"] == []
    assert r["links"][0]["path"] == "/paper/sort"
    by = {s["key"]: s for s in r["sections"]}
    assert by["audit"]["values"] == {"keep_days": 365, "keep_body": False}
    assert by["files"]["values"] == {"work_max_gb": 20, "file_keep_hours": 24}
    assert by["identity"]["values"]["preset"] == "標準"
    # 秘密の値はこの画面に出さない
    text = json.dumps(r, ensure_ascii=False).lower()
    assert "token" not in text and "password" not in text and "パスワード" not in text


def test_config_defaults_match_built_in_defaults():
    s = Settings()
    assert json.loads((ROOT / "config" / "audit.json").read_text(encoding="utf-8"))["keep_days"] == s.audit_keep_days
    assert json.loads((ROOT / "config" / "audit.json").read_text(encoding="utf-8"))["keep_body"] == s.audit_keep_body
    models = json.loads((ROOT / "config" / "models.json").read_text(encoding="utf-8"))
    assert (models["chat"], models["summary"]) == (s.chat_model, s.summary_model)
    from yanua.llm import DEFAULT_MODEL
    assert s.chat_model == DEFAULT_MODEL
    files = json.loads((ROOT / "config" / "files.json").read_text(encoding="utf-8"))
    assert files["work_max_gb"] * 1_000_000_000 == s.work_max_bytes and files["file_keep_hours"] == s.file_keep_hours


# ---------------------------------------------------------------- 本人確認の方針


def test_identity_validation_and_save_take_effect_without_restart(client, settings):
    default_text = (ROOT / "config" / "identity.json").read_text(encoding="utf-8")
    meta = section(client, "identity")["meta"]
    assert set(meta["presets"]) == set(identity.PRESETS) and meta["presets"]["社員コードを使わない"]["clues"] == ["name", "dept"]

    bad = {"preset": "社員コードを使わない", "clues": ["name"], "confirm": [["name", "dept"]], "max_follow_ups": 5,
           "question": " ", "surname_counts": "はい"}
    problems = where(client.post("/api/admin/settings/identity/validate", json={"values": bad}, headers=H))
    assert set(problems) == {"confirm", "max_follow_ups", "question", "surname_counts"}
    assert "使う手がかりにないもの" in problems["confirm"] and "0〜3" in problems["max_follow_ups"]
    assert set(where(put(client, "identity", bad))) == set(problems)
    assert set(where(put(client, "identity", {"preset": "なんでも"}))) == {"preset"}
    assert set(where(put(client, "identity", {"preset": "標準", "token": "x"}))) == {"token"}
    assert not (settings.settings_dir / "identity.json").exists()             # 誤りがあれば何も書かない

    good = {**meta["presets"]["社員コードを使わない"], "preset": "社員コードを使わない", "surname_counts": True, "max_follow_ups": 1}
    good.pop("description")
    saved = put(client, "identity", good)
    assert saved.status_code == 200, saved.text
    assert saved.json()["version"] == 2 and saved.json()["status"]["using"] == "saved" and saved.json()["applied"]
    assert (ROOT / "config" / "identity.json").read_text(encoding="utf-8") == default_text      # 既定は書き換えない
    assert identity.load_policy(settings.settings_dir / "identity.json").preset == "社員コードを使わない"

    # 再起動しなくても、ヘルプデスクの聞き方が新しい方針になる（共用アカウントから始めると本人確認を聞く）
    first = client.post("/api/chat/sessions", json={"account": "eigyo-common"}, headers=H).json()
    assert identity.PRESETS["社員コードを使わない"]["question"] in first["reply"]

    # 保存したものは、起動のときの検証（誤りがあれば起動しない）も通る
    with make(settings) as again:
        login(again, ADMIN)
        assert section(again, "identity")["values"]["preset"] == "社員コードを使わない"
    assert "settings.identity.save" in audit_actions(client)


def test_hand_broken_identity_copy_still_stops_startup(settings):
    """画面からは検証を通ったものしか保存できない。手で壊した写しは、決まりどおり起動しない。"""
    settings.settings_dir.mkdir(parents=True, exist_ok=True)
    (settings.settings_dir / "identity.json").write_text('{"preset": "標準", "max_follow_ups": 9}', encoding="utf-8")
    with pytest.raises(identity.PolicyError, match="0〜3"):
        create_app(settings, FakeLLM())


def test_identity_try_shows_only_whether_identified_and_what_to_ask(client):
    standard = section(client, "identity")["values"]
    r = client.post("/api/admin/settings/identity/try", json={"values": standard, "said": "営業部の高橋美咲です"}, headers=H).json()
    assert r["identified"] is True and r["matched"] == ["名前", "部署"] and r["ask"] == [] and r["question"] == ""
    # 人事データは本物を使うが、ほかの社員の名前や番号は返さない
    text = json.dumps(r, ensure_ascii=False)
    assert "10004" not in text and "employee_no" not in text and "高橋" not in text
    no_code = {**standard, "preset": "社員コードを使わない", "clues": ["name", "dept"], "confirm": [["name", "dept"]]}
    r = client.post("/api/admin/settings/identity/try", json={"values": no_code, "said": "高橋美咲です"}, headers=H).json()
    assert r["identified"] is False and r["ask"] == ["所属部署"] and "所属部署" in r["question"]
    # 保存はしない。試した言い方は監査ログに残さない
    assert section(client, "identity")["status"]["using"] == "default"
    assert client.post("/api/admin/settings/identity/try", json={"values": {"preset": "x"}, "said": "a"}, headers=H).status_code == 400
    logs = client.get("/api/audit", params={"action": "settings.identity.try"}).json()["entries"]
    assert len(logs) == 2 and all("高橋" not in e["detail"] for e in logs)


# ---------------------------------------------------------------- 監査ログ・モデル・ファイル


def test_audit_settings_limits_and_live_change(client, settings):
    assert set(where(put(client, "audit", {"keep_days": 0, "keep_body": False}))) == {"keep_days"}
    assert "1〜3650日" in where(put(client, "audit", {"keep_days": 5000, "keep_body": False}))["keep_days"]
    assert set(where(put(client, "audit", {"keep_days": 30.5, "keep_body": "on"}))) == {"keep_days", "keep_body"}
    r = put(client, "audit", {"keep_days": 30, "keep_body": True})
    assert r.status_code == 200 and r.json()["applied"]
    assert settings.audit_keep_days == 30 and client.app.state.audit.keep_days == 30 and client.app.state.audit.keep_body
    client.post("/api/ask", json={"question": "在宅勤務は週に何日までできますか？"}, headers=H)
    asked = client.get("/api/audit", params={"action": "ask"}).json()["entries"][0]
    assert asked["body"] == "在宅勤務は週に何日までできますか？"            # 再起動しなくても、本文を残すようになった
    assert "settings.audit.save" in audit_actions(client)


def test_audit_body_flag_wins_over_the_screen(settings):
    settings.pinned = {"audit.keep_body"}           # serve --audit-body で起動した
    settings.audit_keep_body = True
    with make(settings) as c:
        login(c, ADMIN)
        s = section(c, "audit")
        assert s["pinned"] == {"keep_body": "--audit-body"} and s["values"]["keep_body"] is False and s["running"]["keep_body"] is True
        assert s["applied"]                         # 起動の指定が勝つ項目は、違っていても再起動待ちにしない
        assert "--audit-body" in where(put(c, "audit", {"keep_days": 30, "keep_body": True}))["keep_body"]
        r = put(c, "audit", {"keep_days": 30, "keep_body": False})       # ほかの項目は変えられる
        assert r.status_code == 200 and settings.audit_keep_days == 30 and settings.audit_keep_body is True


def test_models_only_from_the_catalog_and_usable(client, settings):
    meta = section(client, "models")["meta"]
    by = {m["name"]: m for m in meta["catalog"]}
    # Ollama に入っていても、目録にないもの（complaint-agent）は出さない。whisper（Ollama でない）も出さない
    assert set(by) == {"qwen2.5:3b-instruct", "qwen2.5:7b", "llama3.2:3b", "gemma2:2b", "mistral:7b", "nomic-embed-text"}
    assert by["qwen2.5:7b"]["state"] == "ok" and by["qwen2.5:7b"]["message"] == ""
    assert by["llama3.2:3b"]["state"] == "not_installed" and "入っていません" in by["llama3.2:3b"]["message"]
    assert by["gemma2:2b"]["state"] == "not_registered" and "models pull gemma2:2b" in by["gemma2:2b"]["message"]
    assert by["mistral:7b"]["state"] == "changed" and "--update" in by["mistral:7b"]["message"]
    assert by["nomic-embed-text"]["use"] == ["embedding"]

    def bad(chat, summary="qwen2.5:7b"):
        return where(put(client, "models", {"chat": chat, "summary": summary}))

    assert "目録にありません" in bad("complaint-agent")["chat"]          # API でも断る
    assert "入っていません" in bad("llama3.2:3b")["chat"]
    assert "登録されていません" in bad("gemma2:2b")["chat"]
    assert "登録した digest と違います" in bad("mistral:7b")["chat"]
    assert "チャットには使えません" in bad("nomic-embed-text")["chat"]     # 埋め込み用は使えない
    assert "議事録の要約には使えません" in bad("qwen2.5:7b", "llama3.2:3b")["summary"]
    assert not (settings.settings_dir / "models.json").exists()
    r = put(client, "models", {"chat": "qwen2.5:7b", "summary": "qwen2.5:7b"})
    assert r.status_code == 200 and settings.chat_model == "qwen2.5:7b"
    assert client.get("/health").json()["model"] == "qwen2.5:7b"           # 再起動しなくても、次の回答から使う


def test_models_cannot_be_saved_when_ollama_does_not_answer(settings):
    def down():
        raise ConnectionError("つながりません")

    with make(settings, catalog=down) as c:
        login(c, ADMIN)
        assert "問い合わせられません" in section(c, "models")["meta"]["error"]
        assert "Ollama" in where(put(c, "models", {"chat": "qwen2.5:3b-instruct", "summary": "qwen2.5:7b"}))[""]


def test_register_and_verify_ollama_models_without_network(tmp_path):
    """models pull は、Ollama のモデルなら手元の digest を登録するだけ（社外に通信しない）。verify は登録との一致を確かめる。"""
    policy = Policy([], CATALOG)       # 許可リストが空でも登録できる（通信しないため）
    entry = models.pull("qwen2.5:7b", policy, tmp_path, tags=TAGS)
    assert entry["kind"] == "ollama" and entry["digest"] == "b" * 64
    assert models.verify_ollama("qwen2.5:7b", tmp_path, tags=TAGS) == "b" * 64
    with pytest.raises(models.ModelError, match="登録した digest と違います"):
        models.verify_ollama("qwen2.5:7b", tmp_path, tags={**TAGS, "qwen2.5:7b": "9" * 64})
    with pytest.raises(models.ModelError, match="--update"):
        models.pull("qwen2.5:7b", policy, tmp_path, tags={**TAGS, "qwen2.5:7b": "9" * 64})
    assert models.pull("qwen2.5:7b", policy, tmp_path, update=True, tags={**TAGS, "qwen2.5:7b": "9" * 64})["digest"] == "9" * 64
    with pytest.raises(models.ModelError, match="ollama pull llama3.2:3b"):
        models.pull("llama3.2:3b", policy, tmp_path, tags=TAGS)
    with pytest.raises(models.ModelError, match="目録|models にありません"):
        models.pull("complaint-agent", policy, tmp_path, tags=TAGS)
    assert models.pull("nomic-embed-text", policy, tmp_path, tags=TAGS)["digest"] == "e" * 64   # 「:latest」を省いた名前


def test_repo_catalog_lists_the_default_models():
    """リポジトリの目録に、既定のチャット・要約のモデルが用途つきで入っている。許可リストは変えていない。"""
    net = json.loads((ROOT / "config" / "network.json").read_text(encoding="utf-8"))
    assert net["allowed_hosts"] == ["huggingface.co", "*.huggingface.co", "*.hf.co"]
    s = Settings()
    for name in (s.chat_model, s.summary_model):
        assert net["models"][name]["kind"] == "ollama"
    assert "chat" in net["models"][s.chat_model]["use"] and "summary" in net["models"][s.summary_model]["use"]


def test_files_units_limits_history_restore_and_reset(client, settings):
    assert "1〜10000GB" in where(put(client, "files", {"work_max_gb": 0.5, "file_keep_hours": 24}))["work_max_gb"]
    assert "1〜720時間" in where(put(client, "files", {"work_max_gb": 20, "file_keep_hours": 1000}))["file_keep_hours"]
    assert "整数" in where(put(client, "files", {"work_max_gb": 20, "file_keep_hours": 1.5}))["file_keep_hours"]
    assert put(client, "files", {"work_max_gb": 50, "file_keep_hours": 48}).status_code == 200
    assert settings.work_max_bytes == 50_000_000_000 and settings.file_keep_hours == 48
    assert put(client, "files", {"work_max_gb": 2.5, "file_keep_hours": 12}).status_code == 200
    assert settings.work_max_bytes == 2_500_000_000
    s = section(client, "files")
    assert [v["version"] for v in s["versions"]] == [3, 2, 1] and s["current"] == 3
    assert s["versions"][0]["saved_by_name"] == "田中 健"
    r = client.post("/api/admin/settings/files/history/2/restore", json={}, headers=H)
    assert r.status_code == 200 and r.json()["version"] == 4 and settings.work_max_bytes == 50_000_000_000
    r = client.post("/api/admin/settings/files/reset", json={}, headers=H).json()
    assert r["version"] == 5 and r["status"]["using"] == "default" and settings.work_max_bytes == 20_000_000_000
    assert client.post("/api/admin/settings/files/reset", json={}, headers=H).json()["already_default"] is True
    assert client.post("/api/admin/settings/files/history/99/restore", json={}, headers=H).status_code == 404
    logs = client.get("/api/audit", params={"action": "settings.files", "per_page": 50}).json()["entries"]
    assert [(e["action"], e["result"]) for e in logs] == [
        ("settings.files.restore", "失敗"),          # ない版に戻そうとした（版 99）
        ("settings.files.reset", "成功"), ("settings.files.restore", "成功"),
        ("settings.files.save", "成功"), ("settings.files.save", "成功")]


def test_hand_edited_copy_waits_for_restart_and_broken_copy_falls_back(client, settings):
    assert put(client, "files", {"work_max_gb": 50, "file_keep_hours": 48}).status_code == 200
    # 写しを手で直した（正しい形）：動いている値と違うので、再起動待ちとして知らせる
    (settings.settings_dir / "files.json").write_text('{"work_max_gb": 30, "file_keep_hours": 48}', encoding="utf-8")
    r = client.get("/api/admin/settings").json()
    assert r["pending_restart"] == ["files"]
    with make(settings) as again:                    # 起動し直すと使われる
        login(again, ADMIN)
        assert settings.work_max_bytes == 30_000_000_000 and again.get("/api/admin/settings").json()["pending_restart"] == []
    # 写しを手で壊した：その設定だけ既定で動き、起動は止めない。画面には直すところを出す
    (settings.settings_dir / "audit.json").write_text('{"keep_days": -1, "keep_body": false}', encoding="utf-8")
    settings.audit_keep_days = 365
    with make(settings) as again:
        login(again, ADMIN)
        s = section(again, "audit")
        assert settings.audit_keep_days == 365 and s["values"] is None and s["problems"][0]["where"] == "keep_days"


# ---------------------------------------------------------------- 変えた項目の説明（版の「したこと」と監査ログ）


def test_versions_and_audit_log_describe_what_changed(client):
    standard = section(client, "identity")["values"]
    p = identity.PRESETS["社員コード必須"]
    r = put(client, "identity", {**standard, "preset": "社員コード必須", "question": p["question"], "clues": p["clues"],
                                 "confirm": p["confirm"], "max_follow_ups": 1})
    assert r.status_code == 200, r.text
    assert put(client, "models", {"chat": "qwen2.5:7b", "summary": "qwen2.5:7b"}).status_code == 200
    assert put(client, "files", {"work_max_gb": 50, "file_keep_hours": 24}).status_code == 200
    assert put(client, "audit", {"keep_days": 30, "keep_body": True}).status_code == 200

    ident = section(client, "identity")["versions"][0]["action"]
    assert ident.startswith("本人確認の方針を保存した（型 標準→社員コード必須、最初の聞き方を変えた、")
    assert "確定する組み合わせ 名前＋従業員番号・名前＋部署・従業員番号＋部署→従業員番号＋名前・従業員番号＋部署" in ident
    assert "聞き返しの上限 2回→1回" in ident and "使う手がかり" not in ident     # 並べ方だけ違う手がかりは書かない
    assert section(client, "models")["versions"][0]["action"] == "モデルを保存した（チャットのモデル qwen2.5:3b-instruct→qwen2.5:7b）"
    assert section(client, "files")["versions"][0]["action"] == "ファイル（作業用フォルダ）を保存した（作業用フォルダの上限 20GB→50GB）"
    assert section(client, "audit")["versions"][0]["action"] == "監査ログを保存した（保存期間 365日→30日、質問の本文 残さない→残す）"

    # 版に戻す・既定に戻すも、戻したことで変わる項目を書く
    assert client.post("/api/admin/settings/models/history/1/restore", json={}, headers=H).status_code == 200
    assert section(client, "models")["versions"][0]["action"] == "版 1 に戻した（チャットのモデル qwen2.5:7b→qwen2.5:3b-instruct）"
    assert client.post("/api/admin/settings/files/reset", json={}, headers=H).status_code == 200
    assert section(client, "files")["versions"][0]["action"] == "既定に戻した（画面で保存した写しを消した。作業用フォルダの上限 50GB→20GB）"
    # 同じ説明が監査ログにも入る
    details = {e["action"]: e["detail"] for e in reversed(client.get("/api/audit", params={"action": "settings", "per_page": 200}).json()["entries"])}
    assert details["settings.models.save"] == "models.json：モデルを保存した（チャットのモデル qwen2.5:3b-instruct→qwen2.5:7b）（版 2）"
    assert details["settings.models.restore"] == "models.json：版 1 に戻した（チャットのモデル qwen2.5:7b→qwen2.5:3b-instruct）（版 3）"
    assert details["settings.files.reset"] == "files.json：既定に戻した（作業用フォルダの上限 50GB→20GB）（版 3）"
    assert "型 標準→社員コード必須" in details["settings.identity.save"]


def test_describe_changes_only_known_fields_and_no_change():
    d = adminsettings.describe_changes
    assert d("files", {"work_max_gb": 20, "file_keep_hours": 24}, {"work_max_gb": 20.0, "file_keep_hours": 24}) == "変更なし"
    assert d("files", {"work_max_gb": 20, "file_keep_hours": 24}, {"work_max_gb": 2.5, "file_keep_hours": 48}) == \
        "作業用フォルダの上限 20GB→2.5GB、ファイルを消すまで 24時間→48時間"
    # 決まった項目のほかの値（たとえ紛れ込んでも）は説明に出さない
    assert d("models", {"chat": "a", "summary": "b", "token": "x"}, {"chat": "a", "summary": "b", "token": "yk_secret"}) == "変更なし"
    assert d("identity", {"surname_counts": False}, {"surname_counts": True}) == "姓だけを数える いいえ→はい"
    assert d("audit", None, {"keep_days": 1}) == ""
