"""PDF の仕分け（S-20）と変換（S-21）を、画面から使うための処理。総務だけが使う。

仕分けの流れ：
    受信箱の一覧 → 予定を作る（ジョブ。PDFを読み、OCRもする） → 予定を見てルールを選び直す → 実行 → 記録
- 予定には、PDFから取り出した文字も残す。ルールを選び直すときは、PDFを読み直さずに（OCRし直さずに）作り直す
- 実行するときは、予定を作ったときとファイルが変わっていないか（大きさ・更新日時）を確かめる。変わっていたら置かない
- 予定は、作った人だけが見て・直して・実行できる。実行は1回だけ
"""

from __future__ import annotations

import csv
import logging
import json
import sqlite3
from datetime import datetime
from pathlib import Path

from ..paper import convert, rules, sort
from ..paper.extract import read_pdf
from ..settingfiles import SettingFile

log = logging.getLogger("yanua.paper")

SCHEMA = """
CREATE TABLE IF NOT EXISTS paper_plans (
    plan_id     TEXT PRIMARY KEY,       -- 予定を作ったジョブの番号
    owner       TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    items       TEXT NOT NULL,          -- 画面に返す予定（JSON）
    texts       TEXT NOT NULL,          -- 取り出した文字（JSON。画面には返さない）
    executed_at TEXT,
    result      TEXT
);
"""


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def ensure_schema(db: sqlite3.Connection) -> None:
    db.executescript(SCHEMA)


def rule_store(settings, audit=None) -> SettingFile:
    """仕分けルールの設定のファイル。既定は settings.paper_rules、画面から保存した写しは settings_dir/rules.json。
    audit を渡すと、保存・版に戻す・既定に戻すを監査ログに残す（paper.rules.save / .restore / .reset）。"""
    base = Path(settings.paper_rules).parent    # 様式のファイルは、既定のルールのファイルのフォルダから探す
    return SettingFile("rules.json", settings.paper_rules, settings.settings_dir, lambda text: rules.rule_problems(text, base),
                       audit=audit, audit_action="paper.rules")


def check_rules(settings) -> tuple[Path, list[tuple[str, str]]]:
    """起動のときに、今使う仕分けルール（写しか既定）を読み込みと同じ検証にかける。(ファイル, [(位置, 理由)])。"""
    store = rule_store(settings)
    path = store.path()
    try:
        text = store.read_text()
    except OSError as e:
        return path, [("", f"ファイルを読めません: {e}")]
    return path, rules.rule_problems(text, Path(settings.paper_rules).parent)


def inbox_files(inbox: Path) -> list[Path]:
    if not inbox.is_dir():
        return []
    return sorted(p for p in inbox.iterdir() if p.is_file() and p.suffix.lower() == ".pdf" and not p.name.startswith("."))


def inbox_list(inbox: Path) -> list[dict]:
    out = []
    for p in inbox_files(inbox):
        st = p.stat()
        out.append({"name": p.name, "size": st.st_size,
                    "modified": datetime.fromtimestamp(st.st_mtime).astimezone().isoformat(timespec="seconds")})
    return out


def inbox_path(inbox: Path, name: str) -> Path | None:
    """受信箱のファイル名から、受信箱の中のファイルだけを返す（フォルダの外を指す名前は受け付けない）。"""
    if not name or "/" in name or "\\" in name or name.startswith("."):
        return None
    p = (inbox / name)
    return p if p in inbox_files(inbox) else None


# 読めなかった PDF の、画面に出す文（コマンドやライブラリーの例外の文は出さない。ログに残す）
SHOWN_ERRORS = {
    "ocr": "スキャンした PDF を読む仕組み（OCR）が準備されていません。管理者に連絡してください",
    "read": "PDF を読めませんでした（壊れているか、パスワードがかかっている可能性があります）",
}
OCR_MISSING = SHOWN_ERRORS["ocr"]


def _item(index: int, pl: sort.Plan, stat) -> dict:
    # 画面に出す項目（日付・相手先・金額・宛先と、ルールに書いた独自の項目）。年月日の分けたものなどは出さない
    hidden = ("rule", "original", "yyyy", "mm", "dd")
    return {"index": index, "file": pl.source.name, "rule": pl.rule or "", "by": pl.by,
            "fields": {k: v for k, v in pl.fields.items() if k not in hidden},
            "dest": str(pl.dest) if pl.dest else "", "ocr": pl.ocr, "missing": [] if pl.error else pl.missing,
            "error": (SHOWN_ERRORS.get(pl.error_kind, SHOWN_ERRORS["read"]) if pl.error else None),
            "size": stat.st_size if stat else None, "mtime": stat.st_mtime if stat else None}


def make_plan(db: sqlite3.Connection, *, plan_id: str, owner: str, inbox: Path, names: list[str], rs: rules.RuleSet,
              llm=None, progress=lambda pct, stage: None) -> dict:
    pdfs = [p for n in names if (p := inbox_path(inbox, n))] if names else inbox_files(inbox)
    items, texts = [], []
    for i, pdf in enumerate(pdfs):
        progress(5 + int(90 * i / max(1, len(pdfs))), f"{pdf.name} を読んでいます（{i + 1}/{len(pdfs)}）")
        stat = pdf.stat()
        pl = sort.plan([pdf], rs, llm)[0]
        if pl.error:
            log.warning("仕分けの予定：%s を読めませんでした：%s", pdf.name, pl.error)
        items.append(_item(i, pl, stat))
        texts.append(pl.text)
    ensure_schema(db)
    with db:
        db.execute("INSERT INTO paper_plans (plan_id, owner, created_at, items, texts) VALUES (?, ?, ?, ?, ?)",
                   (plan_id, owner, _now(), json.dumps(items, ensure_ascii=False), json.dumps(texts, ensure_ascii=False)))
    return {"plan_id": plan_id, "count": len(items), "errors": sum(1 for x in items if x["error"])}


def get_plan(db: sqlite3.Connection, plan_id: str, owner: str) -> dict | None:
    ensure_schema(db)
    r = db.execute("SELECT plan_id, owner, created_at, items, executed_at, result FROM paper_plans WHERE plan_id = ?",
                   (plan_id,)).fetchone()
    if not r or r[1] != owner:
        return None
    items = [{k: v for k, v in x.items() if k not in ("size", "mtime")} for x in json.loads(r[3])]
    return {"plan_id": r[0], "created_at": r[2], "items": items, "executed_at": r[4],
            "result": json.loads(r[5]) if r[5] else None}


def plan_text(db: sqlite3.Connection, plan_id: str, index: int) -> str:
    """予定を作ったときに取り出した文字（ルールの下書きに使う。PDF を読み直さない）。"""
    return json.loads(db.execute("SELECT texts FROM paper_plans WHERE plan_id = ?", (plan_id,)).fetchone()[0])[index]


def rechoose(db: sqlite3.Connection, plan_id: str, index: int, rule: str | None, inbox: Path, rs: rules.RuleSet) -> dict:
    """1件のルールを選び直す（rule が空なら、ルールで自動に決め直す）。PDF は読み直さない。"""
    items, texts = map(json.loads, db.execute("SELECT items, texts FROM paper_plans WHERE plan_id = ?", (plan_id,)).fetchone())
    old = items[index]
    pl = sort.plan_from_text(inbox / old["file"], texts[index], old["ocr"], rs, forced=rule or None)
    new = {**_item(index, pl, None), "size": old["size"], "mtime": old["mtime"], "error": old["error"]}
    items[index] = new
    with db:
        db.execute("UPDATE paper_plans SET items = ? WHERE plan_id = ?", (json.dumps(items, ensure_ascii=False), plan_id))
    return {k: v for k, v in new.items() if k not in ("size", "mtime")}


def execute(db: sqlite3.Connection, plan_id: str, inbox: Path, out_dir: Path, move: bool = False) -> dict:
    items = json.loads(db.execute("SELECT items FROM paper_plans WHERE plan_id = ?", (plan_id,)).fetchone()[0])
    plans, skipped = [], []
    for x in items:
        src = inbox_path(inbox, x["file"])
        if x["error"] or not x["dest"]:
            skipped.append({"file": x["file"], "reason": x["error"] or "置く先が決まっていません"})
            continue
        if not src or src.stat().st_size != x["size"] or src.stat().st_mtime != x["mtime"]:
            # 予定を作ったあとに消えた・差し替えられたファイルは置かない（見ていない内容を仕分けないため）
            skipped.append({"file": x["file"], "reason": "予定を作ったあとに、受信箱のファイルがなくなったか変わりました"})
            continue
        plans.append(sort.Plan(src, x["rule"], x["by"], x["fields"], Path(x["dest"]), x["ocr"]))
    # 1件ずつ置いて、予定の置き先と実際の置き先を比べる（同じ名前があれば上書きせず _2, _3 … を付けるので）
    out_root = out_dir.resolve()
    placed, renamed = [], []
    for pl in plans:
        done = sort.execute([pl], out_dir, move=move)
        if not done:
            skipped.append({"file": pl.source.name, "reason": "保存先の外を指していたので置きませんでした"})
            continue
        rel = str(done[0].relative_to(out_root))
        placed.append(rel)
        if rel != str(pl.dest):
            renamed.append({"file": pl.source.name, "planned": str(pl.dest), "placed": rel})
    result = {"placed": placed, "renamed": renamed, "skipped": skipped, "moved": move}
    with db:
        db.execute("UPDATE paper_plans SET executed_at = ?, result = ? WHERE plan_id = ?",
                   (_now(), json.dumps(result, ensure_ascii=False), plan_id))
    return result


def history(out_dir: Path, limit: int = 200) -> list[dict]:
    """仕分けの記録（保存先の _仕分け記録.csv）を、新しい順に返す。"""
    log = out_dir / sort.LOG_NAME
    if not log.is_file():
        return []
    with log.open(encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    keys = ("time", "source", "dest", "rule", "by", "ocr", "date", "company", "amount")
    return [dict(zip(keys, r)) for r in reversed(rows[1:])][:limit]


def convert_pdf(src: Path, dest_dir: Path, to: str, rs: rules.RuleSet | None, rule: str | None,
                progress=lambda pct, stage: None) -> tuple[Path, str]:
    """PDF を Word / Excel にする。rs を渡すと、当てはまるルールに様式があれば様式に差し込む。"""
    dest_dir.mkdir(parents=True, exist_ok=True)
    out = dest_dir / f"{src.stem}.{to}"
    progress(20, "PDF を読んでいます")
    doc = read_pdf(src, tables=True)
    progress(60, "変換しています")
    if rs is not None:
        from ..paper import form

        m = rules.choose(rs, doc.text, forced=rule or None)
        template = m.rule.templates.get(to) if m.rule else None
        if template:
            done = form.fill(template, out, doc, rules.find_fields(doc.text, m.rule, rs.self_names))
            return done, f"「{m.rule.name}」の様式（{template.name}）に差し込みました。読み取った内容は、元の書類と照らして確認してください。"
        note = ("どのルールにも当たらないため" if not m.rule else f"「{m.rule.name}」に {to} の様式がないため") + "、様式なしで変換しました。"
    else:
        note = "変換しました。値は文字のまま入れています（先頭の0なども残ります）。"
    (convert.to_docx if to == "docx" else convert.to_xlsx)(src, out, doc)
    return out, note
