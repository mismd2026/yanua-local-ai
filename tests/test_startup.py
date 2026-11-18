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
