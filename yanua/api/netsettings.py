"""通信とモデル（S-53。K-052）：どの宛先に・いつ通信してよいかの状態。

- 宛先の一覧と目録は config/network.json（既定。画面からは書き換えない）
- 画面で変えるのは「状態」だけ：既定の宛先の ON/OFF、追加の宛先、更新の確認の予定、入れ替えた版。
  保存は yanua/settingfiles.py を通す（既定 config/network_state.json → 写し data/settings/network_state.json。版・既定に戻す・監査ログ）
- 既定の宛先の決まり：
    初めて入れたとき（写しがないとき）は、既定の宛先をすべて ON で始める（起動のときに写しを作り、そのときの既定を覚える）
    更新で既定の宛先が増えたら、新しいものは OFF で加え、画面で知らせる（ON にするかは管理者が決める）
    更新で既定の宛先が減ったら、その宛先には通信しない（写しに ON が残っていても使わない）。画面に「既定から外れました」
    既定の宛先は消せない（ON/OFF だけ）。追加の宛先は足す・消す・ON/OFF ができる
- 「既定に戻す」は写しを消す：既定の宛先がすべて ON、追加の宛先なし、更新の確認は手動だけ
- network.json に誤りがあっても起動は続ける。そのときは社外にはどこにも通信しない（理由を画面に出す）
"""

from __future__ import annotations

import ipaddress
import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from ..netpolicy import Policy
from ..settingfiles import SettingFile

STATE_NAME = "network_state.json"
MODES = {"none": "しない", "manual": "手動だけ", "daily": "毎日", "weekly": "毎週"}
WEEKDAYS = ["月", "火", "水", "木", "金", "土", "日"]
_LABEL = r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?"
HOSTNAME = re.compile(rf"^(?=.{{1,253}}$){_LABEL}(?:\.{_LABEL})+$")
REVISION = re.compile(r"^[0-9a-f]{40}$")
STATE_KEYS = ("known_defaults", "off", "added", "revisions", "update_check")


# ------------------------------------------------------------ 既定（config/network.json）


def load_defaults(path: Path) -> tuple[dict | None, str]:
    """(既定, 誤り)。誤りがあれば既定は None（社外にはどこにも通信しない）。"""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except OSError as e:
        return None, f"{path} を読めません（{e}）"
    except ValueError as e:
        return None, f"{path} を JSON として読めません（{getattr(e, 'lineno', '?')}行目 {getattr(e, 'colno', '?')}文字目）"
    hosts = data.get("allowed_hosts") if isinstance(data, dict) else None
    if not isinstance(hosts, list) or not all(isinstance(h, str) and h.strip() for h in hosts):
        return None, f"{path} の allowed_hosts は、宛先の文字の一覧にしてください"
    if not isinstance(data.get("models", {}), dict) or not isinstance(data.get("purposes", {}), dict):
        return None, f"{path} の models と purposes は {{名前: …}} の形にしてください"
    for name, spec in data.get("models", {}).items():
        if not isinstance(spec, dict) or (spec.get("kind", "huggingface") == "huggingface" and not spec.get("repo")):
            return None, f"{path} の models「{name}」の書き方が正しくありません"
    return data, ""


def covered(host: str, rules: list[str]) -> bool:
    return Policy(rules).allows(f"https://{host}/")


# ------------------------------------------------------------ 状態の検証


def state_problems(text: str, default_hosts: list[str]) -> list[tuple[str, str]]:
    try:
        data = json.loads(text)
    except ValueError as e:
        return [(f"{getattr(e, 'lineno', '?')}行目 {getattr(e, 'colno', '?')}文字目", "JSON として読めません")]
    if not isinstance(data, dict):
        return [("", "{\"項目\": 値} の形にしてください")]
    out = [(k, f"{k} という項目はありません") for k in data if not k.startswith("_") and k not in STATE_KEYS]
    kd = data.get("known_defaults")
    if kd is not None and (not isinstance(kd, list) or not all(isinstance(h, str) for h in kd)):
        out.append(("known_defaults", "覚えた既定の宛先は、宛先の一覧にしてください"))
    off = data.get("off", [])
    if not isinstance(off, list) or not all(isinstance(h, str) for h in off):
        out.append(("off", "OFF にした宛先は、宛先の一覧にしてください"))
    added = data.get("added", [])
    seen: set[str] = set()
    if not isinstance(added, list):
        out.append(("added", "追加の宛先は一覧にしてください"))
        added = []
    for i, a in enumerate(added):
        at = f"added[{i}]"
        if not isinstance(a, dict) or not isinstance(a.get("host"), str):
            out.append((at, "宛先（host）を書いてください"))
            continue
        host = a["host"].strip().lower()
        if "://" in host or "/" in host or ":" in host:
            out.append((at, "https のホスト名だけを書いてください（例：example.com。https:// やパス・ポートは書かない）"))
        elif "*" in host:
            out.append((at, "追加の宛先に、ワイルドカード（*）は使えません"))
        elif _is_ip(host):
            out.append((at, "IP アドレスは使えません。ホスト名を書いてください"))
        elif not HOSTNAME.match(host):
            out.append((at, "ホスト名の書き方が正しくありません（例：example.com）"))
        elif host in seen:
            out.append((at, f"{host} は、追加の宛先に2回あります"))
        elif host in default_hosts or covered(host, default_hosts):
            out.append((at, f"{host} は既定の宛先と重なっています（既定の宛先は ON/OFF で切り替えます）"))
        seen.add(host)
        if not isinstance(a.get("on", True), bool):
            out.append((at, "ON か OFF にしてください"))
        if not isinstance(a.get("purpose", ""), str) or len(a.get("purpose", "")) > 100:
            out.append((at, "用途は100文字までの文にしてください"))
    revs = data.get("revisions", {})
    if not isinstance(revs, dict) or not all(isinstance(v, str) and REVISION.match(v) for v in revs.values()):
        out.append(("revisions", "版はコミットのハッシュ（16進40文字）にしてください"))
    uc = data.get("update_check", {})
    if not isinstance(uc, dict) or uc.get("mode") not in MODES:
        out.append(("update_check", f"更新の確認は {'・'.join(MODES.values())} のどれかにしてください"))
    elif uc["mode"] in ("daily", "weekly"):
        if not isinstance(uc.get("time"), str) or not re.fullmatch(r"([01]\d|2[0-3]):[0-5]\d", uc["time"]):
            out.append(("update_check.time", "時刻は 00:00〜23:59 で書いてください"))
        if uc["mode"] == "weekly" and (isinstance(uc.get("weekday"), bool) or uc.get("weekday") not in range(7)):
            out.append(("update_check.weekday", "曜日を選んでください"))
    return out


def _is_ip(host: str) -> bool:
    try:
        ipaddress.ip_address(host.strip("[]"))
        return True
    except ValueError:
        return bool(re.fullmatch(r"[\d.]+", host))


def store(settings, default_hosts: list[str], audit=None) -> SettingFile:
    return SettingFile(STATE_NAME, Path(settings.network_state_config), settings.settings_dir,
                       lambda text: state_problems(text, default_hosts), audit=audit, audit_action="network")


def read_state(st: SettingFile) -> dict:
    return read_state_text(st.read_text())


def read_state_text(text: str) -> dict:
    data = json.loads(text)
    return {"known_defaults": data.get("known_defaults"), "off": list(data.get("off", [])),
            "added": [{"host": a["host"].strip().lower(), "on": a.get("on", True), "purpose": a.get("purpose", "")}
                      for a in data.get("added", [])],
            "revisions": dict(data.get("revisions", {})),
            "update_check": {"mode": "manual", "time": "03:00", "weekday": 0, **data.get("update_check", {})}}


def text_of(state: dict) -> str:
    body = {"_説明": "通信の設定の状態（S-53）。画面で保存したもの", **{k: state[k] for k in STATE_KEYS}}
    return json.dumps(body, ensure_ascii=False, indent=2) + "\n"


# ------------------------------------------------------------ 画面に出す形・動いている許可リスト


def view(defaults: dict | None, state: dict) -> dict:
    """宛先の一覧（既定・追加）と知らせ。"""
    if defaults is None:
        return {"hosts": [], "new_defaults": [], "removed_defaults": []}
    current = defaults["allowed_hosts"]
    purposes = defaults.get("purposes", {})
    known = current if state["known_defaults"] is None else state["known_defaults"]
    hosts = []
    for h in current:
        new = h not in known
        hosts.append({"host": h, "kind": "default", "purpose": purposes.get(h, ""), "new": new,
                      "on": (not new) and h not in state["off"]})
    for a in state["added"]:
        hosts.append({"host": a["host"], "kind": "added", "purpose": a["purpose"], "new": False, "on": a["on"]})
    removed = [h for h in known if h not in current]
    return {"hosts": hosts, "new_defaults": [{"host": h["host"], "purpose": h["purpose"]} for h in hosts if h["new"]],
            "removed_defaults": removed}


def effective(defaults: dict | None, state: dict) -> Policy | None:
    """動いている許可リスト（ON の宛先だけ）と、目録（入れ替えた版を入れたもの）。network.json が読めなければ None。"""
    if defaults is None:
        return None
    allowed = [h["host"] for h in view(defaults, state)["hosts"] if h["on"]]
    models = {name: dict(spec) for name, spec in defaults.get("models", {}).items()}
    for name, rev in state["revisions"].items():
        if name in models and models[name].get("kind", "huggingface") == "huggingface":
            models[name]["revision"] = rev
    return Policy(allowed, models)


def first_state(defaults: dict) -> dict:
    """初めて入れたときの状態：今の既定の宛先をすべて ON で覚える。"""
    return {"known_defaults": list(defaults["allowed_hosts"]), "off": [], "added": [], "revisions": {},
            "update_check": {"mode": "manual", "time": "03:00", "weekday": 0}}


def from_screen(defaults: dict, state: dict, body: dict) -> dict:
    """画面から来た ON/OFF・追加の宛先・更新の確認から、保存する状態を作る（見た既定の宛先は、知ったことにする）。"""
    current = defaults["allowed_hosts"]
    on = body.get("defaults", {})
    if not isinstance(on, dict):
        on = {}
    known = current if state["known_defaults"] is None else state["known_defaults"]
    off = [h for h in current if not on.get(h, h in known and h not in state["off"])]
    return {"known_defaults": list(current), "off": off,
            "added": body.get("added", state["added"]), "revisions": state["revisions"],
            "update_check": body.get("update_check", state["update_check"])}


def schedule_text(uc: dict) -> str:
    mode = uc.get("mode", "manual")
    if mode == "daily":
        return f"毎日 {uc.get('time')}"
    if mode == "weekly":
        return f"毎週{WEEKDAYS[uc.get('weekday', 0)]}曜 {uc.get('time')}"
    return MODES.get(mode, mode)


def describe_changes(defaults: dict | None, old: dict, new: dict) -> str:
    """例「huggingface.co を OFF、example.com を追加、更新の確認 手動だけ→毎日 03:00」。宛先・予定・版だけを書く。"""
    parts = []
    before = {h["host"]: h for h in view(defaults, old)["hosts"]}
    after = {h["host"]: h for h in view(defaults, new)["hosts"]}
    for host, h in after.items():
        b = before.get(host)
        if b is None or (b["kind"] == "default" and b["new"] and h["kind"] == "default"):
            if h["kind"] == "added":
                parts.append(f"{host} を追加" + ("" if h["on"] else "（OFF）"))
            elif b is not None and h["on"]:
                parts.append(f"{host} を ON")
            continue
        if b["on"] != h["on"]:
            parts.append(f"{host} を {'ON' if h['on'] else 'OFF'}")
    for host, b in before.items():
        if host not in after and b["kind"] == "added":
            parts.append(f"{host} を消した")
    if schedule_text(old["update_check"]) != schedule_text(new["update_check"]):
        parts.append(f"更新の確認 {schedule_text(old['update_check'])}→{schedule_text(new['update_check'])}")
    for name in sorted(set(old["revisions"]) | set(new["revisions"])):
        a, b = old["revisions"].get(name), new["revisions"].get(name)
        if a != b:
            parts.append(f"{name} の版 {(a or '既定')[:12]}→{(b or '既定')[:12]}")
    return "、".join(parts) or "変更なし"


# ------------------------------------------------------------ 更新の確認の予定


def due(uc: dict, last_run: datetime | None, now: datetime) -> bool:
    """決まった時刻を過ぎて、まだその回を見に行っていなければ True。「しない」「手動だけ」は、いつも False。"""
    mode = uc.get("mode", "manual")
    if mode not in ("daily", "weekly"):
        return False
    hh, mm = map(int, str(uc.get("time", "03:00")).split(":"))
    slot = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    if mode == "daily":
        if slot > now:
            slot -= timedelta(days=1)
    else:
        slot -= timedelta(days=(now.weekday() - int(uc.get("weekday", 0))) % 7)
        if slot > now:
            slot -= timedelta(days=7)
    return last_run is None or last_run < slot
