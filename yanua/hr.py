"""人事データの取り込みと、本人確認の照合。

人事DBには直接つながない。人事システムから出力したファイル（CSV / Excel / JSON）を取り込む。
人事システムごとに列名や形式が違うので、「対応表（mapping）」で Yanua の決まった項目に読み替える。

- 取り込むのは本人確認と取次・権限に必要な項目だけ（従業員番号・氏名・よみ・部署・部署番号・在籍の状態・メールアドレス）。
  メールアドレスは任意で、Googleカレンダーなどで登録した来訪予定の担当者を、社員番号に結びつけるときに使う。
  生年月日や住所などの列が出力に含まれていても、その列は読まない（yanua/hrimport.py）。
- 取り込みは毎回まるごと入れ替える（人事システムの出力を正とする）。試し・確定・前に戻すは yanua/hrimport.py。
- 在籍の状態は「在籍」「休職」「退職」の3つ。退職した人は、照合で一致しない（ログインもできない）。
  休職中の人は照合で一致する。ログインと権限は設定（S-52 の「人事」）で決める
- 最後に取り込んだ日時を記録し、古くなったら警告できるようにする。

対応表（JSON）の例:

    {
      "columns": {"employee_no": "社員番号", "name": "氏名", "kana": "氏名カナ", "dept": "所属", "dept_code": "所属コード",
                  "status": "在籍区分"},
      "status_values": {"在職": "在籍", "休業": "休職", "退社": "退職"},
      "sheet": "社員一覧"
    }

"status" を対応させない場合は、全員を在籍として扱う。前の書き方の "active_values"（在籍とみなす値の並び）も読める
（その値は在籍、ただし「休職」は休職。ほかの値は退職）。
"""

from __future__ import annotations

import csv
import functools
import json
import re
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

from .llm import ChatModel

FIELDS = ("employee_no", "name", "kana", "dept", "dept_code", "status", "email")
STATUSES = ("在籍", "休職", "退職")
ACTIVE_STATUSES = ("在籍", "休職")        # 照合で一致する（active = 1）
REQUIRED = ("employee_no", "name", "dept")
LABELS = {
    "employee_no": "従業員番号",
    "name": "氏名",
    "kana": "よみ",
    "dept": "部署",
    "dept_code": "部署番号",
    "status": "在籍の状態",
    "email": "メールアドレス",
}

# 列名から対応表の下書きを作るときの手がかり（決まったルール。AIはこれで決まらないときだけ使う）
HINTS = {
    "employee_no": ["社員番号", "従業員番号", "社員no", "社員コード", "従業員コード", "職員番号", "職員コード", "職員id", "employeeid", "employee_no", "empno", "社員id"],
    "name": ["氏名", "名前", "社員名", "従業員名", "fullname", "name", "漢字氏名"],
    "kana": ["氏名カナ", "よみ", "読み", "フリガナ", "ふりがな", "カナ氏名", "氏名かな", "kana", "furigana"],
    "dept": ["所属", "部署", "所属部署", "部署名", "所属名", "department", "dept", "組織名"],
    "dept_code": ["所属コード", "部署コード", "部署番号", "所属番号", "組織コード", "所属cd", "部署cd", "deptcode", "departmentcode"],
    "status": ["在籍区分", "在籍状況", "在職区分", "在籍状態", "在籍", "雇用状態", "ステータス", "status", "退職区分"],
    "email": ["メールアドレス", "メール", "社用メール", "email", "mail", "e-mail", "mailaddress"],
}

HR_SCHEMA = """
CREATE TABLE IF NOT EXISTS employees (
    employee_no TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    dept        TEXT NOT NULL,
    dept_code   TEXT NOT NULL DEFAULT '',
    active      INTEGER NOT NULL,                 -- 1：在籍・休職（照合で一致する）、0：退職
    email       TEXT NOT NULL DEFAULT '',
    kana        TEXT NOT NULL DEFAULT '',
    status      TEXT NOT NULL DEFAULT '在籍',     -- 在籍・休職・退職
    changed_at  TEXT NOT NULL DEFAULT ''          -- 最後に変わった日（YYYY-MM-DD。取り込みで入った・変わった日）
);
CREATE TABLE IF NOT EXISTS hr_departments (
    dept      TEXT PRIMARY KEY,
    dept_code TEXT NOT NULL DEFAULT ''
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
    result: object = None          # yanua/hrimport.py の Result（監査ログに使う）

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
    return {"columns": cols, "status_values": {}}


# 在籍状態の値の下書き（画面・コマンドで、ファイルに出てくる値を在籍・休職・退職に当てるときの手がかり）
STATUS_HINTS = {
    "在籍": ["在籍", "在職", "現職", "勤務", "就業", "active", "employed"],
    "休職": ["休職", "休業", "休暇", "産休", "育休", "leave", "onleave"],
    "退職": ["退職", "退社", "離職", "解雇", "retired", "resigned", "inactive", "terminated"],
}


def guess_status(value: str) -> str:
    """ファイルの値から、在籍・休職・退職の下書き。決まらなければ空（人が選ぶ）。"""
    k = _key(value)
    for status, words in STATUS_HINTS.items():
        if any(_key(w) == k or (len(_key(w)) >= 2 and _key(w) in k) for w in words):
            return status
    return ""


# ---------------------------------------------------------------- 取り込みと照合

# 照合用の列（正規化した氏名・姓・部署）と索引。在籍が100万人でも、名前や部署から索引で引けるようにする
KEY_COLUMNS = ("name_key", "surname_key", "dept_key")
INDEXES = """
CREATE INDEX IF NOT EXISTS idx_employees_name ON employees(name_key, active);
CREATE INDEX IF NOT EXISTS idx_employees_surname ON employees(surname_key, active);
CREATE INDEX IF NOT EXISTS idx_employees_dept ON employees(dept_key, active);
"""
# 部署名の末尾。「営業部」は「営業」とも呼ばれるので、末尾を除いた形でも探せるようにする
DEPT_SUFFIXES = ("事業本部", "事業部", "本部", "部", "課", "室", "グループ", "チーム", "センター", "支店", "支社", "店")


def name_key(text: str) -> str:
    """氏名の照合用の形：全角・半角をそろえ、空白を除き、英字は小文字にする。"""
    return norm(text).lower()


def surname_of(name: str) -> str:
    """氏名の姓（空白で区切られているときだけ）。1文字の姓は手がかりにならないので空にする。"""
    parts = re.split(r"\s+", unicodedata.normalize("NFKC", str(name or "")).strip())
    return parts[0] if len(parts) > 1 and len(parts[0]) >= 2 else ""


def _keys(name: str, dept: str) -> tuple[str, str, str]:
    return name_key(name), name_key(surname_of(name)), name_key(dept)


def ensure_schema(db) -> None:
    db.executescript(HR_SCHEMA)
    cols = {r[1] for r in db.execute("PRAGMA table_info(employees)")}
    # 古い保存先には列を足す
    if "email" not in cols:
        db.execute("ALTER TABLE employees ADD COLUMN email TEXT NOT NULL DEFAULT ''")
    if "kana" not in cols:
        db.execute("ALTER TABLE employees ADD COLUMN kana TEXT NOT NULL DEFAULT ''")
    if "changed_at" not in cols:
        db.execute("ALTER TABLE employees ADD COLUMN changed_at TEXT NOT NULL DEFAULT ''")
    if "status" not in cols:
        # 前の保存先は在籍（1）か、そうでないか（0）だけ。休職は次の取り込みで分かる
        db.execute("ALTER TABLE employees ADD COLUMN status TEXT NOT NULL DEFAULT '在籍'")
        with db:
            db.execute("UPDATE employees SET status = '退職' WHERE active = 0")
    added = [c for c in KEY_COLUMNS if c not in cols]
    for c in added:
        db.execute(f"ALTER TABLE employees ADD COLUMN {c} TEXT NOT NULL DEFAULT ''")
    db.executescript(INDEXES)
    if "dept_code" not in {r[1] for r in db.execute("PRAGMA table_info(hr_departments)")}:
        db.execute("ALTER TABLE hr_departments ADD COLUMN dept_code TEXT NOT NULL DEFAULT ''")
        db.execute("DELETE FROM hr_departments")
    if (not db.execute("SELECT 1 FROM hr_departments LIMIT 1").fetchone()
            and db.execute("SELECT 1 FROM employees LIMIT 1").fetchone()):
        refresh_departments(db)
    if added:
        rows = db.execute("SELECT employee_no, name, dept FROM employees").fetchall()
        with db:
            db.executemany("UPDATE employees SET name_key = ?, surname_key = ?, dept_key = ? WHERE employee_no = ?",
                           [(*_keys(n, d), no) for no, n, d in rows])


def refresh_departments(db) -> None:
    """部署の一覧（在籍者のいる部署）を作り直す。照合のたびに在籍者全員を読まないために、取り込みのときに作っておく。"""
    with db:
        db.execute("DELETE FROM hr_departments")
        db.execute("INSERT OR IGNORE INTO hr_departments SELECT dept, MAX(dept_code) FROM employees "
                   "WHERE active = 1 GROUP BY dept")


def import_employees(db, path: Path, mapping: dict, by: str = "（コマンド）", leave_policy: str = "そのまま") -> HRImportReport:
    """人事データを取り込む（コマンド・テスト用）。画面と同じ処理（yanua/hrimport.py の commit）を通す。
    対応表の列以外は読まない。毎回まるごと入れ替える。誤りが1件でもあれば入れ替えない。"""
    from . import hrimport

    report = HRImportReport()
    try:
        result = hrimport.commit(db, path, mapping, by, leave_policy)
    except hrimport.MappingError as e:
        report.errors = [f"{path.name}: 対応表 {w} {m}".replace("対応表  ", "対応表 ") for w, m in e.problems]
        return report
    p = result.preview
    report.dropped_columns = p.ignored_columns
    report.errors = [f"{path.name} {e['line']}行目: {e['message']}" for e in p.errors]
    if p.error_count > len(p.errors):
        report.errors.append(f"ほかに {p.error_count - len(p.errors)}件の誤りがあります")
    if not result.committed and not report.errors:
        report.errors.append(result.reason)
    if result.committed:
        report.active = p.counts.get("在籍", 0) + p.counts.get("休職", 0)
        report.inactive = p.counts.get("退職", 0)
    report.result = result
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
    """SSOの個人アカウントから本人の候補を出すとき用（在籍中の人だけ。休職中の人も入る。status で見分ける）。"""
    ensure_schema(db)
    row = db.execute(
        "SELECT employee_no, name, dept, dept_code, status FROM employees WHERE employee_no = ? AND active = 1",
        (norm(employee_no),),
    ).fetchone()
    return {"employee_no": row[0], "name": row[1], "dept": row[2], "dept_code": row[3], "status": row[4]} if row else None


def status_of(db, employee_no: str) -> str | None:
    """在籍の状態（在籍・休職・退職）。人事データにいなければ None。"""
    try:
        row = db.execute("SELECT status FROM employees WHERE employee_no = ?", (employee_no,)).fetchone()
    except Exception:  # noqa: BLE001  人事データをまだ取り込んでいないとき
        return None
    return row[0] if row else None


def lookup_by_email(db, email: str) -> dict | None:
    """カレンダーの予定の主催者（メールアドレス）から、在籍中の社員を探す。"""
    ensure_schema(db)
    e = (email or "").strip().lower()
    if not e:
        return None
    row = db.execute(
        "SELECT employee_no, name, dept, dept_code FROM employees WHERE email = ? AND active = 1", (e,)
    ).fetchone()
    return {"employee_no": row[0], "name": row[1], "dept": row[2], "dept_code": row[3]} if row else None


def last_import(db) -> dict | None:
    ensure_schema(db)
    row = db.execute("SELECT imported_at, source, active, inactive FROM hr_imports ORDER BY imported_at DESC LIMIT 1").fetchone()
    if not row:
        return None
    age = (datetime.now(timezone.utc) - datetime.fromisoformat(row[0])).days
    return {"imported_at": row[0], "source": row[1], "active": row[2], "inactive": row[3], "age_days": age}


def _substrings(text: str, max_len: int = 16) -> list[str]:
    """文に含まれる2文字以上の部分文字列（照合用の形）。名前や部署の索引をこれで引く。"""
    t = name_key(text)[:300]
    return list({t[i:j] for i in range(len(t)) for j in range(i + 2, min(len(t), i + max_len) + 1)})


def _by_keys(db, column: str, keys: list[str], dept: str = "", limit: int = 0) -> list[dict]:
    out: dict[str, dict] = {}
    for i in range(0, len(keys), 500):   # SQLite に一度に渡せる値の数に収める
        part = keys[i:i + 500]
        sql = (f"SELECT employee_no, name, dept, dept_code FROM employees "
               f"WHERE {column} IN ({','.join('?' * len(part))}) AND active = 1")
        args = list(part)
        if dept:
            sql += " AND dept_key = ?"
            args.append(name_key(dept))
        if limit:
            sql += f" LIMIT {int(limit) - len(out)}"
        for r in db.execute(sql, args):
            out[r[0]] = {"employee_no": r[0], "name": r[1], "dept": r[2], "dept_code": r[3]}
        if limit and len(out) >= limit:
            break
    return list(out.values())


SURNAME_LIMIT = 50   # 姓だけで探すときに返す人数の上限（これより多ければ、どのみち姓だけでは決まらない）


def names_in_text(db, text: str, dept: str = "") -> tuple[list[dict], list[dict]]:
    """文の中にフルネームが出てくる在籍中の社員と、姓だけが出てくる社員を、索引で探す。

    フルネームが一致した人がいれば、姓では探さない。姓で探すときは、部署が分かっていれば部署で絞り、
    SURNAME_LIMIT 人までにする（「佐藤」で何万人も読み込まないため）。
    """
    ensure_schema(db)
    keys = _substrings(text)
    full = _by_keys(db, "name_key", keys)
    if full:
        return full, []
    return [], _by_keys(db, "surname_key", keys, dept=dept, limit=SURNAME_LIMIT)


def departments(db) -> dict[str, str | None]:
    """部署の呼び方（照合用の形）→ 部署名。「営業部」なら「営業部」「営業」。
    2つの部署が同じ呼び方になるとき（営業部と営業課など）は、どちらとも決めない（None）。"""
    ensure_schema(db)
    return _dept_aliases(tuple(sorted(d for (d,) in db.execute("SELECT dept FROM hr_departments"))))


@functools.lru_cache(maxsize=8)
def _dept_aliases(depts: tuple[str, ...]) -> dict[str, str | None]:
    out: dict[str, str | None] = {}
    for dept in depts:
        names = {name_key(dept)}
        for suf in DEPT_SUFFIXES:
            if dept.endswith(suf) and len(dept) - len(suf) >= 2:
                names.add(name_key(dept[: -len(suf)]))
                break
        for n in names:
            out[n] = dept if out.get(n, dept) == dept else None
    return out


def dept_by_code(db, code: str) -> str:
    """部署コードから部署名。なければ空。"""
    ensure_schema(db)
    c = norm(code)
    if not c:
        return ""
    row = db.execute("SELECT dept FROM hr_departments WHERE dept_code = ?", (c,)).fetchone()
    return row[0] if row else ""


def dept_in_text(db, text: str) -> tuple[str, str]:
    """文に出てくる部署（部署名, 文の中の言い方）。いちばん長く一致した呼び方を使う。なければ空。"""
    t = name_key(text)
    best = ("", "")
    for alias, dept in departments(db).items():
        if alias in t and len(alias) > len(best[1]):
            best = (dept or "", alias)
    return best


def find_in_text(db, text: str) -> list[dict]:
    """文の中に名前が出てくる在籍中の社員を探す（受付で、訪ね先の社員を見つけるとき用）。

    フルネームが含まれていればその人だけ。なければ姓で探し、同じ姓が複数いればすべて返す（呼び出し側で聞き直す）。
    結果は社内の取次に使うだけで、社外の人に見せない。在籍者を全部読まず、索引で引く。
    """
    full, surname = names_in_text(db, text)
    return full or surname
