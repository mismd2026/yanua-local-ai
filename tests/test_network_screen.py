"""通信とモデル（S-53。K-052）のテスト。社外には通信しない（つなぐところ net._open を偽のものに替える）。

- 宛先：既定は消せない（ON/OFF だけ）・OFF の宛先には通信しない・更新で増えた既定は OFF で加わって知らせる・減った既定は使わない
- 追加の宛先：https のホスト名だけ（パス・ワイルドカード・IP・重複は断る）
- 更新の確認：ON の宛先だけを見に行く。「しない」は時刻になっても通信しない（時計をまねる）。入れ替えは押したときだけ
- モデルの目録：照合（通信しない）。入れ替えは 入手 → 入手元のハッシュと照合 → 記録を更新。失敗したら元のまま
"""

import hashlib
import json
import shutil
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_api import H, FakeLLM, login  # noqa: E402
from test_api import settings as base_settings  # noqa: E402,F401  （fixture を使う）

from yanua import models, net  # noqa: E402
from yanua.api import create_app, netsettings  # noqa: E402
from yanua.netpolicy import NetworkBlocked, Policy  # noqa: E402

ADMIN, GA, EMP = "10005", "10002", "10004"
PINNED = "536b0662742c02347bc0e980a01041f333bce120"
NEWER = "f" * 40
JST = timezone(timedelta(hours=9))


class FakeNet:
    """社外の代わり。呼ばれた URL を覚え、決めた返事を返す（宛先の確かめは net.fetch が先にする）。"""

    def __init__(self):
        self.calls: list[str] = []
        self.latest = NEWER
        self.files: dict[str, bytes] = {"model.bin": b"new-model", "config.json": b"{}"}
        self.manifest = b'{"layers": []}'

    def __call__(self, url, policy, headers, timeout):
        self.calls.append(url)
        if "registry.ollama.ai" in url:
            return self.manifest
        if "/revision/" in url:
            sib = []
            for name, data in self.files.items():
                if name == "model.bin":
                    sib.append({"rfilename": name, "lfs": {"sha256": hashlib.sha256(data).hexdigest()}})
                else:
                    sib.append({"rfilename": name, "blobId": hashlib.sha1(b"blob %d\0" % len(data) + data).hexdigest()})
            return json.dumps({"siblings": sib}).encode()
        return json.dumps({"sha": self.latest}).encode()


@pytest.fixture
def fake_net(monkeypatch):
    f = FakeNet()
    monkeypatch.setattr(net, "_open", f)
    return f


@pytest.fixture
def settings(base_settings, tmp_path):
    cfg = tmp_path / "network.json"
    shutil.copy2(ROOT / "config" / "network.json", cfg)
    base_settings.network_config = cfg
    base_settings.models_dir = tmp_path / "models"
    # 手元の whisper-small（偽のファイル）を、固定した版で入手済みにしておく
    local = base_settings.models_dir / "whisper-small"
    local.mkdir(parents=True)
    (local / "model.bin").write_bytes(b"old-model")
    models.record(base_settings.models_dir, "whisper-small", "Systran/faster-whisper-small", PINNED, local)
    return base_settings


class Clock:
    def __init__(self, t):
        self.t = t

    def __call__(self):
        return self.t


def downloader(fake: FakeNet):
    def download(repo_id, revision, local_dir):
        d = Path(local_dir)
        d.mkdir(parents=True, exist_ok=True)
        for name, data in fake.files.items():
            (d / name).write_bytes(data)
    return download


def make(settings, fake=None, clock=None):
    return TestClient(create_app(settings, FakeLLM(), model_downloader=downloader(fake or FakeNet()), clock=clock,
                                 ollama_tags=lambda: {"qwen2.5:7b": "b" * 64}))


@pytest.fixture
def client(settings, fake_net):
    with make(settings, fake_net) as c:
        login(c, ADMIN)
        yield c


def get(client) -> dict:
    r = client.get("/api/admin/network")
    assert r.status_code == 200, r.text
    return r.json()


def put(client, **body):
    return client.put("/api/admin/network", json={"values": body}, headers=H)


def hosts(view) -> dict:
    return {h["host"]: h for h in view["hosts"]}


def details(r) -> dict:
    assert r.status_code == 400, r.text
    return {d["where"]: d["message"] for d in r.json()["error"]["details"]}


def audit_rows(client, action):
    return client.get("/api/audit", params={"action": action, "per_page": 200}).json()["entries"]


# ---------------------------------------------------------------- 権限・初めての起動


def test_only_admins(settings, fake_net):
    with make(settings, fake_net) as c:
        for who in (EMP, GA):
            login(c, who)
            for method, path in (("GET", "/api/admin/network"), ("PUT", "/api/admin/network"), ("POST", "/api/admin/network/check"),
                                 ("GET", "/api/admin/models"), ("POST", "/api/admin/models/verify"),
                                 ("POST", "/api/admin/models/whisper-small/replace"), ("POST", "/api/admin/network/reset")):
                assert c.request(method, path, json={"values": {}}, headers=H).status_code == 403, (who, path)
        login(c, ADMIN)
        assert "network.edit" in c.get("/api/me").json()["permissions"]


def test_first_start_turns_all_defaults_on(client, settings):
    v = get(client)
    assert [(h["host"], h["kind"], h["on"]) for h in v["hosts"]] == [
        ("huggingface.co", "default", True), ("*.huggingface.co", "default", True), ("*.hf.co", "default", True)]
    assert v["new_defaults"] == [] and v["removed_defaults"] == [] and v["error"] == ""
    assert hosts(v)["huggingface.co"]["purpose"].startswith("モデルの入手")
    assert v["update_check"]["mode"] == "manual" and v["schedule"] == "手動だけ"
    assert (settings.settings_dir / "network_state.json").is_file()
    init = audit_rows(client, "network.init")
    assert len(init) == 1 and init[0]["actor"] == "システム"


# ---------------------------------------------------------------- 宛先


def test_default_hosts_cannot_be_removed_only_switched(client):
    assert "消せません" in details(put(client, defaults={"huggingface.co": None}))["defaults.huggingface.co"]
    assert "既定の宛先ではありません" in details(put(client, defaults={"example.com": True}))["defaults.example.com"]
    # 追加の宛先として既定と同じもの（ワイルドカードで含まれるものも）は足せない
    msgs = details(put(client, added=[{"host": "huggingface.co"}, {"host": "cdn.hf.co"}]))
    assert all("既定の宛先と重なっています" in m for m in msgs.values()) and len(msgs) == 2
    assert [h["host"] for h in get(client)["hosts"]][:3] == ["huggingface.co", "*.huggingface.co", "*.hf.co"]


def test_added_host_validation(client):
    bad = details(put(client, added=[{"host": "https://example.com/x"}, {"host": "*.example.com"}, {"host": "192.168.1.10"},
                                     {"host": "example"}, {"host": "ok.example.com"}, {"host": "ok.example.com"}]))
    assert "ホスト名だけ" in bad["added[0]"] and "ワイルドカード" in bad["added[1]"] and "IP アドレス" in bad["added[2]"]
    assert "書き方" in bad["added[3]"] and "2回" in bad["added[5]"] and "added[4]" not in bad
    r = put(client, added=[{"host": "registry.ollama.ai", "on": True, "purpose": "Ollama の入手元との照合"}],
            defaults={"huggingface.co": True, "*.huggingface.co": True, "*.hf.co": False})
    assert r.status_code == 200, r.text
    v = r.json()
    assert hosts(v)["registry.ollama.ai"] == {"host": "registry.ollama.ai", "kind": "added", "purpose": "Ollama の入手元との照合",
                                              "new": False, "on": True}
    assert v["versions"][0]["action"] == "通信の設定を保存した（*.hf.co を OFF、registry.ollama.ai を追加）"
    assert any(e["detail"].endswith("（*.hf.co を OFF、registry.ollama.ai を追加）（版 3）") for e in audit_rows(client, "network.save"))
    # 追加の宛先は消せる
    r = put(client, added=[])
    assert "registry.ollama.ai を消した" in r.json()["versions"][0]["action"]


def test_off_hosts_are_never_contacted(client, settings, fake_net):
    assert put(client, defaults={"huggingface.co": False, "*.huggingface.co": True, "*.hf.co": True}).status_code == 200
    r = client.post("/api/admin/network/check", headers=H).json()
    whisper = {x["name"]: x for x in r["results"]}["whisper-small"]
    assert whisper["result"] == "skipped" and "OFF なので" in whisper["message"]
    assert fake_net.calls == []                                     # 1回もつないでいない
    # 1か所の入口（net.fetch）は、OFF の宛先・許可していない宛先を、つなぐ前に断る
    policy = Policy(["*.huggingface.co"])
    with pytest.raises(NetworkBlocked):
        net.fetch("https://huggingface.co/api/models/a/b", policy)
    with pytest.raises(NetworkBlocked):
        net.fetch("https://evil.example.com/", policy)
    with pytest.raises(NetworkBlocked):
        net.fetch("https://huggingface.co/", None)                  # network.json が読めないときは、どこにもつながない
    assert fake_net.calls == []
    with pytest.raises(NetworkBlocked):
        models.pull("whisper-medium", Policy(["*.huggingface.co"], {"whisper-medium": {"repo": "a/b", "revision": NEWER}}),
                    settings.models_dir, downloader=lambda **kw: pytest.fail("入手しないはず"))


def test_new_default_hosts_start_off_and_are_announced_removed_ones_are_not_used(client, settings):
    cfg = json.loads(settings.network_config.read_text(encoding="utf-8"))
    cfg["allowed_hosts"] = ["huggingface.co", "*.huggingface.co", "cdn.example.org"]     # *.hf.co が外れ、cdn.example.org が増えた
    cfg["purposes"]["cdn.example.org"] = "新しい配信元"
    settings.network_config.write_text(json.dumps(cfg, ensure_ascii=False), encoding="utf-8")
    v = get(client)
    assert v["new_defaults"] == [{"host": "cdn.example.org", "purpose": "新しい配信元"}]
    assert hosts(v)["cdn.example.org"]["on"] is False and hosts(v)["cdn.example.org"]["new"] is True
    assert v["removed_defaults"] == ["*.hf.co"] and "*.hf.co" not in hosts(v)
    app_policy = netsettings.effective(cfg, netsettings.read_state_text((settings.settings_dir / "network_state.json").read_text()))
    assert not app_policy.allows("https://cdn.example.org/") and not app_policy.allows("https://x.hf.co/")
    # ON にするかは管理者が決める
    r = put(client, defaults={"huggingface.co": True, "*.huggingface.co": True, "cdn.example.org": True})
    assert r.status_code == 200 and r.json()["new_defaults"] == [] and hosts(r.json())["cdn.example.org"]["on"]
    assert "cdn.example.org を ON" in r.json()["versions"][0]["action"]


def test_reset_turns_defaults_on_and_removes_added(client):
    put(client, defaults={"huggingface.co": False, "*.huggingface.co": True, "*.hf.co": True},
        added=[{"host": "registry.ollama.ai", "on": True}], update_check={"mode": "daily", "time": "04:00", "weekday": 0})
    r = client.post("/api/admin/network/reset", json={}, headers=H).json()
    assert all(h["on"] for h in r["hosts"]) and [h["kind"] for h in r["hosts"]] == ["default"] * 3
    assert r["update_check"]["mode"] == "manual"
    actions = [v["action"] for v in r["versions"]]
    assert actions[0] == "既定に戻した：今の既定の宛先を覚えた"
    assert actions[1].startswith("既定に戻した（画面で保存した写しを消した。huggingface.co を ON、registry.ollama.ai を消した、更新の確認 毎日 04:00→手動だけ")


def test_broken_network_json_keeps_server_running(settings, fake_net):
    settings.network_config.write_text("{ broken", encoding="utf-8")
    with make(settings, fake_net) as c:
        login(c, ADMIN)
        v = get(c)
        assert "JSON として読めません" in v["error"] and v["hosts"] == []
        assert c.get("/api/me").status_code == 200                  # ほかの機能は使える
        assert c.post("/api/admin/network/check", headers=H).json()["results"] == []
        assert put(c, added=[]).status_code == 409
    assert fake_net.calls == []


# ---------------------------------------------------------------- 更新の確認


def test_due_follows_the_schedule():
    t = datetime(2026, 11, 18, 2, 59, tzinfo=JST)     # 水曜
    assert not netsettings.due({"mode": "none", "time": "03:00"}, None, t + timedelta(days=30))
    assert not netsettings.due({"mode": "manual"}, None, t + timedelta(days=30))
    daily = {"mode": "daily", "time": "03:00"}
    assert not netsettings.due(daily, t - timedelta(hours=1), t)
    assert netsettings.due(daily, t - timedelta(hours=1), t + timedelta(minutes=1))
    assert not netsettings.due(daily, t + timedelta(minutes=2), t + timedelta(hours=5))
    weekly = {"mode": "weekly", "time": "03:00", "weekday": 4}     # 金曜
    assert not netsettings.due(weekly, t, t + timedelta(days=1))
    assert netsettings.due(weekly, t, t + timedelta(days=2, minutes=1))


def test_none_never_contacts_even_when_time_comes(settings, fake_net):
    clock = Clock(datetime(2026, 11, 18, 2, 0, tzinfo=JST))
    with make(settings, fake_net, clock) as c:
        login(c, ADMIN)
        assert put(c, update_check={"mode": "none", "time": "03:00", "weekday": 0}).status_code == 200
        for days in (0, 1, 7, 30):
            clock.t = datetime(2026, 11, 18, 3, 30, tzinfo=JST) + timedelta(days=days)
            assert c.app.state.run_scheduled_check() is False
        assert fake_net.calls == []
        assert c.post("/api/admin/network/check", headers=H).status_code == 409     # 手で押しても見に行かない


def test_daily_check_runs_once_after_the_time_and_only_reports(settings, fake_net):
    clock = Clock(datetime(2026, 11, 18, 2, 0, tzinfo=JST))
    with make(settings, fake_net, clock) as c:
        login(c, ADMIN)
        assert put(c, update_check={"mode": "daily", "time": "03:00", "weekday": 0}).status_code == 200
        clock.t = datetime(2026, 11, 18, 2, 59, tzinfo=JST)
        assert c.app.state.run_scheduled_check() is False and fake_net.calls == []
        clock.t = datetime(2026, 11, 18, 3, 1, tzinfo=JST)
        assert c.app.state.run_scheduled_check() is True
        assert c.app.state.run_scheduled_check() is False             # 同じ回は1度だけ
        assert fake_net.calls == ["https://huggingface.co/api/models/Systran/faster-whisper-small",
                                  "https://huggingface.co/api/models/Systran/faster-whisper-medium"]
        rows = {r["name"]: r for r in c.get("/api/admin/models").json()["models"]}
        assert rows["whisper-small"]["check"] == "new" and rows["whisper-small"]["latest"] == NEWER
        # 自動では入れ替えない
        assert models.read_lock(settings.models_dir)["whisper-small"]["revision"] == PINNED
        logs = audit_rows(c, "network.check")
        assert logs and all(e["actor"] == "システム" for e in logs)
        assert any("宛先 huggingface.co：新しい版があります" in e["detail"] and NEWER[:12] in e["detail"] for e in logs)


def test_ollama_source_is_checked_only_when_registry_is_added_and_on(client, settings, fake_net):
    models.register_ollama("qwen2.5:7b", settings.models_dir, {"qwen2.5:7b": "b" * 64})
    r = client.post("/api/admin/network/check", headers=H).json()
    q = {x["name"]: x for x in r["results"]}["qwen2.5:7b"]
    assert q["result"] == "skipped" and "registry.ollama.ai を追加して ON" in q["message"]
    assert not any("registry.ollama.ai" in u for u in fake_net.calls)
    put(client, added=[{"host": "registry.ollama.ai", "on": True, "purpose": "入手元との照合"}])
    r = client.post("/api/admin/network/check", headers=H).json()
    q = {x["name"]: x for x in r["results"]}["qwen2.5:7b"]
    assert q["result"] == "differs" and "入手元と違います" in q["message"] and "ollama pull" in q["message"]
    assert "https://registry.ollama.ai/v2/library/qwen2.5/manifests/7b" in fake_net.calls
    fake_net.manifest = b"same"
    models.register_ollama("qwen2.5:7b", settings.models_dir, {"qwen2.5:7b": hashlib.sha256(b"same").hexdigest()}, update=True)
    q = {x["name"]: x for x in client.post("/api/admin/network/check", headers=H).json()["results"]}["qwen2.5:7b"]
    assert q["result"] == "same"


def test_token_from_environment_is_never_shown_or_recorded(client, fake_net, monkeypatch):
    monkeypatch.setenv("HF_TOKEN", "hf_secret_value_123")
    seen = {}
    monkeypatch.setattr(net, "_open", lambda url, policy, headers, timeout: seen.setdefault("h", net._headers(url, headers)) and fake_net(url, policy, headers, timeout))
    client.post("/api/admin/network/check", headers=H)
    assert seen["h"]["Authorization"] == "Bearer hf_secret_value_123"      # 環境変数から読んで、送るだけ
    text = client.app.state.audit.all_text() + json.dumps(get(client)) + json.dumps(client.get("/api/admin/models").json())
    assert "hf_secret_value_123" not in text


# ---------------------------------------------------------------- モデルの目録・照合・入れ替え


def test_model_list_and_verify_without_network(client, settings, fake_net):
    rows = {r["name"]: r for r in client.get("/api/admin/models").json()["models"]}
    small = rows["whisper-small"]
    assert small["kind"] == "whisper" and small["revision"] == PINNED and small["state"] == "unverified" and small["use"] == ["transcribe"]
    assert small["recorded"] == hashlib.sha256(b"old-model").hexdigest()
    assert rows["whisper-medium"]["state"] == "not_registered"
    assert rows["qwen2.5:7b"]["kind"] == "ollama" and rows["qwen2.5:7b"]["state"] == "not_registered"
    rows = {r["name"]: r for r in client.post("/api/admin/models/verify", headers=H).json()["models"]}
    assert rows["whisper-small"]["state"] == "match"
    (settings.models_dir / "whisper-small" / "model.bin").write_bytes(b"tampered")
    rows = {r["name"]: r for r in client.post("/api/admin/models/verify", headers=H).json()["models"]}
    assert rows["whisper-small"]["state"] == "differs" and "すり替え" in rows["whisper-small"]["message"]
    assert fake_net.calls == []                                        # 照合は通信しない
    logs = audit_rows(client, "models.verify")
    assert [e["result"] for e in logs][:2] == ["失敗", "成功"]


def wait(client, job_id):
    import time
    for _ in range(200):
        j = client.get(f"/api/jobs/{job_id}").json()
        if j["status"] in ("完了", "失敗", "取り消し"):
            return j
        time.sleep(0.05)
    raise AssertionError("ジョブが終わりません")


def test_replace_only_when_pressed(client, settings, fake_net):
    assert client.post("/api/admin/models/whisper-small/replace", headers=H).status_code == 409   # 確認の前は入れ替えない
    client.post("/api/admin/network/check", headers=H)
    job = client.post("/api/admin/models/whisper-small/replace", headers=H)
    assert job.status_code == 202
    done = wait(client, job.json()["job_id"])
    assert done["status"] == "完了", done
    lock = models.read_lock(settings.models_dir)["whisper-small"]
    assert lock["revision"] == NEWER and (settings.models_dir / "whisper-small" / "model.bin").read_bytes() == b"new-model"
    rows = {r["name"]: r for r in client.get("/api/admin/models").json()["models"]}
    assert rows["whisper-small"]["revision"] == NEWER                  # 固定した版も新しい版になる（画面の状態に記録）
    assert models.verify("whisper-small", settings.models_dir, revision=NEWER)
    log = audit_rows(client, "models.replace")[0]
    assert log["result"] == "成功" and f"版 {PINNED[:12]} → {NEWER[:12]}" in log["detail"]
    assert "whisper-small を版" in get(client)["versions"][0]["action"]


def test_replace_failure_keeps_the_old_model(settings, fake_net):
    bad = FakeNet()
    bad.files = {"model.bin": b"tampered-in-transit", "config.json": b"{}"}    # 入手元の記録（fake_net）と違うものが届く
    with make(settings, bad) as c:
        login(c, ADMIN)
        c.post("/api/admin/network/check", headers=H)
        done = wait(c, c.post("/api/admin/models/whisper-small/replace", headers=H).json()["job_id"])
        assert done["status"] == "失敗" and "元のモデルのまま" in done["error"]
        lock = models.read_lock(settings.models_dir)["whisper-small"]
        assert lock["revision"] == PINNED and (settings.models_dir / "whisper-small" / "model.bin").read_bytes() == b"old-model"
        assert not any(p.name.startswith(".staging") for p in settings.models_dir.iterdir())
        assert audit_rows(c, "models.replace")[0]["result"] == "失敗"
        assert {r["name"]: r for r in c.get("/api/admin/models").json()["models"]}["whisper-small"]["revision"] == PINNED
