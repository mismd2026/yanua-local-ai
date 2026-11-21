"""人事データの取り込み（S-51）の速さを、大きな人事データで測る。

    python eval/bench_hr_import.py --employees 1000000

架空の人事データ（CSV。住所・給与の列も入れる）を作り、次の時間を測る。作ったファイルは一時フォルダに置き、最後に消す。
1. 初めての取り込み（試し・確定）
2. 2回目（全員のファイル。部署の異動 1%・退職 0.5%・新しく入る人 0.1%・いなくなる人 0.1%）の試しと確定、
   一部のファイル（1つの部署の人だけ）の試し
   （手で入れたグループのメンバーも 1,000人置き、退職した人がそこから外れる分も含む）
3. 1つ前に戻す（試し・確定）
"""

from __future__ import annotations

import argparse
import csv
import random
import sqlite3
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from yanua import access, hrimport  # noqa: E402

SURNAMES = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤"]
CHARS = "一二三四五六七八九十春夏秋冬花月雪風空海山川森林光明美咲健誠真由香子太郎次翔大陽"
HEAD = ["職員コード", "漢字氏名", "ふりがな", "所属名", "所属CD", "在職区分", "メール", "住所", "給与"]
MAPPING = {"columns": {"employee_no": "職員コード", "name": "漢字氏名", "kana": "ふりがな", "dept": "所属名",
                       "dept_code": "所属CD", "status": "在職区分", "email": "メール"},
           "status_values": {"在職": "在籍", "休業": "休職", "退社": "退職"}}


def people(n: int, depts: int, seed: int = 0) -> list[list[str]]:
    rng = random.Random(seed)
    given = [a + b for a in CHARS for b in CHARS]
    out = []
    for i in range(n):
        d = rng.randrange(depts)
        status = "休業" if rng.random() < 0.01 else "在職"
        out.append([str(10_000_000 + i), f"{rng.choice(SURNAMES)} {rng.choice(given)}", "ヤマダ タロウ", f"第{d}営業部", str(d),
                    status, f"u{i}@example.co.jp", "東京都千代田区1-2-3", "300000"])
    return out


def write(path: Path, rows) -> None:
    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(HEAD)
        w.writerows(rows)


def timed(label: str, fn):
    t = time.perf_counter()
    out = fn()
    print(f"{label}：{time.perf_counter() - t:.1f}秒")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--employees", type=int, default=1_000_000)
    ap.add_argument("--departments", type=int, default=2000)
    args = ap.parse_args()
    n = args.employees
    with tempfile.TemporaryDirectory() as tmp:
        first, second, db_path = Path(tmp) / "hr1.csv", Path(tmp) / "hr2.csv", Path(tmp) / "hr.db"
        rows = people(n, args.departments)
        write(first, rows)
        print(f"CSV：{n:,}人、部署 {args.departments:,}、{first.stat().st_size / 1e6:.0f}MB")
        db = sqlite3.connect(db_path)
        access.ensure_schema(db)
        p = timed("1回目の試し", lambda: hrimport.preview(db, first, MAPPING))
        print(f"  入る {p.diff['added']['count']:,}人")
        r = timed("1回目の確定", lambda: hrimport.commit(db, first, MAPPING, "bench"))
        assert r.committed, r.reason
        # 手で入れたグループ（1,000人）と、決まりのグループ（部署 10個）
        with db:
            db.execute("INSERT INTO access_groups VALUES ('G-x', '手で入れた', '', NULL)")
            db.execute("INSERT INTO access_groups VALUES ('G-y', '部署の決まり', '', ?)",
                       ('{"departments": [' + ",".join(f'"第{d}営業部"' for d in range(10)) + "]}",))
            db.executemany("INSERT INTO group_members VALUES ('G-x', ?, NULL, 'bench', 'bench')",
                           [(rows[i][0],) for i in range(0, n, max(1, n // 1000))])
        rng = random.Random(1)
        changed = [list(x) for x in rows]
        for i in rng.sample(range(n), n // 100):
            changed[i][3] = f"第{rng.randrange(args.departments)}営業部"
        for i in rng.sample(range(n), n // 200):
            changed[i][5] = "退社"
        gone = set(rng.sample(range(n), n // 1000))
        changed = [x for i, x in enumerate(changed) if i not in gone]
        changed += [[str(20_000_000 + i), "新人 一郎", "", "第1営業部", "1", "在職", "", "", ""] for i in range(n // 1000)]
        write(second, changed)
        p = timed("2回目の試し（全員のファイル）", lambda: hrimport.preview(db, second, MAPPING, scope="all"))
        print(f"  入る {p.diff['added']['count']:,}人・変わる {p.diff['changed']['count']:,}人・"
              f"いなくなる {p.diff['removed']['count']:,}人・グループ {len(p.groups)}個・手で入れたグループから外れる {len(p.retired_members)}人")
        r = timed("2回目の確定（全員のファイル）", lambda: hrimport.commit(db, second, MAPPING, "bench", scope="all"))
        assert r.committed, r.reason
        part = Path(tmp) / "hr3.csv"
        write(part, [x for x in changed if x[3] == "第1営業部"][:1000])
        p = timed("一部のファイル（1つの部署）の試し", lambda: hrimport.preview(db, part, MAPPING))
        print(f"  変わる {p.diff['changed']['count']:,}人・いなくなる {p.diff['removed']['count']:,}人")
        timed("1つ前に戻す（試し）", lambda: hrimport.preview_restore(db))
        r = timed("1つ前に戻す（確定）", lambda: hrimport.restore(db, "bench"))
        assert r.committed
        db.close()
        print(f"保存先の大きさ（控え {hrimport.HR_KEEP}回分まで）：{db_path.stat().st_size / 1e6:.0f}MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
