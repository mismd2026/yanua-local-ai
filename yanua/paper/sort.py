"""PDFの仕分け：ルールに沿ってファイル名を付け、保存先のフォルダへ置く。

元のファイルは、--move を付けない限り残す（コピーする）。--dry-run で、置く前に予定だけを見られる。
仕分けた結果は、保存先の「_仕分け記録.csv」に追記する（あとから、どのファイルをどこへ置いたかを追える）。
"""

from __future__ import annotations

import csv
import shutil
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from ..llm import ChatModel
from .extract import OCRUnavailable, read_pdf
from .rules import RuleSet, choose, find_fields, render

LOG_NAME = "_仕分け記録.csv"


@dataclass
class Plan:
    source: Path
    rule: str           # 当てはまったルール名（なければ空）
    by: str             # rule / llm / forced / none
    fields: dict
    dest: Path | None   # 置く先（out_dir からの相対パス）
    ocr: bool
    error: str = ""
    text: str = ""      # 取り出した文字（--text で .txt に残すとき、読み直さずに使う）


def plan(pdfs: list[Path], rs: RuleSet, llm: ChatModel | None = None, forced: str | None = None,
         ocr: bool = True) -> list[Plan]:
    plans = []
    for pdf in pdfs:
        try:
            doc = read_pdf(pdf, ocr=ocr)
        except OCRUnavailable as e:
            plans.append(Plan(pdf, "", "none", {}, None, False, str(e)))
            continue
        except Exception as e:  # noqa: BLE001  壊れたPDFなどは、その1件だけ飛ばす
            plans.append(Plan(pdf, "", "none", {}, None, False, f"読めませんでした: {e}"))
            continue
        m = choose(rs, doc.text, llm, forced)
        values = find_fields(doc.text, m.rule, rs.self_names)
        values.update(rule=m.rule.name if m.rule else "未分類", original=pdf.stem)
        folder = render(m.rule.folder if m.rule else rs.unmatched_folder, values)
        # ファイル名にフォルダの区切りが入らないようにする（保存先の外へ出ないように）
        name = render(m.rule.filename if m.rule else rs.unmatched_filename, values).replace("/", "_").replace("\\", "_")
        if not name.lower().endswith(".pdf"):
            name += ".pdf"
        dest = Path(*[p for p in Path(folder).parts if p not in ("..", ".", "/", "\\")]) / name
        plans.append(Plan(pdf, m.rule.name if m.rule else "", m.by, values, dest, doc.used_ocr, text=doc.text))
    return plans


def _free_path(path: Path) -> Path:
    if not path.exists():
        return path
    for i in range(2, 1000):
        p = path.with_name(f"{path.stem}_{i}{path.suffix}")
        if not p.exists():
            return p
    raise FileExistsError(path)


def execute(plans: list[Plan], out_dir: Path, move: bool = False, text: bool = False, form: bool = False,
            rules: RuleSet | None = None) -> list[Path]:
    """予定どおりにファイルを置く。同じ名前のファイルがあれば _2, _3 … を付ける（上書きしない）。

    form=True のときは、当てはまったルールに様式があれば、様式に差し込んだファイルも同じ名前で隣に置く。
    """
    out_dir = out_dir.resolve()
    placed = []
    log = out_dir / LOG_NAME
    out_dir.mkdir(parents=True, exist_ok=True)
    new_log = not log.exists()
    with log.open("a", encoding="utf-8-sig" if new_log else "utf-8", newline="") as f:
        w = csv.writer(f)
        if new_log:
            w.writerow(["日時", "元のファイル", "置いた先", "ルール", "決め方", "OCR", "日付", "相手先", "金額"])
        for p in plans:
            if p.error or p.dest is None:
                continue
            target = (out_dir / p.dest).resolve()
            if out_dir not in target.parents:   # 念のため、保存先の外には置かない
                continue
            target.parent.mkdir(parents=True, exist_ok=True)
            target = _free_path(target)
            (shutil.move if move else shutil.copy2)(str(p.source), str(target))
            if text:
                target.with_suffix(".txt").write_text(p.text, encoding="utf-8")
            if form and rules and p.rule:
                from .form import fill

                rule = rules.get(p.rule)
                if rule.templates:
                    doc = read_pdf(target, ocr=p.ocr, tables=True)
                    for template in rule.templates.values():
                        fill(template, target, doc, p.fields)
            placed.append(target)
            w.writerow([datetime.now().astimezone().isoformat(timespec="seconds"), p.source.name,
                        str(target.relative_to(out_dir)), p.rule or "未分類", p.by, "あり" if p.ocr else "",
                        p.fields.get("date", ""), p.fields.get("company", ""), p.fields.get("amount", "")])
    return placed
