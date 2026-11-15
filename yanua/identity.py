"""問い合わせてきた人の本人確認（K-050）。

現場の問い合わせは、相手ごとに言い方がばらばら。番号を覚えていない人、名前だけで済むと思っている人、
まだ人事データに入っていない新人もいる。マイク越しでは名前を聞き間違えやすい（イシモダ→シモダ）。
そこで、言い方のパターンを網羅しようとはせず、確かめ方を決めておく。

- 聞き方は1つに決める（QUESTION）。返事は順番を問わず、ルールで番号・部署・名前を取り出す
- 番号・名前・部署はそれぞれ「手がかりの1つ」。3つのうち2つが同じ1人を指したときだけ本人と確定する
  （番号を聞き間違えても、名前と部署で確定できる。名前の一致だけでは確定しない）
- 言われた内容は、照合できてもできなくても、そのまま票に残す（担当者が後で調べられるように）
- 確定できないのは失敗ではない。人に引き継ぎ、見せる文書は全社向けだけにする
- 他の人の名前や部署を、候補として相手に見せない
"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass, field

from . import hr

QUESTION = "すみません。本人確認を行う都合上、所属部署とお名前と従業員番号を教えていただけますか？"

LABELS = {"dept": "所属部署", "name": "お名前", "employee_no": "従業員番号"}

# 返事から名前を取り出すときに除く言葉（長いものから順に除く）
FILLERS = sorted([
    "すみません", "すいません", "えっと", "えーと", "あの", "よろしくお願いします", "お願いします",
    "従業員番号", "社員番号", "番号", "お名前", "名前", "氏名", "所属部署", "所属", "部署",
    "でございます", "と申します", "といいます", "になります", "です", "わかりません", "わからない", "不明",
    "私", "わたし", "僕", "俺",
], key=len, reverse=True)
# 名前としては受け取らない言葉（問い合わせの中身を名前と取り違えないため）
NOT_NAME = ["できません", "できない", "ありません", "ので", "ください", "つながら", "使えない", "壊れ", "困っ", "お願い", "?", "？", "!", "！"]


@dataclass
class Declared:
    """相手が言ったこと（照合の前）。"""

    employee_no: str = ""
    name: str = ""
    dept: str = ""
    said: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"employee_no": self.employee_no, "name": self.name, "dept": self.dept, "said": " / ".join(self.said)}


@dataclass
class Result:
    person: dict | None
    matched: list[str]
    note: str
    declared: Declared
    missing: list[str]

    def requester(self, base: dict) -> dict:
        d = self.declared
        if self.person:
            r = {**self.person, **base, "verified": "人事データと一致"}
        else:
            r = {"employee_no": d.employee_no, "name": d.name, "dept": d.dept, "dept_code": "", **base,
                 "verified": "照合できず"}
        return {**r, "matched": self.matched, "declared": d.as_dict(), "note": self.note}


def _number(text: str, dept_alias: str = "") -> str:
    """従業員番号：部署の言い方（「第2営業部」の2など）を除いてから、いちばん長い数字の並び。"""
    t = hr.name_key(text)
    if dept_alias:
        t = t.replace(dept_alias, " ")
    t = re.sub(r"(?<=\d)[-ー‐－](?=\d)", "", t)
    runs = re.findall(r"\d{3,12}", t)
    return max(runs, key=len) if runs else ""


def looks_like_name(text: str) -> bool:
    t = hr.norm(text)
    return 2 <= len(t) <= 20 and not re.search(r"\d", t) and not any(w in t for w in NOT_NAME)


NAME_MARKS = ("と申します", "といいます", "でございます", "です")


def _declared_name(text: str, number: str, dept_alias: str, full: list[dict], surname: list[dict]) -> str:
    """返事から、言われた名前を取り出す（控えとして票に残す）。取り出せなければ空。"""
    key = hr.name_key(text)
    # 人事データの名前がそのまま出てくれば、それを言った名前として使う
    for p in full:
        if hr.name_key(p["name"]) in key:
            return p["name"]
    marked, plain = [], []
    for clause in re.split(r"[、。,.!?！？/]+", unicodedata.normalize("NFKC", text)):
        has_mark = any(m in clause for m in NAME_MARKS)
        c = clause
        for w in [number, dept_alias, *FILLERS]:
            if w:
                c = re.sub(re.escape(w), "|", c, flags=re.IGNORECASE)
        pieces = [x.strip() for x in c.split("|") if x.strip(" のはでがもと")]
        c = pieces[-1] if pieces else ""
        c = c.rsplit("の", 1)[-1] if "の" in c[1:] else c   # 「総務部の新井」→「新井」
        c = c.strip(" のはでがもと")
        if looks_like_name(c):
            (marked if has_mark else plain).append(c)
    # 「〜です」「〜と申します」の直前を優先する
    if marked or plain:
        return (marked or plain)[0]
    return hr.surname_of(surname[0]["name"]) if len(surname) == 1 else ""


def parse(db, said: list[str]) -> tuple[Declared, list[dict], list[dict]]:
    """返事（何回分でも）から、番号・部署・名前と、名前が一致した人の候補を取り出す。"""
    text = " ".join(said)
    dept, alias = hr.dept_in_text(db, text)
    number = ""
    for t in said:   # あとの返事を優先
        number = _number(t, alias) or number
    full, surname = hr.names_in_text(db, text, dept)
    name = ""
    for t in said:   # 名前は、それぞれの返事から取り出す（あとの返事を優先）
        n = _declared_name(t, number, alias, full, surname)
        name = n or name
    return Declared(number, name, dept, list(said)), full, surname


def check(db, said: list[str]) -> Result:
    """3つのうち2つ（番号・名前・部署）が同じ1人を指したら、その人と確定する。"""
    d, full, surname = parse(db, said)
    by_no = hr.lookup(db, d.employee_no) if d.employee_no else None
    # フルネームが一致した人がいれば、その人たちだけを「名前が合った」とする（姓だけ合う大勢は数えない）
    named = {p["employee_no"]: p for p in (full or surname)}
    if by_no and by_no["employee_no"] not in named and not full:
        # 番号の人の姓が返事に出ていれば、その人の名前も合ったとする（姓の検索は部署で絞っているため）
        key = hr.name_key(" ".join(said))
        sur = hr.name_key(hr.surname_of(by_no["name"]))
        if hr.name_key(by_no["name"]) in key or (sur and sur in key):
            named[by_no["employee_no"]] = by_no
    people = dict(named)
    if by_no:
        people[by_no["employee_no"]] = by_no
    scored = []
    for no, p in people.items():
        m = []
        if by_no and no == by_no["employee_no"]:
            m.append("従業員番号")
        if no in named:
            m.append("名前")
        if d.dept and p["dept"] == d.dept:
            m.append("部署")
        scored.append((p, m))
    best = max((len(m) for _, m in scored), default=0)
    # いちばん多く合った人が2つ以上合っていて、しかもその人だけのときに確定する
    sure = [(p, m) for p, m in scored if len(m) == best and best >= 2]
    missing = [k for k in ("dept", "name", "employee_no") if not getattr(d, k)]

    if len(sure) == 1:
        p, m = sure[0]
        note = ""
        if d.employee_no and "従業員番号" not in m:
            note = "言われた従業員番号は一致しませんでした（聞き間違いの可能性）"
        return Result(p, m, note, d, [])
    if len(sure) > 1 or len(named) > 1:
        note = "候補が複数います（同姓同名など）"
    elif not people and (d.name or d.employee_no):
        note = "人事データに見当たりません（新人など未登録の可能性）"
    elif people:
        note = "手がかりが1つしか一致しませんでした"
    else:
        note = "本人確認に使える内容を聞き取れませんでした"
    return Result(None, [], note, d, missing)


def follow_up(missing: list[str]) -> str:
    items = "と".join(LABELS[k] for k in missing)
    return f"恐れ入りますが、{items}も教えていただけますか？（わからなければ「わからない」で大丈夫です）"
