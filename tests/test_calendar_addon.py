"""Googleカレンダーのアドオン（Apps Script）と、Yanua の取り込みのテスト。

アドオン本体は Google の上でしか動かないので、ここでは次を確かめる。
- コードの作り方とハッシュが、Apps Script（Node で動かす）と Yanua でそろっていること
- アドオンが書き出したシート（CSV）を取り込めること。リスケと取り消しも反映されること
"""

import shutil
import subprocess
import sys
from datetime import date
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from yanua import hr, visits  # noqa: E402
from yanua.flows.external.reception import build_app  # noqa: E402
from yanua.flows.external.reception.cli import run  # noqa: E402
from yanua.store import Store  # noqa: E402

ADDON = ROOT / "integrations" / "google-calendar" / "test_addon.js"
EXPORT = ROOT / "demo" / "company" / "flows" / "reception" / "calendar_export_sample.csv"
HR_DIR = ROOT / "demo" / "company" / "hr"
RECEPTION_DATA = ROOT / "demo" / "company" / "flows" / "reception"
NODE = shutil.which("node")


@pytest.fixture
def store(tmp_path):
    s = Store(tmp_path / "index.db")
    assert s.ingest(ROOT / "demo" / "company" / "public").errors == []
    hr.import_employees(s.db, HR_DIR / "hr_export_sample.csv", hr.load_mapping(HR_DIR / "mapping_sample.json"))
    yield s
    s.close()


@pytest.mark.skipif(NODE is None, reason="node がない環境")
def test_addon_self_test():
    out = subprocess.run([NODE, str(ADDON), "--self"], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == "ok"


@pytest.mark.skipif(NODE is None, reason="node がない環境")
@pytest.mark.parametrize("code", ["MRKT-7Q4A", "qw8ert5y", "ＺＫ３Ｎ ８ＰＸＤ"])
def test_hash_matches_apps_script(code):
    out = subprocess.run([NODE, str(ADDON), code], capture_output=True, text=True, check=True)
    assert out.stdout.strip() == visits._hash(code)


def test_import_calendar_export_and_check_in(store):
    assert visits.is_calendar_export(EXPORT)
    report = visits.import_calendar_csv(store.db, EXPORT)
    assert report == {"added": 2, "updated": 0, "cancelled": 1, "errors": []}
    visit = visits.find_by_code(store.db, "qw8e rt5y", date(2026, 11, 16))
    assert visit["host_employee_no"] == "10003" and visit["room"] == "5階 第3会議室"   # 主催者のメールから社員番号へ
    assert visits.find_by_code(store.db, "ZK3N-8PXD", date(2026, 11, 18)) is None      # 取消は使えない

    app = build_app(store, object(), RECEPTION_DATA, date(2026, 11, 16))
    state = run(app, "来訪", lambda q: "QW8E-RT5Y", out=lambda *_: None)
    assert "5階 第3会議室" in state["result"] and "内線4300" in state["result"]


def test_reschedule_updates_same_code(store, tmp_path):
    visits.import_calendar_csv(store.db, EXPORT)
    moved = tmp_path / "moved.csv"
    moved.write_text(EXPORT.read_text(encoding="utf-8").replace("2026-11-16,13:00,5階 第3会議室", "2026-11-17,16:00,5階 第2会議室"),
                     encoding="utf-8")
    report = visits.import_calendar_csv(store.db, moved)
    assert report["added"] == 0 and report["updated"] == 2
    assert visits.find_by_code(store.db, "QW8E-RT5Y", date(2026, 11, 16)) is None
    assert visits.find_by_code(store.db, "QW8E-RT5Y", date(2026, 11, 17))["room"] == "5階 第2会議室"


def test_unknown_or_retired_host_is_rejected(store, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(EXPORT.read_text(encoding="utf-8").replace("suzuki.i@", "watanabe.t@"), encoding="utf-8")
    report = visits.import_calendar_csv(store.db, bad)
    assert report["added"] == 1 and len(report["errors"]) == 1 and "watanabe.t@" in report["errors"][0]
