"""人事データの取り込みと、本人確認の照合。

人事DBには直接つながない。人事システムから出力したファイル（CSV / Excel / JSON）を取り込む。
人事システムごとに列名や形式が違うので、「対応表（mapping）」で Yanua の決まった項目に読み替える。

- 取り込むのは本人確認に必要な項目だけ（従業員番号・氏名・部署・部署番号・在籍の状態）。
  生年月日や住所などの列が出力に含まれていても、読み込んだ時点で捨てる。
- 取り込みは毎回まるごと入れ替える（人事システムの出力を正とする）。
- 在籍していない人（退職者など）は、照合で一致しない。
- 最後に取り込んだ日時を記録し、古くなったら警告できるようにする。

対応表（JSON）の例:

    {
      "columns": {"employee_no": "社員番号", "name": "氏名", "dept": "所属", "dept_code": "所属コード", "status": "在籍区分"},
      "active_values": ["在籍", "休職"],
      "sheet": "社員一覧"
    }

"status" を対応させない場合は、全員を在籍として扱う。
"""

from __future__ import annotations

import csv
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .llm import ChatModel

FIELDS = ("employee_no", "name", "dept", "dept_code", "status")
REQUIRED = ("employee_no", "name", "dept")
LABELS = {
    "employee_no": "従業員番号",
    "name": "氏名",
    "dept": "部署",
    "dept_code": "部署番号",
    "status": "在籍の状態",
}

# 列名から対応表の下書きを作るときの手がかり（決まったルール。AIはこれで決まらないときだけ使う）
HINTS = {
    "employee_no": ["社員番号", "従業員番号", "社員no", "社員コード", "従業員コード", "職員番号", "employeeid", "employee_no", "empno", "社員id"],
    "name": ["氏名", "名前", "社員名", "従業員名", "fullname", "name", "漢字氏名"],
    "dept": ["所属", "部署", "所属部署", "部署名", "所属名", "department", "dept", "組織名"],
    "dept_code": ["所属コード", "部署コード", "部署番号", "所属番号", "組織コード", "deptcode", "departmentcode"],
    "status": ["在籍区分", "在籍状況", "在籍", "雇用状態", "ステータス", "status", "退職区分"],
}

HR_SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    employee_no TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    dept        TEXT NOT NULL,
    dept_code   TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS hr_imports (
    imported_at TEXT NOT NULL,
    source      TEXT NOT NULL,
    active      INTEGER NOT NULL,
    inactive    INTEGER NOT NULL
);
"""


class HRImportError(ValueError):
    """取り込めない人事データ・対応表。"""


@dataclass
class HRImportReport:
    active: int = 0
    inactive: int = 0
    dropped_columns: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"active": self.active, "inactive": self.inactive,
                "dropped_columns": self.dropped_columns, "errors": self.errors}


def norm(text: str) -> str:
    """照合用：全角・半角をそろえ、空白をすべて除く。"""
    return re.sub(r"\s", "", unicodedata.normalize("NFKC", str(text or "")))


def _key(text: str) -> str:
    return norm(text).lower().replace("_", "").replace("-", "")


# ---------------------------------------------------------------- ファイルの読み込み

def read_rows(path: Path, sheet: str | None = None) -> list[dict[str, str]]:
    """人事システムの出力を、列名→値 の行の並びとして読む。"""
    suffix = path.suffix.lower()
    if suffix == ".csv":
        # Excelで保存したCSVはBOM付きのことが多いので utf-8-sig で読む。Shift_JIS も試す
        for enc in ("utf-8-sig", "cp932"):
            try:
                with path.open(encoding=enc, newline="") as f:
                    return [{k.strip(): (v or "").strip() for k, v in r.items() if k} for r in csv.DictReader(f)]
            except UnicodeDecodeError:
                continue
        raise HRImportError(f"{path.name}: 文字コードを読み取れません（UTF-8 か Shift_JIS で保存してください）")
    if suffix == ".json":
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, dict):  # {"employees": [...]} のような形にも対応
            data = next((v for v in data.values() if isinstance(v, list)), [])
        if not isinstance(data, list):
            raise HRImportError(f"{path.name}: 行の並び（配列）が見つかりません")
        return [{str(k).strip(): "" if v is None else str(v).strip() for k, v in r.items()} for r in data]
    if suffix in (".xlsx", ".xlsm"):
        from openpyxl import load_workbook

        wb = load_workbook(path, read_only=True, data_only=True)
        ws = wb[sheet] if sheet else wb.worksheets[0]
        rows = ws.iter_rows(values_only=True)
        header = [str(h).strip() if h is not None else "" for h in next(rows, [])]
        out = []
        for r in rows:
            if all(v is None for v in r):
                continue
            out.append({h: "" if v is None else str(v).strip() for h, v in zip(header, r) if h})
        wb.close()
        return out
    raise HRImportError(f"{path.name}: 対応していない形式です（CSV / Excel / JSON）")


def load_mapping(path: Path) -> dict:
    m = json.loads(path.read_text(encoding="utf-8"))
    cols = m.get("columns") or {}
    missing = [LABELS[f] for f in REQUIRED if not cols.get(f)]
    if missing:
        raise HRImportError(f"{path.name}: 対応表に {'・'.join(missing)} の列名がありません")
    return m


# ---------------------------------------------------------------- 対応表の下書き

def suggest_mapping(headers: list[str], llm: ChatModel | None = None) -> dict:
    """列名から対応表の下書きを作る。まず決まったルールで当て、決まらない項目だけAIに聞く。

    AIに渡すのは列名だけ（中身の個人情報は渡さない）。下書きは人が確認してから保存する前提。
    """
    cols: dict[str, str] = {}
    for f in FIELDS:
        hints = [_key(h) for h in HINTS[f]]
        for h in headers:
            if h in cols.values():
                continue
            if _key(h) in hints:
                cols[f] = h
                break
    missing = [f for f in FIELDS if f not in cols]
    if missing and llm is not None:
        prompt = (
            "人事システムが出力したファイルの列名の一覧です。次の項目に当たる列名を、一覧の中から選んでJSONで返してください。"
            "当たる列がなければ空文字にしてください。\n"
            + "\n".join(f"- {f}: {LABELS[f]}" for f in missing)
            + f"\n\n列名の一覧: {json.dumps(headers, ensure_ascii=False)}"
        )
        try:
            text = llm.chat([{"role": "user", "content": prompt}])
            data = json.loads(re.search(r"\{.*\}", text, re.S).group(0))
            for f in missing:
                v = str(data.get(f, "")).strip()
                if v in headers and v not in cols.values():   # 一覧にない列名は採らない
                    cols[f] = v
        except Exception:  # noqa: BLE001  AIの下書きは補助なので、失敗しても止めない
            pass
    return {"columns": cols, "active_values": ["在籍"]}


# ---------------------------------------------------------------- 取り込みと照合

def ensure_schema(db) -> None:
    db.executescript(HR_SCHEMA)


def import_employees(db, path: Path, mapping: dict) -> HRImportReport:
    """人事データを取り込む。対応表の列以外は読み込んだ時点で捨てる。毎回まるごと入れ替える。"""
    ensure_schema(db)
    cols = mapping["columns"]
    rows = read_rows(path, mapping.get("sheet"))
    report = HRImportReport()
    if not rows:
        report.errors.append(f"{path.name}: 行がありません")
        return report

    headers = list(rows[0].keys())
    absent = [f"{LABELS[f]}（{cols[f]}）" for f in FIELDS if cols.get(f) and cols[f] not in headers]
    if absent:
        report.errors.append(f"{path.name}: 対応表の列がファイルにありません: {'、'.join(absent)}")
        return report
    report.dropped_columns = [h for h in headers if h not in cols.values()]

    active_values = {norm(v) for v in mapping.get("active_values", ["在籍"])}
    records = {}
    for i, r in enumerate(rows, start=2):
        rec = {f: r.get(cols[f], "") if cols.get(f) else "" for f in FIELDS}
        no = norm(rec["employee_no"])
        if not no or not rec["name"] or not rec["dept"]:
            report.errors.append(f"{path.name} {i}行目: 従業員番号・氏名・部署のどれかが空です")
            continue
        if no in records:
            report.errors.append(f"{path.name} {i}行目: 従業員番号 {no} が重複しています")
            continue
        active = (norm(rec["status"]) in active_values) if cols.get("status") else True
        records[no] = (no, rec["name"].strip(), rec["dept"].strip(), rec["dept_code"].strip(), int(active))

    if report.errors:
        return report   # 1件でも不備があれば入れ替えない（中途半端な名簿にしない）

    with db:
        db.execute("DELETE FROM employees")
        db.executemany("INSERT INTO employees VALUES (?, ?, ?, ?, ?)", list(records.values()))
        report.active = sum(1 for r in records.values() if r[4])
        report.inactive = len(records) - report.active
        db.execute(
            "INSERT INTO hr_imports VALUES (?, ?, ?, ?)",
            (datetime.now(timezone.utc).isoformat(timespec="seconds"), path.name, report.active, report.inactive),
        )
    return report


def verify(db, employee_no: str, name: str) -> dict | None:
    """従業員番号と氏名の両方が、在籍中の人と一致したときだけ、その人の情報を返す。"""
    ensure_schema(db)
    row = db.execute(
        "SELECT employee_no, name, dept, dept_code FROM employees WHERE employee_no = ? AND active = 1",
        (norm(employee_no),),
    ).fetchone()
    if row and norm(row[1]) == norm(name):
        return {"employee_no": row[0], "name": row[1], "dept": row[2], "dept_code": row[3]}
    return None


def lookup(db, employee_no: str) -> dict | None:
    """SSOの個人アカウントから本人の候補を出すとき用（在籍中の人だけ）。"""
    ensure_schema(db)
    row = db.execute(
        "SELECT employee_no, name, dept, dept_code FROM employees WHERE employee_no = ? AND active = 1",
        (norm(employee_no),),
    ).fetchone()
    return {"employee_no": row[0], "name": row[1], "dept": row[2], "dept_code": row[3]} if row else None


def last_import(db) -> dict | None:
    ensure_schema(db)
    row = db.execute("SELECT imported_at, source, active, inactive FROM hr_imports ORDER BY imported_at DESC LIMIT 1").fetchone()
    if not row:
        return None
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).days
    return {"imported_at": row[0], "source": row[1], "active": row[2], "inactive": row[3], "age_days": age}


def find_in_text(db, text: str) -> list[dict]:
    """文の中に名前が出てくる在籍中の社員を探す（受付で、訪ね先の社員を見つけるとき用）。

    フルネームが含まれていればその人だけ。なければ姓で探し、同じ姓が複数いればすべて返す（呼び出し側で聞き直す）。
    結果は社内の取次に使うだけで、社外の人に見せない。
    """
    ensure_schema(db)
    t = _key(text)
    rows = [
        {"employee_no": r[0], "name": r[1], "dept": r[2], "dept_code": r[3]}
        for r in db.execute("SELECT employee_no, name, dept, dept_code FROM employees WHERE active = 1")
    ]
    full = [r for r in rows if _key(r["name"]) and _key(r["name"]) in t]
    if full:
        return full
    def surname(name: str) -> str:
        parts = re.split(r"\s+", unicodedata.normalize("NFKC", name).strip())
        return parts[0] if len(parts) > 1 else ""
    return [r for r in rows if len(surname(r["name"])) >= 2 and _key(surname(r["name"])) in t]
