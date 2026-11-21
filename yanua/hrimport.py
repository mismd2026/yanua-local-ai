"""人事データの取り込み：読む → 試す（保存しない）→ 確定（まとめて1回で入れ替える）→ 前に戻す（S-51・K-016・K-054）。

画面（/admin/hr）とコマンド（python -m yanua hr-import）は、どちらもここを通る。

- 読むのは、対応表で割り当てた列だけ（社員番号・氏名・よみ・部署名・部署コード・在籍状態・メールアドレス）。
  住所・給与などの列がファイルにあっても、その列の値は読まない（CSV・Excel は列の位置で拾い、JSON は割り当てた名前だけ取る）
- 在籍状態は、ファイルの値（例「在職」「休業」「退社」）を、Yanua の3つ（在籍・休職・退職）に対応づける。
  対応づけていない値は誤り（行番号つき）。誤りが1件でもあれば確定しない
- 試しの結果：新しく入る人・変わる人（部署・在籍状態・名前など）・いなくなる人の件数と先頭の数件、
  部署が変わる人・退職になる人・いなくなる人の、入るグループ・外れるグループ（グループごとの人数）
- 確定：1つのトランザクションで、今のデータを控えに写し → まるごと入れ替え → 部署の一覧を作り直し →
  退職した人・いなくなった人を、手で入れたグループから外す → 権限の結果を作り直す（世代を上げる）。途中で失敗したら前のまま
- 取り込みの種類：「一部のファイル」（既定。ファイルにいない人は変えない）と「全員のファイル」（ファイルにいない人は
  いなくなる人）。全員のファイルで、いなくなる人が今の人数の REMOVAL_WARN を超えるときは、確かめてからでないと確定しない
- 「使わない」にした任意の列（よみ・部署コード・メールアドレス・在籍状態）は、今いる人の値をそのまま残す（変わる人にも数えない）。
  新しく入る人だけ、その列が空（在籍状態は「在籍」）で入る
- 控えは直近の HR_KEEP 回分。「1つ前に戻す」は、いちばん新しい控えを、取り込みと同じ流れ（試し → 確定）で入れる
  （戻したことも控えに残るので、戻したことをまた戻せる）。手で入れたグループのメンバーは、戻しても自動では戻らない
- 100万人でも数分で終わるように、比べるのは SQLite の一時表どうし（全員を Python に読み込まない）
"""

from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Callable, Iterator

from . import hr

HR_KEEP = 3                 # 控え（前の取り込みのデータ）を残す数
SAMPLE = 5                  # 試しの結果に出す先頭の件数
MAX_ERRORS = 100            # 誤りを出す件数の上限（数はすべて数える）
COMPARED = ("name", "kana", "dept", "dept_code", "status", "email")
CHANGE_LABELS = {"name": "名前", "kana": "よみ", "dept": "部署", "dept_code": "部署コード", "status": "在籍状態",
                 "email": "メールアドレス"}
LEAVE_KEEP, LEAVE_EMPLOYEE_ONLY = "そのまま", "社員の役割だけ"
SCOPE_PARTIAL, SCOPE_ALL = "partial", "all"
SCOPE_LABELS = {SCOPE_PARTIAL: "一部のファイル", SCOPE_ALL: "全員のファイル"}
REMOVAL_WARN = 0.10         # 全員のファイルで、いなくなる人がこの割合を超えたら、確かめてからでないと確定しない
OPTIONAL = ("kana", "dept_code", "email", "status")   # 「使わない」にできる列（今の値を残す）

META_SCHEMA = """
CREATE TABLE IF NOT EXISTS hr_backups (
    backup_id   INTEGER PRIMARY KEY AUTOINCREMENT,
    saved_at    TEXT NOT NULL,       -- 控えにした時刻（このデータを入れ替える取り込みの時刻）
    source      TEXT NOT NULL,       -- このデータを入れた取り込みのファイル名（なければ「（前の取り込み）」）
    total       INTEGER NOT NULL,
    active      INTEGER NOT NULL
);
"""
IMPORT_COLUMNS = {"kind": "TEXT NOT NULL DEFAULT '取り込み'", "by_who": "TEXT NOT NULL DEFAULT ''",
                  "added": "INTEGER NOT NULL DEFAULT 0", "changed": "INTEGER NOT NULL DEFAULT 0",
                  "removed": "INTEGER NOT NULL DEFAULT 0", "scope": "TEXT NOT NULL DEFAULT ''"}


class MappingError(hr.HRImportError):
    def __init__(self, problems: list[tuple[str, str]]):
        super().__init__("対応づけに直すところがあります：" + "／".join(f"{w}: {m}" if w else m for w, m in problems))
        self.problems = problems


def ensure_schema(db: sqlite3.Connection) -> None:
    hr.ensure_schema(db)
    db.executescript(META_SCHEMA)
    have = {r[1] for r in db.execute("PRAGMA table_info(hr_imports)")}
    for col, decl in IMPORT_COLUMNS.items():
        if col not in have:
            db.execute(f"ALTER TABLE hr_imports ADD COLUMN {col} {decl}")


# ---------------------------------------------------------------- 対応づけ


@dataclass
class Mapping:
    columns: dict[str, str]                  # Yanua の項目 → ファイルの列名
    status_values: dict[str, str] | None     # ファイルの値（照合用の形）→ 在籍・休職・退職。None なら在籍状態の列なし
    legacy_active: set[str] | None = None    # 前の書き方（active_values）
    sheet: str | None = None


def mapping_problems(m) -> list[tuple[str, str]]:
    """対応づけの誤り（位置, 理由）。画面の保存・取り込みの前に確かめる。"""
    if not isinstance(m, dict):
        return [("", "{\"columns\": {...}, \"status_values\": {...}} の形にしてください")]
    out = []
    cols = m.get("columns")
    if not isinstance(cols, dict):
        return [("columns", "列の対応（columns）がありません")]
    for k, v in cols.items():
        if k not in hr.FIELDS:
            out.append((f"columns.{k}", f"{k} という項目はありません（使える項目：{'・'.join(hr.LABELS[f] for f in hr.FIELDS)}）"))
        elif not isinstance(v, str):
            out.append((f"columns.{k}", "列の名前を文字で入れてください"))
    for f in hr.REQUIRED:
        if not str(cols.get(f) or "").strip():
            out.append((f"columns.{f}", f"{hr.LABELS[f]}の列を選んでください"))
    used = [v for v in cols.values() if isinstance(v, str) and v.strip()]
    for dup in sorted({v for v in used if used.count(v) > 1}):
        out.append(("columns", f"列「{dup}」を2つの項目に割り当てています"))
    sv = m.get("status_values")
    if sv is not None:
        if not isinstance(sv, dict):
            out.append(("status_values", "在籍状態の対応を {\"ファイルの値\": \"在籍・休職・退職\"} の形にしてください"))
        else:
            for k, v in sv.items():
                if v not in hr.STATUSES:
                    out.append((f"status_values.{k}", f"「{k}」を在籍・休職・退職のどれかに当ててください"))
            if cols.get("status") and not sv:
                out.append(("status_values", "在籍状態の値を、在籍・休職・退職に当ててください"))
    elif cols.get("status") and not isinstance(m.get("active_values"), list):
        out.append(("status_values", "在籍状態の値を、在籍・休職・退職に当ててください"))
    if m.get("sheet") is not None and not isinstance(m.get("sheet"), str):
        out.append(("sheet", "シートの名前を文字で入れてください"))
    return out


def parse_mapping(m: dict) -> Mapping:
    found = mapping_problems(m)
    if found:
        raise MappingError(found)
    cols = {k: v.strip() for k, v in m["columns"].items() if isinstance(v, str) and v.strip()}
    sv = m.get("status_values")
    legacy = None
    if not cols.get("status"):
        values = None
    elif sv is not None:
        values = {hr.norm(k): v for k, v in sv.items()}
    else:
        values, legacy = None, {hr.norm(v) for v in m.get("active_values", ["在籍"])}
    return Mapping(cols, values, legacy, m.get("sheet") or None)


def status_value(mapping: Mapping, raw: str) -> str | None:
    """ファイルの値 → 在籍・休職・退職。対応づけていない値は None。"""
    if not mapping.columns.get("status"):
        return "在籍"
    v = hr.norm(raw)
    if mapping.status_values is not None:
        return mapping.status_values.get(v)
    # 前の書き方：在籍とみなす値なら在籍（「休職」だけは休職）、ほかは退職
    if v in (mapping.legacy_active or set()):
        return "休職" if v == "休職" else "在籍"
    return "退職"


# ---------------------------------------------------------------- 読む（割り当てた列だけ）


def headers(path: Path, sheet: str | None = None) -> list[str]:
    """ファイルの列名（対応づけの画面に出す）。中身の値は読まない。"""
    for _line, row in _iter(path, None, sheet):
        return list(row)
    return []


def _iter(path: Path, wanted: list[str] | None, sheet: str | None) -> Iterator[tuple[int, dict[str, str]]]:
    """(行番号, {列名: 値}) を1行ずつ。wanted を渡すと、その列の値だけを拾う（ほかの列は読まない）。
    wanted が None のときは、最初の1回だけ列名（値は空）を返す。"""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        for enc in ("utf-8-sig", "cp932"):
            try:
                with path.open(encoding=enc, newline="") as f:
                    reader = csv.reader(f)
                    head = [h.strip() for h in next(reader, [])]
                    if wanted is None:
                        yield 1, {h: "" for h in head if h}
                        return
                    pos = [(w, head.index(w)) for w in wanted if w in head]
                    for i, r in enumerate(reader, start=2):
                        if not any(c.strip() for c in r):
                            continue
                        yield i, {w: (r[j].strip() if j < len(r) else "") for w, j in pos}
                return
            except UnicodeDecodeError:
                continue
        raise hr.HRImportError(f"{path.name}: 文字コードを読み取れません（UTF-8 か Shift_JIS で保存してください）")
    if suffix == ".json":
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (ValueError, UnicodeDecodeError) as e:
            raise hr.HRImportError(f"{path.name}: JSON として読めません") from e
        if isinstance(data, dict):
            data = next((v for v in data.values() if isinstance(v, list)), [])
        if not isinstance(data, list):
            raise hr.HRImportError(f"{path.name}: 行の並び（配列）が見つかりません")
        if wanted is None:
            names: dict[str, str] = {}
            for r in data[:100]:
                if isinstance(r, dict):
                    names.update({str(k).strip(): "" for k in r})
            yield 1, names
            return
        for i, r in enumerate(data, start=1):
            if not isinstance(r, dict):
                yield i, {}
                continue
            keys = {str(k).strip(): k for k in r}
            yield i, {w: ("" if r.get(keys[w]) is None else str(r.get(keys[w])).strip()) if w in keys else "" for w in wanted}
        return
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        try:
            if sheet and sheet not in wb.sheetnames:
                raise hr.HRImportError(f"{path.name}: シート「{sheet}」がありません（{'・'.join(wb.sheetnames)}）")
            ws = wb[sheet] if sheet else wb.worksheets[0]
            rows = ws.iter_rows(values_only=True)
            head = [str(h).strip() if h is not None else "" for h in next(rows, [])]
            if wanted is None:
                yield 1, {h: "" for h in head if h}
                return
            pos = [(w, head.index(w)) for w in wanted if w in head]
            for i, r in enumerate(rows, start=2):
                if all(v is None for v in r):
                    continue
                yield i, {w: ("" if j >= len(r) or r[j] is None else str(r[j]).strip()) for w, j in pos}
        finally:
            wb.close()
        return
    raise hr.HRImportError(f"{path.name}: 対応していない形式です（CSV / Excel / JSON）")


@dataclass
class Parsed:
    rows: int = 0
    errors: list[dict] = field(default_factory=list)   # {"line", "message"}（先頭 MAX_ERRORS 件）
    error_count: int = 0
    read_columns: list[str] = field(default_factory=list)
    ignored_columns: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)  # 在籍状態ごとの人数

    def error(self, line: int, message: str) -> None:
        self.error_count += 1
        if len(self.errors) < MAX_ERRORS:
            self.errors.append({"line": line, "message": message})


STAGE = "hr_new"


def _stage(db: sqlite3.Connection, path: Path, mapping: Mapping, progress=None) -> Parsed:
    """ファイルを読み、一時表 hr_new に入れる（割り当てた列だけ）。誤りは行番号つきで Parsed に。"""
    out = Parsed()
    head = headers(path, mapping.sheet)
    absent = [f"{hr.LABELS[f]}（{c}）" for f, c in mapping.columns.items() if c not in head]
    if absent:
        out.error(1, f"対応づけた列がファイルにありません：{'、'.join(absent)}")
        return out
    out.read_columns = [mapping.columns[f] for f in hr.FIELDS if f in mapping.columns]
    out.ignored_columns = [h for h in head if h not in out.read_columns]
    _create_stage(db)
    cols = mapping.columns
    wanted = out.read_columns
    seen: dict[str, int] = {}
    batch: list[tuple] = []
    for line, r in _iter(path, wanted, mapping.sheet):
        out.rows += 1
        get = lambda f: r.get(cols[f], "") if f in cols else ""  # noqa: E731
        no = hr.norm(get("employee_no"))
        name, dept = get("name").strip(), get("dept").strip()
        missing = [hr.LABELS[f] for f, v in (("employee_no", no), ("name", name), ("dept", dept)) if not v]
        if missing:
            out.error(line, f"{'・'.join(missing)}が空です")
            continue
        if no in seen:
            out.error(line, f"社員番号 {no} が重複しています（{seen[no]}行目と同じ）")
            continue
        seen[no] = line
        raw = get("status")
        status = status_value(mapping, raw)
        if status is None:
            out.error(line, f"在籍状態「{raw}」を、在籍・休職・退職のどれに当てるか決めていません")
            continue
        batch.append((no, name, get("kana").strip(), dept, get("dept_code").strip(), status, get("email").strip().lower(),
                      int(status in hr.ACTIVE_STATUSES), *hr._keys(name, dept)))
        if len(batch) >= 20000:
            _insert_stage(db, batch)
            batch = []
            if progress:
                progress(out.rows)
    if not out.rows:
        out.error(1, "行がありません")
    _insert_stage(db, batch)
    _keep_unmapped(db, mapping)
    found = dict(db.execute(f"SELECT status, COUNT(*) FROM temp.{STAGE} GROUP BY status").fetchall())
    out.counts = {s: found.get(s, 0) for s in hr.STATUSES}
    return out


def _keep_unmapped(db, mapping: Mapping) -> None:
    """「使わない」にした任意の列は、今いる人の値をそのまま使う（新しく入る人だけ空のまま）。"""
    unmapped = [c for c in OPTIONAL if c not in mapping.columns]
    if not unmapped:
        return
    sets = ", ".join(f"{c} = e.{c}" for c in unmapped)
    if "status" in unmapped:
        sets += ", active = e.active"
    db.execute(f"UPDATE temp.{STAGE} AS n SET {sets} FROM employees AS e WHERE e.employee_no = n.employee_no")


def _add_others(db) -> None:
    """一部のファイル：ファイルにいない人は、今のまま残す（一時表に今の値で足す）。"""
    db.execute(f"INSERT INTO temp.{STAGE} SELECT employee_no, name, kana, dept, dept_code, status, email, active, "
               f"name_key, surname_key, dept_key FROM employees WHERE employee_no NOT IN (SELECT employee_no FROM temp.{STAGE})")


def _create_stage(db) -> None:
    db.execute(f"DROP TABLE IF EXISTS temp.{STAGE}")
    db.execute(f"""CREATE TEMP TABLE {STAGE} (employee_no TEXT PRIMARY KEY, name TEXT, kana TEXT, dept TEXT, dept_code TEXT,
                   status TEXT, email TEXT, active INTEGER, name_key TEXT, surname_key TEXT, dept_key TEXT)""")


def _insert_stage(db, batch) -> None:
    if batch:
        db.executemany(f"INSERT INTO temp.{STAGE} VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", batch)


def _stage_from_backup(db, backup_id: int) -> Parsed:
    _create_stage(db)
    t = _backup_table(backup_id)
    db.execute(f"INSERT INTO temp.{STAGE} SELECT employee_no, name, kana, dept, dept_code, status, email, active, "
               f"name_key, surname_key, dept_key FROM {t}")
    out = Parsed()
    out.rows = db.execute(f"SELECT COUNT(*) FROM temp.{STAGE}").fetchone()[0]
    out.counts = {s: n for s, n in db.execute(f"SELECT status, COUNT(*) FROM temp.{STAGE} GROUP BY status")}
    out.counts = {s: out.counts.get(s, 0) for s in hr.STATUSES}
    return out


# ---------------------------------------------------------------- 比べる（試しの結果）


DIFF_WHERE = " OR ".join(f"n.{c} IS NOT e.{c}" for c in COMPARED)


def _person(r) -> dict:
    return dict(zip(("employee_no", "name", "dept", "status"), r))


def _diff(db) -> dict:
    s = STAGE
    added_n = db.execute(f"SELECT COUNT(*) FROM temp.{s} n LEFT JOIN employees e USING (employee_no) "
                         "WHERE e.employee_no IS NULL").fetchone()[0]
    added = [_person(r) for r in db.execute(
        f"SELECT n.employee_no, n.name, n.dept, n.status FROM temp.{s} n LEFT JOIN employees e USING (employee_no) "
        f"WHERE e.employee_no IS NULL ORDER BY n.employee_no LIMIT {SAMPLE}")]
    removed_n = db.execute(f"SELECT COUNT(*) FROM employees e LEFT JOIN temp.{s} n USING (employee_no) "
                           "WHERE n.employee_no IS NULL").fetchone()[0]
    removed = [_person(r) for r in db.execute(
        f"SELECT e.employee_no, e.name, e.dept, e.status FROM employees e LEFT JOIN temp.{s} n USING (employee_no) "
        f"WHERE n.employee_no IS NULL ORDER BY e.employee_no LIMIT {SAMPLE}")]
    counts = db.execute("SELECT COUNT(*), " + ", ".join(f"SUM(n.{c} IS NOT e.{c})" for c in COMPARED) +
                        f" FROM temp.{s} n JOIN employees e USING (employee_no) WHERE {DIFF_WHERE}").fetchone()
    changed_n = counts[0]
    by_field = {CHANGE_LABELS[c]: int(counts[i + 1] or 0) for i, c in enumerate(COMPARED)}
    changed = []
    for r in db.execute(f"SELECT n.employee_no, e.name, n.name, e.dept, n.dept, e.status, n.status, "
                        + ", ".join(f"n.{c} IS NOT e.{c}" for c in COMPARED) +
                        f" FROM temp.{s} n JOIN employees e USING (employee_no) WHERE {DIFF_WHERE} "
                        f"ORDER BY n.employee_no LIMIT {SAMPLE}"):
        what = [CHANGE_LABELS[c] for c, flag in zip(COMPARED, r[7:]) if flag]
        changes = []
        if r[1] != r[2]:
            changes.append(f"名前 {r[1]}→{r[2]}")
        if r[3] != r[4]:
            changes.append(f"部署 {r[3]}→{r[4]}")
        if r[5] != r[6]:
            changes.append(f"在籍状態 {r[5]}→{r[6]}")
        changes += [w for w in what if w not in ("名前", "部署", "在籍状態")]
        changed.append({"employee_no": r[0], "name": r[2], "dept": r[4], "status": r[6], "changes": changes})
    return {"added": {"count": added_n, "first": added},
            "changed": {"count": changed_n, "first": changed, "by_field": by_field},
            "removed": {"count": removed_n, "first": removed}}


def _changed_numbers(db, limit: int) -> tuple[list[str], int]:
    """入る・変わる・いなくなる人の社員番号（監査ログ用。氏名やメールアドレスは入れない）。"""
    s = STAGE
    sql = (f"SELECT n.employee_no FROM temp.{s} n LEFT JOIN employees e USING (employee_no) "
           f"WHERE e.employee_no IS NULL OR {DIFF_WHERE} "
           f"UNION SELECT e.employee_no FROM employees e LEFT JOIN temp.{s} n USING (employee_no) WHERE n.employee_no IS NULL")
    total = db.execute(f"SELECT COUNT(*) FROM ({sql})").fetchone()[0]
    nos = [r[0] for r in db.execute(f"SELECT employee_no FROM ({sql}) ORDER BY employee_no LIMIT {int(limit)}")]
    return nos, total


def _state_groups(data: dict, account_id: str, dept: str | None, status: str | None) -> set[str]:
    """その人（部署・在籍状態）が入るグループ。退職・いない人は、どこにも入らない。"""
    from . import access

    if dept is None or status in (None, "退職"):
        return set()
    return set(access.compute_memberships(data, account_id, dept, status))


def _group_effects(db, data: dict) -> list[dict]:
    """新しく入る人・部署や在籍状態が変わる人・いなくなる人の、入るグループ・外れるグループ（グループごとの人数）。
    決まり（全員・部署）で入るグループは、(前の部署, 前の状態, 後の部署, 後の状態) ごとにまとめて数える
    （新しく入る人は「前」がなく、いなくなる人は「後」がない）。取り込みと「1つ前に戻す」で同じ数え方をする。
    手で入れたグループは、メンバーの人だけを1人ずつ見る。"""
    s = STAGE
    manual = {m["account_id"] for m in data["members"]}
    join: Counter = Counter()
    leave: Counter = Counter()
    rule_only = {**data, "members": []}
    combos = db.execute(
        f"SELECT e.dept, e.status, n.dept, n.status, COUNT(*) FROM employees e JOIN temp.{s} n USING (employee_no) "
        f"WHERE e.dept IS NOT n.dept OR e.status IS NOT n.status GROUP BY 1, 2, 3, 4").fetchall()
    combos += [(None, None, d, st, c) for d, st, c in db.execute(
        f"SELECT n.dept, n.status, COUNT(*) FROM temp.{s} n LEFT JOIN employees e USING (employee_no) "
        f"WHERE e.employee_no IS NULL GROUP BY 1, 2")]
    combos += [(d, st, None, None, c) for d, st, c in db.execute(
        f"SELECT e.dept, e.status, COUNT(*) FROM employees e LEFT JOIN temp.{s} n USING (employee_no) "
        f"WHERE n.employee_no IS NULL GROUP BY 1, 2")]
    for od, ost, nd, nst, c in combos:
        before = _state_groups(rule_only, "", od, ost)
        after = _state_groups(rule_only, "", nd, nst)
        for g in after - before:
            join[g] += c
        for g in before - after:
            leave[g] += c
    # 手で入れたグループ：退職になる人・いなくなる人は、手で入れたグループからも外れる
    if manual:
        nos = sorted(no for no in manual if not no.startswith("sys:"))
        for i in range(0, len(nos), 500):
            part = nos[i:i + 500]
            q = ",".join("?" * len(part))
            for no, od, ost, nd, nst in db.execute(
                    f"SELECT e.employee_no, e.dept, e.status, n.dept, n.status FROM employees e "
                    f"LEFT JOIN temp.{s} n USING (employee_no) WHERE e.employee_no IN ({q}) "
                    f"AND (n.employee_no IS NULL OR (n.status = '退職' AND e.status <> '退職'))", part):
                mine = {m["group_id"] for m in data["members"] if m["account_id"] == no}
                rules_before = _state_groups(rule_only, no, od, ost)
                for g in mine - rules_before:
                    leave[g] += 1
    out = []
    for gid, g in data["groups"].items():
        if join[gid] or leave[gid]:
            out.append({"group_id": gid, "name": g["name"], "join": join[gid], "leave": leave[gid]})
    return out


def _assign_holders(db, data: dict, leave_policy: str, table: str) -> int:
    """roles.assign を持つ人の数（締め出しを防ぐ）。table は今の employees か、確定したあとの一時表。グループごとに SQL で数える。"""
    from . import access

    gids = set()
    for gr in data["grants"]:
        role = data["roles"].get(gr["role_id"])
        if role and "roles.assign" in role["permissions"]:
            if leave_policy == LEAVE_EMPLOYEE_ONLY and role["name"] != "社員":
                gids.add((gr["group_id"], False))      # 休職の人はこの役割を持たない
            else:
                gids.add((gr["group_id"], True))
    holders: set[str] = set()
    count = 0
    for gid, leave_ok in gids:
        g = data["groups"].get(gid)
        if not g:
            continue
        statuses = "('在籍', '休職')" if leave_ok else "('在籍')"
        today = date.today().isoformat()
        for m in data["members"]:
            if m["group_id"] == gid and not access._expired(m["expires_at"], today) and not m["account_id"].startswith("sys:"):
                row = db.execute(f"SELECT 1 FROM {table} WHERE employee_no = ? AND status IN {statuses}",
                                 (m["account_id"],)).fetchone()
                if row:
                    holders.add(m["account_id"])
        rule = g["rule"]
        if rule and rule.get("all"):
            count += db.execute(f"SELECT COUNT(*) FROM {table} WHERE status IN {statuses}").fetchone()[0]
        elif rule and rule.get("departments"):
            depts = rule["departments"]
            count += db.execute(f"SELECT COUNT(*) FROM {table} WHERE status IN {statuses} "
                                f"AND dept IN ({','.join('?' * len(depts))})", depts).fetchone()[0]
    return count + len(holders)


@dataclass
class Preview:
    source: str
    rows: int
    errors: list[dict]
    error_count: int
    read_columns: list[str]
    ignored_columns: list[str]
    counts: dict[str, int]
    diff: dict
    groups: list[dict]
    lockout: bool
    retired_members: list[dict]           # 退職・いなくなるので、手で入れたグループから外れる人（社員番号とグループ）
    scope: str = SCOPE_PARTIAL
    current_total: int = 0                # 今の人事データの人数

    @property
    def ok(self) -> bool:
        return not self.error_count and not self.lockout

    @property
    def removal_warning(self) -> dict | None:
        """いなくなる人が今の人数の REMOVAL_WARN を超えるとき {count, percent, message}（確かめてからでないと確定しない）。"""
        n = self.diff["removed"]["count"]
        if not self.current_total or n <= self.current_total * REMOVAL_WARN:
            return None
        pct = round(100 * n / self.current_total, 1)
        return {"count": n, "percent": pct, "message": f"いなくなる人が {n}人（全体の {pct:g}%）います"}

    def as_dict(self) -> dict:
        return {"source": self.source, "rows": self.rows, "errors": self.errors, "error_count": self.error_count,
                "read_columns": self.read_columns, "ignored_columns": self.ignored_columns, "counts": self.counts,
                **self.diff, "groups": self.groups, "lockout": self.lockout, "ok": self.ok,
                "retired_members": len(self.retired_members), "scope": self.scope, "scope_label": SCOPE_LABELS[self.scope],
                "current_total": self.current_total, "removal_warning": self.removal_warning}


def _retired_members(db, data: dict) -> list[dict]:
    s = STAGE
    out = []
    nos = sorted({m["account_id"] for m in data["members"] if not m["account_id"].startswith("sys:")})
    for i in range(0, len(nos), 500):
        part = nos[i:i + 500]
        for (no,) in db.execute(
                f"SELECT e.employee_no FROM employees e LEFT JOIN temp.{s} n USING (employee_no) "
                f"WHERE e.employee_no IN ({','.join('?' * len(part))}) AND (n.employee_no IS NULL OR n.status = '退職')", part):
            groups = [m["group_id"] for m in data["members"] if m["account_id"] == no]
            names = [data["groups"][g]["name"] for g in groups if g in data["groups"]]
            out.append({"employee_no": no, "groups": groups, "group_names": names})
    return out


def _empty_diff() -> dict:
    return {"added": {"count": 0, "first": []}, "changed": {"count": 0, "first": [], "by_field": {}},
            "removed": {"count": 0, "first": []}}


def _preview_staged(db, parsed: Parsed, source: str, leave_policy: str, scope: str = SCOPE_PARTIAL) -> Preview:
    from . import access

    if scope not in SCOPE_LABELS:
        raise hr.HRImportError(f"取り込みの種類が正しくありません：{scope}")
    current = db.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
    if parsed.error_count:
        return Preview(source, parsed.rows, parsed.errors, parsed.error_count, parsed.read_columns, parsed.ignored_columns,
                       parsed.counts, _empty_diff(), [], False, [], scope, current)
    if scope == SCOPE_PARTIAL:
        _add_others(db)
    data = access.load(db)
    # 今は roles.assign を持つ人がいて、確定するといなくなるときだけ止める（まだだれも持っていないときは止めない）
    lockout = (_assign_holders(db, data, leave_policy, f"temp.{STAGE}") == 0
               and _assign_holders(db, data, leave_policy, "employees") > 0)
    return Preview(source, parsed.rows, [], 0, parsed.read_columns, parsed.ignored_columns, parsed.counts,
                   _diff(db), _group_effects(db, data), lockout, _retired_members(db, data), scope, current)


def preview(db, path: Path, mapping: dict, leave_policy: str = LEAVE_KEEP, progress=None, scope: str = SCOPE_PARTIAL,
            source_name: str | None = None) -> Preview:
    """試しの結果（保存しない）。source_name は上げたときのファイル名（画面に出す。なければ path の名前）。"""
    ensure_schema(db)
    parsed = _stage(db, path, parse_mapping(mapping), progress)
    try:
        return _preview_staged(db, parsed, source_name or path.name, leave_policy, scope)
    finally:
        db.execute(f"DROP TABLE IF EXISTS temp.{STAGE}")


# ---------------------------------------------------------------- 確定


def _backup_table(backup_id: int) -> str:
    return f"hr_backup_{int(backup_id)}"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Result:
    preview: Preview
    committed: bool
    import_kind: str
    changed_numbers: list[str] = field(default_factory=list)
    changed_total: int = 0
    removed_members: list[dict] = field(default_factory=list)   # 手で入れたグループから外した人 {employee_no, groups}
    reason: str = ""

    def as_dict(self) -> dict:
        return {**self.preview.as_dict(), "committed": self.committed, "kind": self.import_kind, "reason": self.reason,
                "removed_members": len(self.removed_members)}

    def audit_detail(self, limit: int = 200) -> str:
        p = self.preview
        d = p.as_dict()
        by = d["changed"]["by_field"]
        parts = "・".join(f"{k} {v}" for k, v in by.items() if v)
        head = (f"{d['scope_label']}：入る {d['added']['count']}人・変わる {d['changed']['count']}人"
                + (f"（{parts}）" if parts else "") + f"・いなくなる {d['removed']['count']}人")
        if self.changed_numbers:
            more = self.changed_total - len(self.changed_numbers)
            head += "。社員番号：" + "・".join(self.changed_numbers) + (f" ほか {more}人" if more > 0 else "")
        return head


def _commit_staged(db, parsed: Parsed, source: str, kind: str, by: str, leave_policy: str,
                   today: str, fail_hook: Callable[[str], None] | None = None, scope: str = SCOPE_PARTIAL,
                   confirm_removal: bool = False) -> Result:
    from . import access

    p = _preview_staged(db, parsed, source, leave_policy, scope)
    if not p.ok:
        reason = ("誤りがあるので確定しません" if p.error_count else
                  "確定すると、roles.assign（権限を変える権限）を持つ人がいなくなるので確定しません")
        return Result(p, False, kind, reason=reason)
    if p.removal_warning and not confirm_removal:
        return Result(p, False, kind, reason=p.removal_warning["message"] + "。確かめてから、もう一度確定してください")
    numbers, total = _changed_numbers(db, 200)
    s = STAGE
    db.commit()                                 # 一時表に入れた分（読み込み）を区切ってから、入れ替えを1つのトランザクションで
    db.execute("BEGIN IMMEDIATE")
    try:
        # 1. 今のデータを控えに写す
        current_source = (db.execute("SELECT source FROM hr_imports ORDER BY rowid DESC LIMIT 1").fetchone() or ["（前の取り込み）"])[0]
        tot, act = db.execute("SELECT COUNT(*), COALESCE(SUM(active), 0) FROM employees").fetchone()
        cur = db.execute("INSERT INTO hr_backups (saved_at, source, total, active) VALUES (?, ?, ?, ?)",
                         (_now(), current_source, tot, act))
        bid = cur.lastrowid
        db.execute(f"CREATE TABLE {_backup_table(bid)} AS SELECT employee_no, name, kana, dept, dept_code, status, email, "
                   "active, name_key, surname_key, dept_key, changed_at FROM employees")
        if fail_hook:
            fail_hook("backup")
        # 2. まるごと入れ替える（変わらない人は「最後に変わった日」をそのまま）
        db.execute(f"CREATE TEMP TABLE hr_final AS SELECT n.*, CASE WHEN e.employee_no IS NULL OR {DIFF_WHERE} "
                   f"THEN ? ELSE e.changed_at END AS changed_at FROM temp.{s} n LEFT JOIN employees e USING (employee_no)",
                   (today,))
        db.execute("DELETE FROM employees")
        db.execute("INSERT INTO employees (employee_no, name, kana, dept, dept_code, status, email, active, name_key, "
                   "surname_key, dept_key, changed_at) SELECT employee_no, name, kana, dept, dept_code, status, email, "
                   "active, name_key, surname_key, dept_key, changed_at FROM temp.hr_final")
        db.execute("DROP TABLE temp.hr_final")
        if fail_hook:
            fail_hook("replace")
        # 3. 部署の一覧（在籍者のいる部署）
        db.execute("DELETE FROM hr_departments")
        db.execute("INSERT OR IGNORE INTO hr_departments SELECT dept, MAX(dept_code) FROM employees WHERE active = 1 GROUP BY dept")
        # 4. 退職した人・いなくなった人を、手で入れたグループから外す（在籍に戻っても自動では戻さない）
        for r in p.retired_members:
            db.execute("DELETE FROM group_members WHERE account_id = ?", (r["employee_no"],))
        # 5. 記録と、古い控えを消す
        active = db.execute("SELECT COUNT(*) FROM employees WHERE active = 1").fetchone()[0]
        total_now = db.execute("SELECT COUNT(*) FROM employees").fetchone()[0]
        d = p.diff
        db.execute("INSERT INTO hr_imports (imported_at, source, active, inactive, kind, by_who, added, changed, removed, scope) "
                   "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                   (_now(), source, active, total_now - active, kind, by, d["added"]["count"], d["changed"]["count"],
                    d["removed"]["count"], SCOPE_LABELS[scope]))
        for (old,) in db.execute("SELECT backup_id FROM hr_backups ORDER BY backup_id DESC LIMIT -1 OFFSET ?", (HR_KEEP,)).fetchall():
            db.execute(f"DROP TABLE IF EXISTS {_backup_table(old)}")
            db.execute("DELETE FROM hr_backups WHERE backup_id = ?", (old,))
        if fail_hook:
            fail_hook("finish")
        # 6. 権限の結果を作り直す（世代を上げると、判定の控えが作り直される）
        access._bump(db)
        db.commit()
    except BaseException:
        db.rollback()
        db.execute("DROP TABLE IF EXISTS temp.hr_final")
        raise
    hr._dept_aliases.cache_clear()
    access.sync_targets(db)                     # 部署の対象をそろえる
    return Result(p, True, kind, numbers, total, p.retired_members)


def commit(db, path: Path, mapping: dict, by: str, leave_policy: str = LEAVE_KEEP, today: str | None = None,
           progress=None, fail_hook=None, scope: str = SCOPE_PARTIAL, confirm_removal: bool = False,
           source_name: str | None = None) -> Result:
    """確定：まとめて1回で入れ替える。誤りがあれば何も変えない（committed = False）。
    全員のファイルで、いなくなる人が多いとき（removal_warning）は、confirm_removal がなければ確定しない。"""
    ensure_schema(db)
    parsed = _stage(db, path, parse_mapping(mapping), progress)
    try:
        return _commit_staged(db, parsed, source_name or path.name, "取り込み", by, leave_policy,
                              today or date.today().isoformat(), fail_hook, scope, confirm_removal)
    finally:
        db.execute(f"DROP TABLE IF EXISTS temp.{STAGE}")


def backups(db) -> list[dict]:
    ensure_schema(db)
    return [{"backup_id": r[0], "saved_at": r[1], "source": r[2], "total": r[3], "active": r[4]}
            for r in db.execute("SELECT backup_id, saved_at, source, total, active FROM hr_backups ORDER BY backup_id DESC")]


def _latest_backup(db) -> int | None:
    row = db.execute("SELECT backup_id FROM hr_backups ORDER BY backup_id DESC LIMIT 1").fetchone()
    return row[0] if row else None


class NoBackup(hr.HRImportError):
    pass


RESTORED = "（戻した）"
_OLD_RESTORED = "（1つ前に戻した）"      # 前の版の名前の付け方


def restored_name(source: str) -> str:
    """戻したときの記録の名前：「元のファイル名（戻した）」。何回戻しても「（戻した）」は1つだけ。"""
    base = source
    while True:
        for suffix in (RESTORED, _OLD_RESTORED):
            if base.endswith(suffix):
                base = base[: -len(suffix)]
                break
        else:
            return base + RESTORED


def preview_restore(db, leave_policy: str = LEAVE_KEEP) -> Preview:
    ensure_schema(db)
    bid = _latest_backup(db)
    if bid is None:
        raise NoBackup("戻せる前の取り込みがありません")
    parsed = _stage_from_backup(db, bid)
    try:
        return _preview_staged(db, parsed, f"控え {bid}", leave_policy, SCOPE_ALL)
    finally:
        db.execute(f"DROP TABLE IF EXISTS temp.{STAGE}")


def restore(db, by: str, leave_policy: str = LEAVE_KEEP, today: str | None = None, confirm_removal: bool = False) -> Result:
    """1つ前（いちばん新しい控え）に戻す。取り込みと同じ流れで入れるので、戻したこともまた戻せる。"""
    ensure_schema(db)
    bid = _latest_backup(db)
    if bid is None:
        raise NoBackup("戻せる前の取り込みがありません")
    source = db.execute("SELECT source FROM hr_backups WHERE backup_id = ?", (bid,)).fetchone()[0]
    parsed = _stage_from_backup(db, bid)
    try:
        # 控えは全員のデータなので、全員のファイルと同じに扱う（いなくなる人が多いときは確かめる）
        return _commit_staged(db, parsed, restored_name(source), "戻す", by, leave_policy,
                              today or date.today().isoformat(), scope=SCOPE_ALL, confirm_removal=confirm_removal)
    finally:
        db.execute(f"DROP TABLE IF EXISTS temp.{STAGE}")


def history(db, limit: int = 10) -> list[dict]:
    ensure_schema(db)
    return [dict(zip(("imported_at", "source", "active", "inactive", "kind", "by", "added", "changed", "removed", "scope"), r))
            for r in db.execute("SELECT imported_at, source, active, inactive, kind, by_who, added, changed, removed, scope "
                                "FROM hr_imports ORDER BY rowid DESC LIMIT ?", (limit,))]


# ---------------------------------------------------------------- 一覧（hr.view）


def employees(db, departments, q: str = "", dept: str = "", offset: int = 0, limit: int = 100) -> dict:
    """一覧：社員番号・氏名・部署・在籍状態・最後に変わった日。departments は見てよい部署（None ならすべて）。"""
    ensure_schema(db)
    where, args = [], []
    if departments is not None:
        if not departments:
            return {"employees": [], "total": 0}
        where.append(f"dept IN ({','.join('?' * len(departments))})")
        args += sorted(departments)
    if dept:
        where.append("dept = ?")
        args.append(dept)
    if q.strip():
        key = hr.name_key(q)
        like = key.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        where.append("(employee_no LIKE ? ESCAPE '\\' OR name_key LIKE ? ESCAPE '\\')")
        args += [like + "%", "%" + like + "%"]
    sql = " WHERE " + " AND ".join(where) if where else ""
    total = db.execute(f"SELECT COUNT(*) FROM employees{sql}", args).fetchone()[0]
    rows = db.execute(f"SELECT employee_no, name, dept, status, changed_at FROM employees{sql} "
                      f"ORDER BY employee_no LIMIT ? OFFSET ?", [*args, int(limit), int(offset)]).fetchall()
    return {"employees": [dict(zip(("employee_no", "name", "dept", "status", "changed_at"), r)) for r in rows], "total": total}


def department_names(db, departments=None) -> list[str]:
    ensure_schema(db)
    names = sorted({d for (d,) in db.execute("SELECT DISTINCT dept FROM employees")})
    return names if departments is None else [d for d in names if d in departments]


def audit_records(result: "Result", source: str) -> list[dict]:
    """監査ログに残すもの（件数と社員番号だけ。氏名やメールアドレスは入れない）。"""
    action = "hr.restore" if result.import_kind == "戻す" else "hr.import"
    if not result.committed:
        p = result.preview
        why = result.reason + (f"（誤り {p.error_count}件）" if p.error_count else "")
        return [{"action": action, "target": source, "result": "失敗", "detail": why}]
    out = [{"action": action, "target": source, "result": "成功", "detail": result.audit_detail()}]
    for r in result.removed_members:
        out.append({"action": "roles.member.remove", "target": r["employee_no"], "result": "成功",
                    "detail": f"退職したか人事データにいないので、{r['employee_no']} を手で入れたグループ"
                              f"「{'」「'.join(r['group_names'])}」から外した（人事データの取り込み。在籍に戻っても自動では戻さない）"})
    return out
