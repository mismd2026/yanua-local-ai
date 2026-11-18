"""画面から直せる設定のファイルの共通の部品（yanua/settingfiles.py）。仕分けルール以外（CSV など）でも使えること。"""

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yanua.settingfiles import DEFAULT, FILE_EDIT, SettingError, SettingFile  # noqa: E402


def csv_check(text: str):
    # 例：routes.csv のような「見出し＋2列」の検証。誤りは行の位置つき
    lines = text.strip().splitlines()
    problems = [] if lines and lines[0] == "種類,引き継ぎ先" else [("1行目", "見出しは「種類,引き継ぎ先」にしてください")]
    problems += [(f"{i}行目", "列が2つではありません") for i, ln in enumerate(lines[1:], start=2) if len(ln.split(",")) != 2]
    return problems


@pytest.fixture
def store(tmp_path):
    default = tmp_path / "repo" / "routes.csv"
    default.parent.mkdir()
    default.write_text("種類,引き継ぎ先\nPC,情シス\n", encoding="utf-8")
    return SettingFile("routes.csv", default, tmp_path / "settings", csv_check)


def test_reads_default_until_saved_and_never_writes_default(store):
    before = store.default.read_text(encoding="utf-8")
    assert store.path() == store.default and store.status()["using"] == "default"
    v = store.save("種類,引き継ぎ先\nPC,情シス\n来客,総務\n", "10002", "来客を足した")
    assert v == 2 and store.path() == store.saved and store.saved.parent.name == "settings"
    assert store.read_text().endswith("来客,総務\n") and store.default.read_text(encoding="utf-8") == before
    assert [(h["version"], h["saved_by"]) for h in store.history()] == [(2, "10002"), (1, DEFAULT)]
    assert store.get(1) == before and store.status()["current_version"] == 2


def test_validation_errors_have_locations_and_write_nothing(store):
    with pytest.raises(SettingError) as e:
        store.save("種類;引き継ぎ先\nPC\n", "10002", "x")
    assert e.value.problems == [("1行目", "見出しは「種類,引き継ぎ先」にしてください"), ("2行目", "列が2つではありません")]
    assert not store.saved.exists() and store.history() == []


def test_restore_reset_and_default_changed(store):
    store.save("種類,引き継ぎ先\nPC,情シス\n来客,総務\n", "10002", "来客を足した")
    assert store.status()["default_changed"] is False
    store.default.write_text("種類,引き継ぎ先\nPC,情シス\n電話,総務\n", encoding="utf-8")   # 既定が更新された
    st = store.status()
    assert st["default_changed"] is True and st["using"] == "saved" and "電話" not in store.read_text()
    assert store.restore(1, "10002") == 3 and store.status()["default_changed"] is True   # 写しのまま
    v = store.reset_to_default("10002")
    assert not store.saved.exists() and "電話" in store.read_text()
    assert store.status() == {**store.status(), "using": "default", "default_changed": False, "current_version": v}
    assert "来客" in store.get(2)                                                          # 消す前の写しも残る
    with pytest.raises(KeyError):
        store.restore(99, "10002")


def test_hand_edited_copy_is_kept_before_saving(store):
    store.save("種類,引き継ぎ先\nPC,情シス\n", "10002", "a")
    store.saved.write_text("種類,引き継ぎ先\nPC,情シス部\n", encoding="utf-8")
    store.save(lambda cur: cur + "来客,総務\n", "10003", "b")
    hist = store.history()
    assert [h["saved_by"] for h in hist] == ["10003", FILE_EDIT, "10002", DEFAULT]
    assert store.read_text() == "種類,引き継ぎ先\nPC,情シス部\n来客,総務\n"     # 直接直した分も消えない


def test_concurrent_saves_do_not_lose_each_other(store):
    def add(i):
        store.save(lambda cur: cur + f"種類{i},総務\n", f"1000{i}", f"{i}")

    ts = [threading.Thread(target=add, args=(i,)) for i in range(8)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    text = store.read_text()
    assert all(f"種類{i},総務" in text for i in range(8))
    assert [h["version"] for h in store.history()] == list(range(9, 0, -1))


def test_name_must_be_a_plain_file_name(tmp_path):
    with pytest.raises(ValueError):
        SettingFile("../x.json", tmp_path / "x.json", tmp_path / "s")
