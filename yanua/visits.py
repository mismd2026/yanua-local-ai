"""来訪の予定とインビテーションコード、担当者へのお知らせ。

担当者がアポを登録すると、コードを1つ作る。担当者はコードを訪問者に送り、訪問者は受付でコードを入力する。
受付は「挨拶とコードの入力のお願い」だけで済み、入力されたら担当者にお知らせを残す。

コードの扱い:
- 紛らわしい文字（0とO、1とIなど）を除いた8文字。推測されにくいよう、secrets で作る
- 保存するのはハッシュだけ。コードそのものは登録したときに1回だけ表示する
- 使えるのは予定の当日だけ。違う日のコード・間違ったコード・取り消したコードは、どれも同じ「確認できません」にする
  （どのコードが存在するかを、受付の画面から探れないようにするため）

お知らせ（notifications）は、今は保存先に残すだけ。メールやチャットへの送信は、会社ごとにここへつなぐ。
"""

from __future__ import annotations

import csv
import hashlib
import html
import re
import secrets
import unicodedata
from datetime import date, datetime
from pathlib import Path

from . import hr

ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"   # 0 O 1 I を除く
CODE_LEN = 8

SCHEMA = """
CREATE TABLE IF NOT EXISTS visits (
    visit_id         INTEGER PRIMARY KEY AUTOINCREMENT,
    code_hash        TEXT UNIQUE,
    date             TEXT NOT NULL,
    time             TEXT NOT NULL,
    room             TEXT NOT NULL,
    host_employee_no TEXT NOT NULL,
    host_contact     TEXT NOT NULL DEFAULT '',
    visitor_company  TEXT NOT NULL,
    visitor_name     TEXT NOT NULL,
    purpose          TEXT NOT NULL DEFAULT '',
    created_at       TEXT NOT NULL,
    checked_in_at    TEXT,
    cancelled        INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_visits_date ON visits(date);
CREATE TABLE IF NOT EXISTS notifications (
    notification_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at       TEXT NOT NULL,
    employee_no      TEXT NOT NULL,
    dept             TEXT NOT NULL,
    kind             TEXT NOT NULL,
    message          TEXT NOT NULL,
    ref              TEXT NOT NULL DEFAULT '',
    read             INTEGER NOT NULL DEFAULT 0
);
"""

COLUMNS = ("visit_id", "date", "time", "room", "host_employee_no", "host_contact",
           "visitor_company", "visitor_name", "purpose", "checked_in_at")


class VisitError(ValueError):
    pass


def ensure_schema(db) -> None:
    db.executescript(SCHEMA)
    hr.ensure_schema(db)


def normalize_code(text: str) -> str:
    """入力のゆれ（全角・小文字・ハイフン・空白）をそろえる。"""
    return re.sub(r"[\s\-ー－_]", "", unicodedata.normalize("NFKC", text or "")).upper()


def looks_like_code(text: str) -> bool:
    c = normalize_code(text)
    return len(c) == CODE_LEN and all(ch in ALPHABET for ch in c)


def format_code(code: str) -> str:
    c = normalize_code(code)
    return f"{c[:4]}-{c[4:]}"


def _hash(code: str) -> str:
    return hashlib.sha256(normalize_code(code).encode()).hexdigest()


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def create(db, *, host_employee_no: str, visit_date: str, time: str, room: str, visitor_company: str,
           visitor_name: str, purpose: str = "", host_contact: str = "", code: str | None = None) -> tuple[dict, str]:
    """来訪の予定を登録し、(予定, コード) を返す。コードは保存しないので、ここでしか取り出せない。"""
    ensure_schema(db)
    host = hr.lookup(db, host_employee_no)
    if not host:
        raise VisitError(f"社員番号 {host_employee_no} は人事データ（在籍中）にありません")
    try:
        date.fromisoformat(visit_date)
    except ValueError as e:
        raise VisitError(f"日付は YYYY-MM-DD で指定してください: {visit_date}") from e
    for label, value in (("時刻", time), ("会議室", room), ("会社名", visitor_company), ("お名前", visitor_name)):
        if not str(value).strip():
            raise VisitError(f"{label}が空です")
    if code is not None and not looks_like_code(code):
        raise VisitError(f"コードの形式が違います: {code}")
    code = normalize_code(code) if code else "".join(secrets.choice(ALPHABET) for _ in range(CODE_LEN))
    with db:
        cur = db.execute(
            "INSERT INTO visits (code_hash, date, time, room, host_employee_no, host_contact, visitor_company, "
            "visitor_name, purpose, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (_hash(code), visit_date, time.strip(), room.strip(), host["employee_no"], host_contact.strip(),
             visitor_company.strip(), visitor_name.strip(), purpose.strip(), _now()),
        )
    return get(db, cur.lastrowid), code


def import_csv(db, path: Path) -> list[tuple[dict, str]]:
    """来訪予定のCSVをまとめて登録する。code の列があればそのコードを使う（デモ用）。なければ作る。"""
    ensure_schema(db)
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    out = []
    for r in rows:
        code = r.get("code") or None
        if code and db.execute("SELECT 1 FROM visits WHERE code_hash = ?", (_hash(code),)).fetchone():
            continue   # 同じコードの予定は登録済み（デモを何度取り込んでも重ならないように）
        out.append(create(db, host_employee_no=r["host_employee_no"], visit_date=r["date"], time=r["time"],
                          room=r["room"], visitor_company=r["company"], visitor_name=r["visitor"],
                          purpose=r.get("purpose", ""), host_contact=r.get("host_contact", ""),
                          code=code))
    return out


CALENDAR_COLUMNS = ("event_id", "date", "time", "room", "host_email", "visitor_company", "visitor_name", "code_hash", "status")


def is_calendar_export(path: Path) -> bool:
    with path.open(encoding="utf-8-sig") as f:
        header = next(csv.reader(f), [])
    return "code_hash" in header


def import_calendar_csv(db, path: Path) -> dict:
    """Googleカレンダーのアドオンが書き出した予定（シートをCSVで保存したもの）を取り込む。

    コードそのものは入っていない（ハッシュだけ）。同じハッシュの予定はあれば更新するので、
    予定の日時や会議室が変わった（リスケした）ときも、書き出し直して取り込めばそろう。
    status が「取消」の予定は、受付でコードを使えなくする。
    """
    ensure_schema(db)
    with path.open(encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    report = {"added": 0, "updated": 0, "cancelled": 0, "errors": []}
    if rows:
        absent = [c for c in CALENDAR_COLUMNS if c not in rows[0]]
        if absent:
            report["errors"].append(f"{path.name}: 列がありません: {'、'.join(absent)}")
            return report
    for i, r in enumerate(rows, start=2):
        code_hash = (r["code_hash"] or "").strip().lower()
        if not re.fullmatch(r"[0-9a-f]{64}", code_hash):
            report["errors"].append(f"{path.name} {i}行目: code_hash の形式が違います")
            continue
        try:
            date.fromisoformat(r["date"])
        except ValueError:
            report["errors"].append(f"{path.name} {i}行目: 日付は YYYY-MM-DD にしてください")
            continue
        host = hr.lookup_by_email(db, r["host_email"])
        if not host:
            report["errors"].append(f"{path.name} {i}行目: 担当者 {r['host_email']} が人事データ（在籍中）にありません")
            continue
        cancelled = int(r["status"].strip() == "取消")
        values = (r["date"], r["time"].strip(), r["room"].strip() or "受付にお問い合わせください", host["employee_no"],
                  (r.get("host_contact") or "").strip(), r["visitor_company"].strip(), r["visitor_name"].strip(), cancelled)
        with db:
            cur = db.execute(
                "UPDATE visits SET date = ?, time = ?, room = ?, host_employee_no = ?, host_contact = ?, "
                "visitor_company = ?, visitor_name = ?, cancelled = ? WHERE code_hash = ?", (*values, code_hash))
            if cur.rowcount:
                report["updated"] += 1
            else:
                db.execute(
                    "INSERT INTO visits (date, time, room, host_employee_no, host_contact, visitor_company, visitor_name, "
                    "cancelled, code_hash, purpose, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (*values, code_hash, f"カレンダーの予定 {r['event_id']}", _now()))
                report["added"] += 1
        report["cancelled"] += cancelled
    return report


def _row(r) -> dict:
    return dict(zip(COLUMNS, r))


def get(db, visit_id: int) -> dict:
    r = db.execute(f"SELECT {', '.join(COLUMNS)} FROM visits WHERE visit_id = ?", (visit_id,)).fetchone()
    return _row(r)


def find_by_code(db, code: str, today: date) -> dict | None:
    """当日の、取り消していない予定だけを返す。それ以外は None（理由は区別しない）。"""
    ensure_schema(db)
    if not looks_like_code(code):
        return None
    r = db.execute(
        f"SELECT {', '.join(COLUMNS)} FROM visits WHERE code_hash = ? AND date = ? AND cancelled = 0",
        (_hash(code), today.isoformat()),
    ).fetchone()
    return _row(r) if r else None


def for_date(db, day: date) -> list[dict]:
    ensure_schema(db)
    rows = db.execute(
        f"SELECT {', '.join(COLUMNS)} FROM visits WHERE date = ? AND cancelled = 0 ORDER BY time", (day.isoformat(),)
    )
    return [_row(r) for r in rows]


def check_in(db, visit: dict) -> dict:
    """受付が済んだことを記録し、担当者にお知らせを残す。2回目以降はお知らせを重ねない。"""
    host = hr.lookup(db, visit["host_employee_no"])
    first = visit["checked_in_at"] is None
    with db:
        if first:
            db.execute("UPDATE visits SET checked_in_at = ? WHERE visit_id = ?", (_now(), visit["visit_id"]))
            notify(db, visit["host_employee_no"], host["dept"] if host else "総務部", "来訪",
                   f"{visit['visitor_company']} {visit['visitor_name']}様が受付に到着しました。"
                   f"（{visit['time']} {visit['room']}）", ref=f"visit:{visit['visit_id']}", commit=False)
    return {**get(db, visit["visit_id"]), "first_check_in": first}


def notify(db, employee_no: str, dept: str, kind: str, message: str, ref: str = "", commit: bool = True) -> None:
    """担当者（社員）や部署へのお知らせを残す。employee_no が空なら部署あて。"""
    ensure_schema(db)
    sql = ("INSERT INTO notifications (created_at, employee_no, dept, kind, message, ref) VALUES (?, ?, ?, ?, ?, ?)")
    args = (_now(), employee_no or "", dept, kind, message, ref)
    if commit:
        with db:
            db.execute(sql, args)
    else:
        db.execute(sql, args)


def notifications(db, employee_no: str = "", dept: str = "", unread_only: bool = True) -> list[dict]:
    ensure_schema(db)
    where, args = ["1 = 1"], []
    if employee_no or dept:
        where.append("(employee_no = ? OR (employee_no = '' AND dept = ?))")
        args += [employee_no, dept]
    if unread_only:
        where.append("read = 0")
    rows = db.execute(
        "SELECT notification_id, created_at, employee_no, dept, kind, message, ref FROM notifications "
        f"WHERE {' AND '.join(where)} ORDER BY notification_id", args)
    keys = ("notification_id", "created_at", "employee_no", "dept", "kind", "message", "ref")
    return [dict(zip(keys, r)) for r in rows]


def mark_read(db, ids: list[int]) -> None:
    with db:
        db.executemany("UPDATE notifications SET read = 1 WHERE notification_id = ?", [(i,) for i in ids])


def invitation_text(visit: dict, code: str, host_name: str) -> str:
    """担当者が訪問者に送る案内文。"""
    return (
        f"{visit['visitor_company']} {visit['visitor_name']}様\n\n"
        f"ご来訪の予定を承りました。\n"
        f"日時：{visit['date']} {visit['time']}\n"
        f"場所：{visit['room']}\n"
        f"担当：{host_name}\n"
        + (f"連絡先：{visit['host_contact']}\n" if visit["host_contact"] else "")
        + "\n"
        f"当日は受付で、次のインビテーションコードをご入力ください。\n"
        f"インビテーションコード：{format_code(code)}\n"
        f"（ご来訪の当日だけ使えます）"
    )


def pass_html(visit: dict, host_name: str) -> str:
    """印刷用の入館票。コードは載せない（落としても、ほかの人が使えないようにするため）。"""
    e = html.escape
    rows = [("日時", f"{visit['date']} {visit['time']}"), ("会議室", visit["room"]), ("担当", host_name),
            ("連絡先", visit["host_contact"] or "受付にお声がけください"),
            ("お名前", f"{visit['visitor_company']} {visit['visitor_name']}様")]
    body = "".join(f"<tr><th>{e(k)}</th><td>{e(v)}</td></tr>" for k, v in rows)
    return (
        "<!DOCTYPE html><html lang='ja'><head><meta charset='utf-8'><title>入館票</title>"
        "<style>body{font-family:sans-serif;margin:24px}table{border-collapse:collapse;font-size:18px}"
        "th,td{border:1px solid #333;padding:8px 14px;text-align:left}th{background:#eee}"
        "h1{font-size:22px}@media print{button{display:none}}</style></head><body>"
        f"<h1>入館票（ゲスト）</h1><table>{body}</table>"
        "<p>お帰りの際は、受付に入館票をお返しください。</p>"
        "<button onclick='window.print()'>印刷する</button></body></html>"
    )
