"""要約・決まったこと・宿題（社内のLLM）。

長い会議は小さなモデル（3b・7b）に一度で渡しきれないので、区切って読み取り、あとでまとめる。
LLMの答えはそのまま信じず、決まった形（JSON）以外は捨てる。宿題の担当者は、会議に出てきた名前のときだけ残す。

小さなモデルは「決まったこと」や「私が〜します」の担当を落としやすいので、決まった言い方は発言から直接拾って補う:
    決まったこと：「〜で決定します」「〜に決めます」「〜ことにします」などを含む文
    宿題：「〇〇さん、〜してください」（担当は〇〇さん）、「私が〜します」（担当は話し手）。
          期限は、担当者のその後の発言の「〜までに」から取る
    次回：「次回は〜に開催します」
"""

from __future__ import annotations

import re
import difflib
from dataclasses import dataclass, field

from ..flows.base import ask_json
from .transcribe import Segment, fmt_time

CHUNK_CHARS = 2400


@dataclass
class Summary:
    summary: str = ""
    points: list[str] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    todos: list[dict] = field(default_factory=list)   # {"who", "what", "due"}
    next_meeting: str = ""


DECISION = re.compile(r"(で決定|決定します|決定しました|決まりました|に決めます|に決めました|ことにします|ことにしました)")
ASK = re.compile(r"([^\s、。「」]{1,6}?)(?:さん|様)、(.+?)(してください|てください|をお願いします|お願いします)")
SELF = re.compile(r"(?:私|わたし|自分)が(.+?)(?:します|しておきます|やります|いたします)")
DUE = re.compile(r"([^\s、。]{2,15}?)までに")
NEXT = re.compile(r"次回は(.+?)に(?:開催|行い|実施)")


def _plain(body: str, ending: str) -> str:
    """「〜してください」「〜てください」を「〜する」「〜る」の形にする（直せないものは「〜て」のまま）。"""
    if ending == "してください":
        return body + "する"
    if ending == "てください":
        return body + ("る" if body and body[-1] not in "っんいきぎしち" else "て")
    return body


def _sentences(text: str) -> list[str]:
    return [x.strip() for x in re.split(r"(?<=[。！？!?])", text) if x.strip()]


def _similar(a: str, b: str, ratio: float = 0.55) -> bool:
    a, b = _norm(a), _norm(b)
    return a in b or b in a or difflib.SequenceMatcher(None, a, b).ratio() > ratio


def _full_name(short: str, names: list[str]) -> str:
    hits = [n for n in names if _norm(n).startswith(_norm(short))]
    return hits[0] if len(hits) == 1 else short


def rule_items(segments: list[Segment], names: list[str]) -> tuple[list[str], list[dict], str]:
    """決まった言い方の発言から、決まったこと・宿題・次回を拾う。"""
    decisions, todos, nxt = [], [], ""
    for i, s in enumerate(segments):
        for sent in _sentences(s.text):
            if DECISION.search(sent):
                decisions.append(re.sub(r"^(では|それでは|じゃあ)、?", "", sent).rstrip("。"))
            m = ASK.search(sent)
            if m:
                todos.append({"who": _full_name(m[1], names), "what": _plain(m[2].rstrip("、"), m[3]), "due": "", "_i": i})
            m = SELF.search(sent)
            if m and s.speaker and s.speaker != "不明":
                todos.append({"who": s.speaker, "what": m[1].rstrip("、") + "する", "due": "", "_i": i})
            m = NEXT.search(sent)
            if m:
                nxt = m[1]
    for t in todos:   # 期限：担当者のその後の発言（3件まで）の「〜までに」
        for s in segments[t["_i"]: t["_i"] + 4]:
            m = DUE.search(s.text)
            if m and _norm(s.speaker) and (_norm(t["who"]).startswith(_norm(s.speaker)[:2]) or _norm(s.speaker).startswith(_norm(t["who"])[:2])):
                t["due"] = m[1]
                break
        del t["_i"]
    return decisions, todos, nxt

def chunks(segments: list[Segment], size: int = CHUNK_CHARS) -> list[str]:
    out, cur = [], ""
    for s in segments:
        line = f"[{fmt_time(s.start)}]{(' ' + s.speaker) if s.speaker else ''}：{s.text}\n"
        if cur and len(cur) + len(line) > size:
            out.append(cur)
            cur = ""
        cur += line
    if cur:
        out.append(cur)
    return out


def _strings(v) -> list[str]:
    return [str(x).strip() for x in v if str(x).strip()] if isinstance(v, list) else []


def _norm(t: str) -> str:
    return re.sub(r"\s", "", t)


def summarize(llm, segments: list[Segment], title: str = "", known_names: list[str] | None = None) -> Summary:
    text_all = _norm("".join(s.text + s.speaker for s in segments))
    names = [n for n in (known_names or []) if n]
    result = Summary()
    for part in chunks(segments):
        data = ask_json(llm, (
            f"次は会議「{title}」の文字起こしの一部です。内容を読み取り、JSONで返してください。\n"
            '形式: {"points": [話し合った要点], "decisions": [決まったこと], '
            '"todos": [{"who": 担当者, "what": やること, "due": 期限（なければ空）}]}\n'
            "書かれていないことは書かない。決まっていないことは decisions に入れない。\n\n" + part
        ))
        result.points += _strings(data.get("points"))
        result.decisions += _strings(data.get("decisions"))
        for t in data.get("todos", []) if isinstance(data.get("todos"), list) else []:
            if not isinstance(t, dict) or not str(t.get("what", "")).strip():
                continue
            who = str(t.get("who", "")).strip()
            # 会議に出てこない名前（LLMが作った名前）は担当者にしない
            if who and _norm(who) not in text_all and not any(_norm(who) in _norm(n) or _norm(n) in _norm(who) for n in names):
                who = ""
            result.todos.append({"who": who or "（未定）", "what": str(t["what"]).strip(),
                                 "due": str(t.get("due", "")).strip()})
    result.points = list(dict.fromkeys(result.points))
    result.decisions = list(dict.fromkeys(result.decisions))
    # 決まった言い方の発言から拾ったものを補う（LLMの結果と重なるものは足さない）
    decisions, todos, result.next_meeting = rule_items(segments, names)
    for d in decisions:
        if not any(_similar(d, x, 0.45) for x in result.decisions):
            result.decisions.append(d)
    for t in todos:
        same = [x for x in result.todos if _similar(t["what"], x["what"])]
        if not same:
            result.todos.append(t)
        else:   # LLMが担当や期限を落としていたら、発言から拾ったもので埋める
            for x in same:
                if x["who"] == "（未定）":
                    x["who"] = t["who"]
                x["due"] = x["due"] or t["due"]
    for t in result.todos:   # 担当者の表記を、人事データ・出席者の名前にそろえる（「田中」→「田中 健」）
        t["who"] = _full_name(t["who"], names) if t["who"] != "（未定）" else t["who"]
    if result.points:
        decided = "\n".join(f"- {d}" for d in result.decisions) or "（なし）"
        data = ask_json(llm, (
            f"会議「{title}」の要点です。全体を3〜5文で要約し、JSONで返してください。形式: {{\"summary\": 要約}}\n"
            "「決まった」と書いてよいのは、下の「決まったこと」にあるものだけです。\n\n"
            + "\n".join(f"- {p}" for p in result.points) + f"\n\n決まったこと:\n{decided}"
        ))
        result.summary = str(data.get("summary", "")).strip()
    return result
