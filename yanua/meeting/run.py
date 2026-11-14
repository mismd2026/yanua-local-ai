"""打ち合わせの記録の流れ：文字起こし → 話し手 → 要約 → 議事録。"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

from . import minutes
from .speakers import PRESETS, label_segments
from .summarize import Summary, summarize
from .transcribe import Segment, build_prompt


@dataclass
class Result:
    segments: list[Segment]
    summary: Summary
    files: list[Path] = field(default_factory=list)
    info: dict = field(default_factory=dict)


def company_words(db) -> tuple[list[str], list[str]]:
    """人事データ（在籍中の人）の氏名と部署名。言葉のヒントと、話し手の名前の表記合わせに使う。"""
    from .. import hr

    hr.ensure_schema(db)
    rows = list(db.execute("SELECT name, dept FROM employees WHERE active = 1 ORDER BY employee_no"))
    return [r[0] for r in rows], sorted({r[1] for r in rows})


def load_segments(path: Path) -> list[Segment]:
    return [Segment(float(s["start"]), float(s["end"]), s["text"], s.get("speaker", "")) for s in
            json.loads(path.read_text(encoding="utf-8"))]


def run(media: Path, *, title: str, out_dir: Path, llm=None, transcriber=None, segments: list[Segment] | None = None,
        tool: str = "zoom", names: list[str] | None = None, depts: list[str] | None = None,
        participants: list[str] | None = None, glossary: list[str] | None = None, template: Path | None = None,
        meeting_date: date | None = None, place: str = "", debug: Path | None = None) -> Result:
    names, participants = names or [], participants or []
    info: dict = {}
    if segments is None:
        prompt = build_prompt(glossary or [], [*participants, *names], depts or [])
        segments, info = transcriber.transcribe(media, prompt)
    if tool != "none" and tool in PRESETS:
        info["speakers_found"] = label_segments(media, segments, tool, [*participants, *names], debug)
    summary = summarize(llm, segments, title, [*participants, *names]) if llm is not None else Summary()

    out_dir.mkdir(parents=True, exist_ok=True)
    d = meeting_date or date.today()
    stem = f"{d:%Y%m%d}_議事録_{title}".replace("/", "_")
    v = minutes.values(title, f"{d.year}年{d.month}月{d.day}日", place, minutes.attendees(segments, participants),
                       summary, media, segments)
    files = [out_dir / f"{stem}.md", out_dir / f"{stem}_発言.json"]
    files[0].write_text(minutes.to_markdown(v, summary), encoding="utf-8")
    files[1].write_text(json.dumps([s.as_dict() for s in segments], ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if template:
        files.append(minutes.to_docx(template, out_dir / f"{stem}.docx", v, summary))
    return Result(segments, summary, files, info)
