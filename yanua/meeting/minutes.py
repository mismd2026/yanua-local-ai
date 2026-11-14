"""議事録を作る：Markdown（いつも作る）と、会社の様式の Word（様式があれば）。

Word の様式には、紙の書類の差し込み（yanua/paper/form.py）と同じ書き方で差し込む位置を書く:
    {{title}} {{date_jp}} {{place}} {{attendees}} {{summary}} {{decisions}} {{next}} {{source}} {{transcript}}
    宿題の表の1行に {{items.担当}} {{items.内容}} {{items.期限}}
"""

from __future__ import annotations

from collections import Counter
from pathlib import Path

from .summarize import Summary
from .transcribe import Segment, fmt_time

NOTE = "この議事録は Yanua が録画・録音から作った下書きです。発言者・決まったこと・宿題は、出席者が確認してください。"


def attendees(segments: list[Segment], extra: list[str] | None = None) -> list[str]:
    """話し手として読み取れた名前（発言の多い順）と、指定された出席者。"""
    counted = Counter(s.speaker for s in segments if s.speaker and s.speaker != "不明")
    names = [n for n, _ in counted.most_common()]
    return names + [n for n in (extra or []) if n not in names]


def transcript_text(segments: list[Segment]) -> str:
    return "\n".join(f"[{fmt_time(s.start)}] {s.speaker or '不明'}：{s.text}" for s in segments)


def values(title: str, date_jp: str, place: str, people: list[str], summary: Summary, source: Path,
           segments: list[Segment]) -> dict[str, str]:
    return {
        "title": title, "date_jp": date_jp, "place": place, "attendees": "、".join(people),
        "summary": summary.summary or "\n".join(summary.points[:5]),
        "decisions": "\n".join(f"・{d}" for d in summary.decisions) or "（なし）",
        "next": summary.next_meeting, "source": source.name, "note": NOTE, "transcript": transcript_text(segments),
    }


def to_markdown(v: dict[str, str], summary: Summary) -> str:
    lines = [f"# 議事録：{v['title']}", "", f"- 日時：{v['date_jp']}", f"- 場所：{v['place'] or '（記載なし）'}",
             f"- 出席者（発言者）：{v['attendees'] or '（読み取れず）'}", f"- 元の録画・録音：{v['source']}"]
    if v["next"]:
        lines.append(f"- 次回：{v['next']}")
    lines += ["",
             f"> {NOTE}", "", "## 要約", "", v["summary"] or "（なし）", "", "## 決まったこと", "", v["decisions"], "",
             "## 宿題", ""]
    if summary.todos:
        lines += ["| 担当 | 内容 | 期限 |", "|---|---|---|"]
        lines += [f"| {t['who']} | {t['what']} | {t['due'] or '―'} |" for t in summary.todos]
    else:
        lines.append("（なし）")
    if summary.points:
        lines += ["", "## 話し合った要点", ""] + [f"- {p}" for p in summary.points]
    lines += ["", "## 発言の記録", "", "```", v["transcript"], "```", ""]
    return "\n".join(lines)


def to_docx(template: Path, dest: Path, v: dict[str, str], summary: Summary) -> Path:
    from ..paper.form import fill_docx

    rows = [[t["who"], t["what"], t["due"]] for t in summary.todos]
    return fill_docx(template, dest, v, ["担当", "内容", "期限"], rows)
