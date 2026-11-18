"""画面から直せる設定のファイル（共通の部品）。仕分けルールで使い始め、K-042 の設定の画面（S-52）でも使う。

考え方：
- リポジトリのファイル（例：demo/company/paper/rules.json）は「既定」。この部品は既定を書き換えない
- 画面から保存すると、中身の全部を写しとして settings_dir（既定 data/settings/）に書く（例：data/settings/rules.json）
- 読むときは、写しがあれば写しだけを使い、なければ既定を使う
- 版の記録も settings_dir の下に残す（history/<名前>/）。保存の前の中身も版として残るので、前の版に戻せる。
  戻すのも新しい版として保存するので、戻したこともまた戻せる。「既定に戻す」は写しを消して既定を使う
- 写しのもとになった既定の中身（SHA-256）を覚えておき、そのあと既定が変わっていたら知らせる（default_changed）。
  写しを使っている間は、既定の更新は反映されないため。写しから保存し直しても知らせは消えず、「既定に戻す」で消える
- 保存・戻すの前に、その設定の読み込みと同じ検証（validate）をかける。誤りがあれば何も書かない
- ファイルは同じフォルダの一時ファイルに書いてから置き換える。同じサーバーの中では、ロックの中で今の中身を読んでから作る
- 保存・版に戻す・既定に戻すは、成功も失敗も監査ログに残す（audit を渡したとき。操作の名前は <audit_action>.save など）

版の記録の形（history/<名前>/）：
    index.jsonl   1行に1つの版（version・saved_at・saved_by・action・sha256・default_sha256・kind）
    v0001.<拡張子> …  その版の中身
"""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import threading
from datetime import datetime
from pathlib import Path
from typing import Callable

FILE_EDIT = "（ファイル）"     # 写しを直接直していた中身の版の「保存した人」
DEFAULT = "（既定）"           # 既定の中身の版の「保存した人」

Validator = Callable[[str], list[tuple[str, str]]]   # 中身 → [(位置, 理由)]。空なら正しい
# 監査ログに残す関数：(操作の名前, した人, 結果「成功」「失敗」, 短い説明)。yanua/audit.py の Auditor.record につなぐ
AuditFn = Callable[[str, str, str, str], None]

_locks: dict[str, threading.Lock] = {}
_locks_guard = threading.Lock()


class SettingError(ValueError):
    """検証に通らない中身。problems は (位置, 理由)。"""

    def __init__(self, message: str, problems: list[tuple[str, str]]):
        super().__init__(message)
        self.problems = problems


def sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _write(path: Path, body: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


class SettingFile:
    def __init__(self, name: str, default: Path, settings_dir: Path, validate: Validator | None = None,
                 audit: AuditFn | None = None, audit_action: str | None = None):
        """name は写しのファイル名（例：rules.json）。同じ settings_dir の中で重ならない名前にする。
        audit_action は監査ログの操作の名前の頭（例：paper.rules → paper.rules.save）。なければ settings.<name>。"""
        if "/" in name or "\\" in name or name.startswith("."):
            raise ValueError(f"設定の名前が正しくありません: {name}")
        self.name = name
        self.default = Path(default)
        self.saved = Path(settings_dir) / name
        self.history_dir = Path(settings_dir) / "history" / name
        self.validate = validate or (lambda text: [])
        self._audit = audit
        self.audit_action = audit_action or f"settings.{name}"
        with _locks_guard:
            self._lock = _locks.setdefault(str(self.saved.resolve()), threading.Lock())

    # ------------------------------------------------------------ 読む

    def path(self) -> Path:
        """今使っているファイル（写しがあれば写し、なければ既定）。"""
        return self.saved if self.saved.is_file() else self.default

    def read_text(self) -> str:
        return self.path().read_text(encoding="utf-8")

    def _default_text(self) -> str | None:
        return self.default.read_text(encoding="utf-8") if self.default.is_file() else None

    def _entries(self) -> list[dict]:
        index = self.history_dir / "index.jsonl"
        if not index.is_file():
            return []
        return [json.loads(line) for line in index.read_text(encoding="utf-8").splitlines() if line.strip()]

    def status(self) -> dict:
        """using：saved（画面で保存した写し）/ default（既定）。default_changed：写しを書いたあとに既定が変わったか。"""
        entries = self._entries()
        using = "saved" if self.saved.is_file() else "default"
        default_changed = False
        if using == "saved":
            wrote = [e for e in entries if e.get("default_sha256")]
            current_default = self._default_text()
            if wrote and current_default is not None:
                default_changed = wrote[-1]["default_sha256"] != sha256(current_default)
        current = None
        if entries and self.path().is_file() and entries[-1]["sha256"] == sha256(self.read_text()):
            current = entries[-1]["version"]
        return {"using": using, "default_changed": default_changed, "current_version": current,
                "default_updated_at": (datetime.fromtimestamp(self.default.stat().st_mtime).astimezone().isoformat(timespec="seconds")
                                       if default_changed else None)}

    def history(self, limit: int = 50) -> list[dict]:
        keys = ("version", "saved_at", "saved_by", "action", "kind")
        return [{k: e.get(k) for k in keys} for e in reversed(self._entries())][:limit]

    def get(self, version: int) -> str | None:
        p = self.history_dir / f"v{version:04d}{self.default.suffix}"
        return p.read_text(encoding="utf-8") if p.is_file() else None

    # ------------------------------------------------------------ 書く

    def _add(self, entries: list[dict], by: str, action: str, body: str, kind: str, default_sha: str | None) -> int:
        version = (entries[-1]["version"] if entries else 0) + 1
        _write(self.history_dir / f"v{version:04d}{self.default.suffix}", body)
        entry = {"version": version, "saved_at": _now(), "saved_by": by, "action": action, "sha256": sha256(body),
                 "default_sha256": default_sha, "kind": kind}
        with (self.history_dir / "index.jsonl").open("a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
        entries.append(entry)
        return version

    def _keep_before(self, entries: list[dict]) -> None:
        """保存の前の中身が、まだ版になっていなければ残す（写しを直接直していたとき・はじめて保存するとき）。"""
        if self.saved.is_file():
            text = self.saved.read_text(encoding="utf-8")
            if not entries or entries[-1]["sha256"] != sha256(text):
                self._add(entries, FILE_EDIT, "保存の前の中身（写しを直接直したもの）", text, "saved", None)
        elif not entries or entries[-1]["sha256"] != sha256(self._default_text() or ""):
            text = self._default_text()
            if text is not None:
                self._add(entries, DEFAULT, "保存の前の中身（既定）", text, "default", None)

    def _check(self, body: str) -> None:
        problems = self.validate(body)
        if problems:
            raise SettingError("設定に直すところがあります", problems)

    def _log(self, op: str, by: str, ok: bool, detail: str) -> None:
        if self._audit:
            try:
                self._audit(f"{self.audit_action}.{op}", by, "成功" if ok else "失敗", f"{self.name}：{detail}")
            except Exception:  # noqa: BLE001  記録できなくても、設定の保存は止めない
                pass

    def _logged(self, op: str, by: str, what: str, fn: Callable[[], int]) -> int:
        try:
            version = fn()
        except SettingError as e:
            self._log(op, by, False, f"{what}（保存しませんでした：検証を通らないところが {len(e.problems)}件）")
            raise
        except Exception as e:
            reason = getattr(e, "message", None) or type(e).__name__     # 使う側の検証の断り（例：ApiError）は、その文
            self._log(op, by, False, f"{what}（保存しませんでした：{reason}）")
            raise
        self._log(op, by, True, f"{what}（版 {version}）")
        return version

    def save(self, new_body: str | Callable[[str], str], by: str, action: str) -> int:
        """写しに保存して、新しい版の番号を返す。new_body は文字か、今の中身を受け取って新しい中身を返す関数。"""
        return self._logged("save", by, action, lambda: self._save(new_body, by, action))

    def _save(self, new_body: str | Callable[[str], str], by: str, action: str) -> int:
        with self._lock:
            body = new_body(self.read_text()) if callable(new_body) else new_body
            self._check(body)
            entries = self._entries()
            self._keep_before(entries)
            version = self._add(entries, by, action, body, "saved", self._base_default(entries))
            _write(self.saved, body)
            return version

    def _base_default(self, entries: list[dict]) -> str:
        """写しのもとになった既定の中身（SHA-256）。写しから保存し直しても、既定の更新は入らないので引き継ぐ。
        既定から写しを作るとき（はじめての保存・既定に戻したあと）だけ、今の既定にする。"""
        if self.saved.is_file():
            for e in reversed(entries):
                if e.get("kind") == "default":
                    break
                if e.get("default_sha256"):
                    return e["default_sha256"]
        default_text = self._default_text()
        return sha256(default_text) if default_text is not None else ""

    def restore(self, version: int, by: str) -> int:
        """前の版の中身を、新しい版として写しに保存する。今の検証に通らない版には戻さない。"""
        def run() -> int:
            body = self.get(version)
            if body is None:
                raise KeyError(version)
            return self._save(body, by, f"版 {version} に戻した")
        return self._logged("restore", by, f"版 {version} に戻した", run)

    def reset_to_default(self, by: str) -> int:
        """写しを消して、既定を使う（新しい既定も、ここから使われる）。消す前の写しは版として残る。"""
        return self._logged("reset", by, "既定に戻した", lambda: self._reset(by))

    def _reset(self, by: str) -> int:
        with self._lock:
            default_text = self._default_text()
            if default_text is None:
                raise FileNotFoundError(self.default)
            self._check(default_text)
            entries = self._entries()
            self._keep_before(entries)
            version = self._add(entries, by, "既定に戻した（画面で保存した写しを消した）", default_text, "default", None)
            self.saved.unlink(missing_ok=True)
            return version
