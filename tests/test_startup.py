"""serve の起動のときの設定の検証（README「画面で変えた設定」の決まり）。

- 権限（roles.json）と本人確認の方針（identity.json）に誤りがあると、起動しない（だれが何をできるかが決まらないため）
- それ以外（仕分けルールなど）は、誤りを位置つきで出して起動を続け、その機能だけを止める
"""

import json
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yanua import __main__ as cli  # noqa: E402
from yanua.llm import LLMError  # noqa: E402
from yanua.store import Store  # noqa: E402

DEMO = ROOT / "demo" / "company"


@pytest.fixture
def base(tmp_path, monkeypatch):
    db = tmp_path / "index.db"
    assert Store(db).ingest(DEMO / "docs").errors == []
    # モデルの確かめで止める（ここまで来れば、設定の検証は通って起動を続けたということ）
    def no_model(self):
        raise LLMError("（テスト）モデルの確かめで止めます")
    monkeypatch.setattr(cli.OllamaChat, "check", no_model)
    rules = tmp_path / "paper" / "rules.json"
    rules.parent.mkdir()
    shutil.copy2(DEMO / "paper" / "rules.json", rules)
    return ["--db", str(db), "serve", "--chat-db", str(tmp_path / "chat.db"), "--settings-dir", str(tmp_path / "settings"),
            "--paper-rules", str(rules)], tmp_path


def test_bad_roles_stop_startup(base, capsys):
    args, tmp = base
    bad = tmp / "roles.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert cli.main([*args, "--roles", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "roles.json" in out and "モデルの確かめ" not in out


def test_bad_identity_policy_stops_startup(base, capsys):
    args, tmp = base
    bad = tmp / "identity.json"
    bad.write_text("{ not json", encoding="utf-8")
    assert cli.main([*args, "--identity", str(bad)]) == 1
    out = capsys.readouterr().out
    assert "identity.json" in out and "モデルの確かめ" not in out


def test_bad_paper_rules_only_stop_that_feature(base, capsys):
    args, tmp = base
    rules = Path(args[args.index("--paper-rules") + 1])
    data = json.loads(rules.read_text(encoding="utf-8"))
    data["rules"][0]["fields"] = {"no": r"番号(\d+"}
    rules.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    assert cli.main(args) == 1       # モデルの確かめまで進んだ（起動を続けた）
    out = capsys.readouterr().out
    assert "PDF の仕分け・変換は、直すまで使えません" in out
    assert "rules[1].fields.no：正規表現の誤り：かっこ「(」が閉じていません（3文字目）" in out
    assert "モデルの確かめで止めます" in out


class _Stop(Exception):
    pass


def test_serve_pins_given_flags_and_uses_saved_settings(base, monkeypatch, capsys):
    """起動の指定で付けた項目は画面より勝つ（pinned）。付けていない項目は、画面で保存した写しを使う。"""
    args, tmp = base
    settings_dir = Path(args[args.index("--settings-dir") + 1])
    settings_dir.mkdir()
    (settings_dir / "models.json").write_text('{"chat": "llama3.2:3b", "summary": "qwen2.5:7b"}', encoding="utf-8")
    (settings_dir / "audit.json").write_text('{"keep_days": 30, "keep_body": false}', encoding="utf-8")
    (settings_dir / "files.json").write_text('{"work_max_gb": "たくさん", "file_keep_hours": 24}', encoding="utf-8")
    checked, got = [], {}
    monkeypatch.setattr(cli.OllamaChat, "check", lambda self: checked.append(self.name))

    def fake_create_app(settings, llm, **kw):
        got["settings"] = settings
        raise _Stop()

    import yanua.api
    monkeypatch.setattr(yanua.api, "create_app", fake_create_app)
    with pytest.raises(_Stop):
        cli.main([*args, "--audit-body", "--summary-model", "qwen2.5:7b"])
    s = got["settings"]
    assert s.pinned == {"audit.keep_body", "models.summary"}
    assert s.audit_keep_body is True and s.audit_keep_days == 30          # 起動の指定が勝つ／画面で保存した日数
    assert checked[0] == "llama3.2:3b" and s.chat_model == "llama3.2:3b"   # 画面で保存したモデルを確かめて使う
    assert s.work_max_bytes == 20_000_000_000                             # 壊れた写しは使わず、既定のまま
    out = capsys.readouterr().out
    assert "設定「ファイル（作業用フォルダ）」が読み込めないので、既定で動きます" in out and "work_max_gb" in out


def test_serve_does_not_start_with_a_broken_identity_copy(base, capsys):
    args, tmp = base
    settings_dir = Path(args[args.index("--settings-dir") + 1])
    settings_dir.mkdir()
    (settings_dir / "identity.json").write_text('{"preset": "ない型"}', encoding="utf-8")
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    assert "知らない型です" in out and "モデルの確かめ" not in out


def test_serve_builds_access_once_and_stops_on_broken_access(base, capsys):
    """権限（役割 × 対象 × グループ）：初めての起動で roles.json から作る。誤りがあれば起動しない。"""
    import sqlite3

    args, tmp = base
    assert cli.main(args) == 1                                     # モデルの確かめまで進む（起動を続けた）
    out = capsys.readouterr().out
    assert "権限の仕組みを、roles.json から作りました" in out and "モデルの確かめで止めます" in out
    db = sqlite3.connect(tmp / "index.db")
    with db:
        db.execute("UPDATE access_roles SET permissions = '[\"nosuch\"]' WHERE name = '総務'")
    db.close()
    assert cli.main(args) == 1
    out = capsys.readouterr().out
    assert "権限の設定に誤りがあるので起動しません" in out and "roles.総務：知らない権限です: nosuch" in out
    assert "モデルの確かめ" not in out and "作りました" not in out


def _migrate_logs(tmp) -> list[str]:
    import sqlite3

    db = sqlite3.connect(tmp / "audit.db")
    try:
        return [r[0] for r in db.execute("SELECT detail FROM audit_log WHERE action = 'roles.migrate' ORDER BY id")]
    except sqlite3.Error:
        return []
    finally:
        db.close()


def test_serve_records_what_it_built_or_added_in_the_audit_log(base, capsys):
    """serve の起動の確かめで作った・足したもの（役割・グループ）は、監査ログに roles.migrate で残す。すでにあれば残さない。"""
    import sqlite3

    args, tmp = base
    assert cli.main(args) == 1                                     # 初めての起動（roles.json から作る）
    assert _migrate_logs(tmp) == ["初めての起動：roles.json から役割・グループ・対象を作った（docs/access-model.md）"]
    # 前の版のデータ（職務ロールと「経営」のグループがない）にする
    db = sqlite3.connect(tmp / "index.db")
    with db:
        db.execute("DELETE FROM group_grants WHERE group_id = 'G-exec'")
        db.execute("DELETE FROM access_groups WHERE group_id = 'G-exec'")
        db.execute("DELETE FROM access_roles WHERE name IN ('マネージャー', 'ディレクター', '経営')")
        db.execute("DELETE FROM access_meta WHERE key = 'roles_v2'")
    db.close()
    assert cli.main(args) == 1
    logs = _migrate_logs(tmp)[1:]
    assert logs == ["役割「マネージャー」を足した（roles.delegate・reports.view）",
                    "役割「ディレクター」を足した（roles.delegate・reports.view）",
                    "役割「経営」を足した（documents.view・reports.view）",
                    "グループ「経営」（役職の段 5 以上）を作り、役割「経営」× 全社を与えた"]
    assert "権限の仕組みを今の版に合わせました：役割「経営」を足した" in capsys.readouterr().out
    assert cli.main(args) == 1                                     # すでにあるので、もう残さない
    assert len(_migrate_logs(tmp)) == 5
