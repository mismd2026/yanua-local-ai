"""本人確認の速さを、大きな人事データで測る（K-050・K-013）。

    python eval/bench_identity.py --employees 1000000

架空の人事データ（CSV）を作って取り込み、照合（identity.check）と受付の訪ね先探し（hr.find_in_text）に
かかる時間を測る。同姓同名がたくさんいるデータにしてある。作ったファイルは一時フォルダに置き、最後に消す。
"""

from __future__ import annotations

import argparse
import csv
import random
import sqlite3
import statistics
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yanua import hr, identity  # noqa: E402

SURNAMES = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤",
            "吉田", "山田", "佐々木", "山口", "松本", "井上", "木村", "林", "斎藤", "清水"]
CHARS = "一二三四五六七八九十春夏秋冬花月雪風空海山川森林光明美咲健誠真由香子太郎次翔大陽"


def make_csv(path: Path, n: int, depts: int) -> None:
    given = [a + b for a in CHARS for b in CHARS]   # 姓20 × 名2500 = 5万通り：1つの名前に平均20人の同姓同名
    rng = random.Random(0)
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["社員番号", "氏名", "所属", "所属コード", "在籍区分"])
        for i in range(n):
            d = rng.randrange(depts)
            w.writerow([str(10_000_000 + i), f"{rng.choice(SURNAMES)} {rng.choice(given)}", f"第{d}営業部", str(d), "在籍"])


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--employees", type=int, default=1_000_000)
    ap.add_argument("--departments", type=int, default=2000)
    ap.add_argument("--trials", type=int, default=200)
    args = ap.parse_args()

    with tempfile.TemporaryDirectory() as tmp:
        src, db_path = Path(tmp) / "hr.csv", Path(tmp) / "hr.db"
        t = time.perf_counter()
        make_csv(src, args.employees, args.departments)
        print(f"CSVの作成：{time.perf_counter() - t:.1f}秒（{args.employees:,}人、部署 {args.departments:,}）")

        db = sqlite3.connect(db_path)
        mapping = {"columns": {"employee_no": "社員番号", "name": "氏名", "dept": "所属", "dept_code": "所属コード",
                               "status": "在籍区分"}, "active_values": ["在籍"]}
        t = time.perf_counter()
        report = hr.import_employees(db, src, mapping)
        print(f"取り込み：{time.perf_counter() - t:.1f}秒（在籍 {report.active:,}人、エラー {len(report.errors)}件）")
        print(f"保存先の大きさ：{db_path.stat().st_size / 1e6:.0f}MB")

        rows = db.execute("SELECT employee_no, name, dept FROM employees").fetchall()
        rng = random.Random(1)
        cases = {
            "番号＋名前＋部署": lambda r: f"{r[2]}の{r[1].replace(' ', '')}です。番号は{r[0]}です",
            "名前＋部署（番号なし）": lambda r: f"{r[2]}の{r[1].replace(' ', '')}です。番号はわかりません",
            "姓＋部署（同姓が多い）": lambda r: f"{r[2]}の{hr.surname_of(r[1])}です",
        }
        for label, make in cases.items():
            times, confirmed = [], 0
            for r in rng.sample(rows, args.trials):
                t = time.perf_counter()
                res = identity.check(db, [make(r)])
                times.append(time.perf_counter() - t)
                confirmed += bool(res.person and res.person["employee_no"] == r[0])
            print(f"照合（{label}）：中央値 {statistics.median(times) * 1000:.1f}ms、"
                  f"最大 {max(times) * 1000:.1f}ms、確定 {confirmed}/{args.trials}")

        times = []
        for r in rng.sample(rows, args.trials):
            t = time.perf_counter()
            hr.find_in_text(db, f"{r[1].replace(' ', '')}さんにお約束しています")
            times.append(time.perf_counter() - t)
        print(f"受付の訪ね先探し：中央値 {statistics.median(times) * 1000:.1f}ms、最大 {max(times) * 1000:.1f}ms")
        db.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
