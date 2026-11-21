"""設定の画面（S-52。K-042）で扱う設定。保存の仕組みは yanua/settingfiles.py（写し・版・既定に戻す・保存前の検証）。

今回の4つ（どれも管理者だけ。秘密の値は入っていない）：
    identity  本人確認の方針       既定 config/identity.json  → 写し data/settings/identity.json
    audit     監査ログ             既定 config/audit.json     → 写し data/settings/audit.json
    models    使うモデル           既定 config/models.json    → 写し data/settings/models.json
              選べるのは、Yanua の目録（config/network.json の models）にある Ollama のモデルで、
              その用途（use）があり、models pull で登録した digest と手元の Ollama の digest が同じものだけ
    files     作業用フォルダ       既定 config/files.json     → 写し data/settings/files.json
    hr        人事（休職中の人）   既定 config/hr.json        → 写し data/settings/hr.json（K-054）

決まり：
- 読むときは、写しがあれば写し、なければ既定（settingfiles と同じ）
- 起動の指定（--audit-body・--audit-keep-days・--model・--summary-model）があれば、そちらが勝つ。
  その項目は画面では変えられない（Settings.pinned に入る）
- どれも再起動なしで効かせる（API が保存のあとに動いている値を入れ替える）。
  動いている値と保存したものが違うとき（写しを手で直したときなど）は「再起動すると使われます」と知らせる
- 本人確認の方針は、誤りがあると起動しない（README の決まり）。ほかの3つは、誤りがあればその設定だけ使わず既定で動く
"""

from __future__ import annotations

import json
import math
from pathlib import Path
from typing import Callable

from .. import identity
from ..settingfiles import SettingError, SettingFile

SECTIONS = ("identity", "audit", "models", "files", "hr")
LABELS = {"identity": "本人確認の方針", "audit": "監査ログ", "models": "モデル", "files": "ファイル（作業用フォルダ）",
          "hr": "人事（休職中の人）"}
LEAVE_PERMISSIONS = ("そのまま", "社員の役割だけ")
KEYS = {
    "identity": identity.POLICY_KEYS,
    "audit": ("keep_days", "keep_body"),
    "models": ("chat", "summary"),
    "files": ("work_max_gb", "file_keep_hours"),
    "hr": ("on_leave_login", "on_leave_permissions"),
}
# 起動の指定が勝つ項目 → 起動の指定の名前
PIN_FLAGS = {"audit.keep_days": "--audit-keep-days", "audit.keep_body": "--audit-body",
             "models.chat": "--model", "models.summary": "--summary-model"}
# 下限と上限（単位つきで入れる）
LIMITS = {
    "audit.keep_days": (1, 3650, "日"),
    "files.work_max_gb": (1, 10000, "GB"),
    "files.file_keep_hours": (1, 720, "時間"),
}
COMMENTS = {
    "identity": "本人確認の方針（yanua/identity.py）。画面（S-52）で保存したもの",
    "audit": "監査ログの設定。画面（S-52）で保存したもの",
    "models": "使う Ollama のモデル。画面（S-52）で保存したもの",
    "files": "作業用フォルダの設定。画面（S-52）で保存したもの",
    "hr": "休職中の人の扱い。画面（S-52）で保存したもの",
}


def default_path(settings, key: str) -> Path:
    return Path(settings.identity_path) if key == "identity" else Path(getattr(settings, f"{key}_config"))


def _json_problems(text: str) -> tuple[dict | None, list[tuple[str, str]]]:
    try:
        data = json.loads(text)
    except ValueError as e:
        line, col = getattr(e, "lineno", None), getattr(e, "colno", None)
        return None, [(f"{line}行目 {col}文字目" if line else "", f"JSON として読めません（{getattr(e, 'msg', e)}）")]
    if not isinstance(data, dict):
        return None, [("", "{\"項目\": 値} の形にしてください")]
    return data, []


def _number(data: dict, key: str, where: str, integer: bool) -> list[tuple[str, str]]:
    lo, hi, unit = LIMITS[where]
    v = data.get(key)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or (isinstance(v, float) and not math.isfinite(v)):
        return [(key, f"数字で入れてください（{lo}〜{hi}{unit}）")]
    if integer and not float(v).is_integer():
        return [(key, f"整数で入れてください（{lo}〜{hi}{unit}）")]
    if not lo <= v <= hi:
        return [(key, f"{lo}〜{hi}{unit}の間にしてください")]
    return []


USES = {"chat": "チャット", "summary": "議事録の要約"}
Catalog = Callable[[], list[dict]]   # [{name, use, state, message}]（yanua/models.py の catalog）


def problems(key: str, text: str, catalog: Catalog | None = None) -> list[tuple[str, str]]:
    """読み込みと同じ検証。catalog を渡すと、モデルが目録にあり、その用途で、使える状態かも確かめる（保存・戻すとき）。"""
    data, out = _json_problems(text)
    if data is None:
        return out
    if key == "identity":
        return identity.policy_problems(data)
    for k in data:
        if not k.startswith("_") and k not in KEYS[key]:
            out.append((k, f"{k} という項目はありません（使える項目：{'・'.join(KEYS[key])}）"))
    for k in KEYS[key]:
        if k not in data:
            out.append((k, "この項目がありません"))
    if out:
        return out
    if key == "audit":
        out += _number(data, "keep_days", "audit.keep_days", integer=True)
        if not isinstance(data["keep_body"], bool):
            out.append(("keep_body", "残すか残さないか（true か false）にしてください"))
    elif key == "hr":
        if not isinstance(data["on_leave_login"], bool):
            out.append(("on_leave_login", "ログインできるか（true か false）にしてください"))
        if data["on_leave_permissions"] not in LEAVE_PERMISSIONS:
            out.append(("on_leave_permissions", f"{'か'.join(f'「{x}」' for x in LEAVE_PERMISSIONS)}にしてください"))
    elif key == "files":
        out += _number(data, "work_max_gb", "files.work_max_gb", integer=False)
        out += _number(data, "file_keep_hours", "files.file_keep_hours", integer=True)
    elif key == "models":
        for k in ("chat", "summary"):
            if not isinstance(data[k], str) or not data[k].strip():
                out.append((k, "モデルの名前を選んでください"))
        if not out and catalog is not None:
            try:
                entries = {e["name"]: e for e in catalog()}
            except Exception as e:  # noqa: BLE001  Ollama に聞けないときは保存しない（入っているか確かめられないため）
                return [("", f"Ollama に、入っているモデルを問い合わせられません（{e}）。Ollama が動いているか確かめてください")]
            for k in ("chat", "summary"):
                e = entries.get(data[k])
                if not e:
                    out.append((k, f"{data[k]} は Yanua の目録にありません（config/network.json の models に載せたモデルだけを選べます）"))
                elif k not in e["use"]:
                    out.append((k, f"{data[k]} は{USES[k]}には使えません（用途：{'・'.join(e['use']) or 'なし'}）"))
                elif e["state"] != "ok":
                    out.append((k, f"{data[k]} は使えません：{e['message']}"))
    return out


def values_of(key: str, text: str) -> dict:
    """ファイルの中身から、画面に出す値（本人確認は、型の中身を混ぜた実際の値）。"""
    data = json.loads(text)
    if key == "identity":
        p = identity.build_policy(data)
        return {"preset": p.preset, "question": p.question, "clues": p.clues, "confirm": p.confirm,
                "surname_counts": p.surname_counts, "max_follow_ups": p.max_follow_ups}
    return {k: data[k] for k in KEYS[key]}


# 変えた項目の説明に使う名前（ここに書いた項目だけを説明に出す。ほかの項目の値は出さない）
FIELD_LABELS = {
    "identity": {"preset": "型", "question": "最初の聞き方", "clues": "使う手がかり", "confirm": "確定する組み合わせ",
                 "surname_counts": "姓だけを数える", "max_follow_ups": "聞き返しの上限"},
    "audit": {"keep_days": "保存期間", "keep_body": "質問の本文"},
    "models": {"chat": "チャットのモデル", "summary": "要約のモデル"},
    "files": {"work_max_gb": "作業用フォルダの上限", "file_keep_hours": "ファイルを消すまで"},
    "hr": {"on_leave_login": "休職中の人のログイン", "on_leave_permissions": "休職中の人の権限"},
}


def _shown(key: str, field: str, v) -> str:
    if key == "identity":
        if field == "clues":
            return "・".join(identity.MATCH_LABELS.get(c, str(c)) for c in v or []) or "なし"
        if field == "confirm":
            return "・".join("＋".join(identity.MATCH_LABELS.get(c, str(c)) for c in combo) for combo in v or []) or "なし"
        if field == "surname_counts":
            return "はい" if v else "いいえ"
        if field == "max_follow_ups":
            return f"{v}回"
    if key == "audit":
        return f"{v}日" if field == "keep_days" else ("残す" if v else "残さない")
    if key == "hr" and field == "on_leave_login":
        return "できる" if v else "できない"
    if key == "files":
        return f"{v:g}GB" if field == "work_max_gb" and isinstance(v, (int, float)) else f"{v}時間" if field == "file_keep_hours" else str(v)
    return str(v)


def describe_changes(key: str, old: dict | None, new: dict | None) -> str:
    """変えた項目の短い説明（例「型 標準→社員コード必須、聞き返しの上限 2回→1回」）。版の「したこと」と監査ログに入れる。
    決まった項目だけを、決まった書き方で出す（秘密の値は扱わない。長い文の項目は「変えた」とだけ書く）。"""
    if old is None or new is None:
        return ""
    parts = []
    for field, label in FIELD_LABELS[key].items():
        a, b = old.get(field), new.get(field)
        if a == b or (isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool) and float(a) == float(b)):
            continue
        if key == "identity" and field == "question":
            parts.append("最初の聞き方を変えた")
        elif key == "identity" and field in ("clues", "confirm") and sorted(map(str, a or [])) == sorted(map(str, b or [])):
            continue                                   # 並べ方だけの違いは変えたことにしない
        else:
            parts.append(f"{label} {_shown(key, field, a)}→{_shown(key, field, b)}")
    return "、".join(parts) or "変更なし"


def text_of(key: str, values: dict) -> str:
    """画面から来た値を、保存するファイルの中身にする（説明の行をつける）。"""
    return json.dumps({"_説明": COMMENTS[key], **values}, ensure_ascii=False, indent=2) + "\n"


def store(settings, key: str, audit=None, catalog: Catalog | None = None) -> SettingFile:
    return SettingFile(f"{key}.json", default_path(settings, key), settings.settings_dir,
                       lambda text: problems(key, text, catalog), audit=audit, audit_action=f"settings.{key}")


def pinned_fields(settings, key: str) -> dict[str, str]:
    """起動の指定が勝っている項目 → 起動の指定の名前。"""
    return {f.split(".", 1)[1]: PIN_FLAGS[f] for f in getattr(settings, "pinned", ()) if f.startswith(f"{key}.") and f in PIN_FLAGS}


def running_values(settings, key: str, policy: identity.Policy | None = None) -> dict:
    """今動いている値（保存したものと比べて、再起動が要るかを見る）。"""
    if key == "identity":
        p = policy or identity.Policy.default()
        return {"preset": p.preset, "question": p.question, "clues": p.clues, "confirm": p.confirm,
                "surname_counts": p.surname_counts, "max_follow_ups": p.max_follow_ups}
    if key == "audit":
        return {"keep_days": settings.audit_keep_days, "keep_body": settings.audit_keep_body}
    if key == "models":
        return {"chat": settings.chat_model, "summary": settings.summary_model}
    if key == "hr":
        return {"on_leave_login": settings.on_leave_login, "on_leave_permissions": settings.on_leave_permissions}
    return {"work_max_gb": settings.work_max_bytes / 1_000_000_000, "file_keep_hours": settings.file_keep_hours}


def apply_to_settings(settings, key: str, values: dict) -> None:
    """保存した値を Settings に入れる（起動の指定が勝つ項目は入れない）。モデル・監査ログの動いている物は app.py が入れ替える。"""
    pins = pinned_fields(settings, key)
    put = {k: v for k, v in values.items() if k not in pins}
    if key == "audit":
        if "keep_days" in put:
            settings.audit_keep_days = int(put["keep_days"])
        if "keep_body" in put:
            settings.audit_keep_body = bool(put["keep_body"])
    elif key == "models":
        if "chat" in put:
            settings.chat_model = put["chat"]
        if "summary" in put:
            settings.summary_model = put["summary"]
    elif key == "hr":
        settings.on_leave_login = bool(put["on_leave_login"])
        settings.on_leave_permissions = put["on_leave_permissions"]
    elif key == "files":
        settings.work_max_bytes = int(round(float(put["work_max_gb"]) * 1_000_000_000))
        settings.file_keep_hours = int(put["file_keep_hours"])


def startup(settings) -> tuple[set[str], dict[str, list[tuple[str, str]]]]:
    """起動のとき：画面で保存した写しがあれば Settings に入れる（監査ログ・モデル・ファイル・人事）。
    (写しを使った設定, {設定: 誤り}) を返す。誤りのある写しは使わず、既定のまま動く（起動は止めない）。
    本人確認の方針は load_policy で読む（誤りがあれば起動しない）。"""
    bad, loaded = {}, set()
    for key in ("audit", "models", "files", "hr"):
        s = store(settings, key)
        if s.path() != s.saved:
            continue                     # 写しがなければ、起動の値（既定）のまま
        text = s.read_text()
        found = problems(key, text)      # 起動のときは、モデルが入っているかまでは見ない（serve がモデルを確かめる）
        if found:
            bad[key] = found
            continue
        apply_to_settings(settings, key, values_of(key, text))
        loaded.add(key)
    return loaded, bad


def identity_path(settings) -> Path:
    """今使う本人確認の方針のファイル（写しがあれば写し）。"""
    return store(settings, "identity").path()


def check_text(key: str, text: str, catalog=None) -> None:
    found = problems(key, text, catalog)
    if found:
        raise SettingError("設定に直すところがあります", found)
