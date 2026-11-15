"""問い合わせてきた人の本人確認（K-050）。

現場の問い合わせは、相手ごとに言い方がばらばら。番号を覚えていない人、名前だけで済むと思っている人、
まだ人事データに入っていない新人もいる。マイク越しでは名前を聞き間違えやすい（イシモダ→シモダ）。
そこで、言い方のパターンを網羅しようとはせず、確かめ方を決めておく。

- 何で相手を特定するかは会社ごとに違う（社員コードが極秘で、部署コードと名前で探す会社もある）。
  使う手がかり・確定する組み合わせ・最初の聞き方は「本人確認の方針」（config/identity.json）で決める
- 返事は順番を問わず、ルールで手がかり（従業員番号・名前・部署（部署名か部署コード））を取り出す
- 方針の組み合わせのどれかが、同じ1人を指したときだけ本人と確定する。
  既定では、名前はフルネームが合ったときだけ数える（姓だけでは数えず、番号などを求める）
- 確定しなければ、返ってきた言葉に合わせて、確定に足りないものを聞き返す
  （部署だけ → 「お名前か従業員番号」、名前がヒットしない → 「従業員番号」）
- 言われた内容は、照合できてもできなくても、そのまま票に残す。確定できなくても依頼は受ける
  （人に引き継ぎ、見せる文書は全社向けだけにする）。他の人の名前や部署を、候補として相手に見せない
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from . import hr

CLUES = ("employee_no", "name", "dept")
LABELS = {"employee_no": "従業員番号", "name": "お名前", "dept": "所属部署"}
MATCH_LABELS = {"employee_no": "従業員番号", "name": "名前", "dept": "部署"}

# 本人確認の方針の型。config/identity.json の "preset" で選び、項目ごとに上書きできる
PRESETS: dict[str, dict] = {
    "標準": {
        "_説明": "所属部署・お名前・従業員番号のうち2つが同じ1人を指したら確定",
        "question": "すみません。本人確認を行う都合上、所属部署とお名前と従業員番号を教えていただけますか？",
        "clues": ["dept", "name", "employee_no"],
        "confirm": [["name", "employee_no"], ["name", "dept"], ["employee_no", "dept"]],
    },
    "相手に委ねる": {
        "_説明": "最初は何を言ってもらってもよい。返ってきた言葉に合わせて、足りないものを聞く",
        "question": "すみません。本人確認を行う都合上、お名前、所属部署、または従業員番号を教えていただけますか？",
        "clues": ["name", "dept", "employee_no"],
        "confirm": [["name", "employee_no"], ["name", "dept"], ["employee_no", "dept"]],
    },
    "社員コードを使わない": {
        "_説明": "社員コード（従業員番号）を極秘にしている会社向け。部署（部署名か部署コード）と名前で確定",
        "question": "すみません。本人確認を行う都合上、お名前と所属部署（部署名か部署コード）を教えていただけますか？",
        "clues": ["name", "dept"],
        "confirm": [["name", "dept"]],
    },
    "社員コード必須": {
        "_説明": "必ず従業員番号を含めて確定する",
        "question": "すみません。本人確認を行う都合上、従業員番号とお名前を教えていただけますか？",
        "clues": ["employee_no", "name", "dept"],
        "confirm": [["employee_no", "name"], ["employee_no", "dept"]],
    },
}
DEFAULTS = {"preset": "標準", "surname_counts": False, "max_follow_ups": 2}

# 返事から名前を取り出すときに除く言葉（長いものから順に除く）
FILLERS = sorted([
    "すみません", "すいません", "えっと", "えーと", "あの", "よろしくお願いします", "お願いします",
    "従業員番号", "社員番号", "社員コード", "部署コード", "番号", "コード", "お名前", "名前", "氏名",
    "所属部署", "所属", "部署", "でございます", "と申します", "といいます", "になります", "です",
    "わかりません", "わからない", "不明", "私", "わたし", "僕", "俺",
], key=len, reverse=True)
# 名前としては受け取らない言葉（問い合わせの中身を名前と取り違えないため）
NOT_NAME = ["できません", "できない", "ありません", "ので", "ください", "つながら", "使えない", "壊れ", "困っ",
            "お願い", "?", "？", "!", "！"]
NAME_MARKS = ("と申します", "といいます", "でございます", "です")


class PolicyError(ValueError):
    pass


@dataclass
class Policy:
    question: str
    clues: list[str]
    confirm: list[list[str]]
    surname_counts: bool = False
    max_follow_ups: int = 2
    preset: str = "標準"

    @classmethod
    def default(cls) -> "Policy":
        return build_policy({})


def build_policy(data: dict) -> Policy:
    """方針の設定（型の名前と上書きする項目）から Policy を作る。書き間違いはエラーにする。"""
    preset = data.get("preset", DEFAULTS["preset"])
    if preset not in PRESETS:
        raise PolicyError(f"知らない型です: {preset}（使える型: {'・'.join(PRESETS)}）")
    merged = {**DEFAULTS, **{k: v for k, v in PRESETS[preset].items() if not k.startswith("_")},
              **{k: v for k, v in data.items() if not k.startswith("_")}}
    clues, confirm = merged["clues"], merged["confirm"]
    if not isinstance(clues, list) or not clues or any(c not in CLUES for c in clues):
        raise PolicyError(f"clues には {'・'.join(CLUES)} の中から選んで並べてください")
    if not isinstance(confirm, list) or not confirm:
        raise PolicyError("confirm には、確定する手がかりの組み合わせを1つ以上書いてください")
    for combo in confirm:
        if not isinstance(combo, list) or len(combo) < 2 or len(set(combo)) != len(combo):
            raise PolicyError(f"confirm の組み合わせは、違う手がかりを2つ以上にしてください: {combo}")
        if any(c not in clues for c in combo):
            raise PolicyError(f"confirm の組み合わせに、clues にない手がかりがあります: {combo}")
    if not str(merged["question"]).strip():
        raise PolicyError("question（最初の聞き方）が空です")
    n = merged["max_follow_ups"]
    if not isinstance(n, int) or not 0 <= n <= 3:
        raise PolicyError("max_follow_ups は 0〜3 にしてください")
    return Policy(str(merged["question"]), list(clues), [list(c) for c in confirm],
                  bool(merged["surname_counts"]), n, preset)


def load_policy(path: Path | None) -> Policy:
    if not path or not Path(path).exists():
        return Policy.default()
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except ValueError as e:
        raise PolicyError(f"{path} をJSONとして読めません: {e}") from e
    return build_policy(data)


# ---------------------------------------------------------------- 返事から取り出す


@dataclass
class Declared:
    """相手が言ったこと（照合の前）。"""

    employee_no: str = ""
    name: str = ""
    dept: str = ""
    said: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"employee_no": self.employee_no, "name": self.name, "dept": self.dept, "said": " / ".join(self.said)}

    def given(self) -> set[str]:
        return {c for c in CLUES if getattr(self, c)}


def _digit_runs(text: str, dept_alias: str) -> list[str]:
    t = hr.name_key(text)
    if dept_alias:
        t = t.replace(dept_alias, " ")   # 「第2営業部」の2を番号と取り違えない
    t = re.sub(r"(?<=\d)[-ー‐－](?=\d)", "", t)
    return re.findall(r"\d{2,12}", t)


def looks_like_name(text: str) -> bool:
    t = hr.norm(text)
    return 2 <= len(t) <= 20 and not re.search(r"\d", t) and not any(w in t for w in NOT_NAME)


def _declared_name(text: str, numbers: list[str], dept_alias: str, full: list[dict], surname: list[dict]) -> str:
    """返事から、言われた名前を取り出す（控えとして票に残す）。取り出せなければ空。"""
    key = hr.name_key(text)
    for p in full:   # 人事データの名前がそのまま出てくれば、それを言った名前として使う
        if hr.name_key(p["name"]) in key:
            return p["name"]
    marked, plain = [], []
    for clause in re.split(r"[、。,.!?！？/]+", unicodedata.normalize("NFKC", text)):
        has_mark = any(m in clause for m in NAME_MARKS)
        c = clause
        for w in [*numbers, dept_alias, *FILLERS]:
            if w:
                c = re.sub(re.escape(w), "|", c, flags=re.IGNORECASE)
        pieces = [x.strip() for x in c.split("|") if x.strip(" のはでがもと")]
        c = pieces[-1] if pieces else ""
        c = c.rsplit("の", 1)[-1] if "の" in c[1:] else c   # 「総務部の新井」→「新井」
        c = c.strip(" のはでがもと")
        if looks_like_name(c):
            (marked if has_mark else plain).append(c)
    if marked or plain:   # 「〜です」「〜と申します」の直前を優先する
        return (marked or plain)[0]
    return hr.surname_of(surname[0]["name"]) if len(surname) == 1 else ""


def parse(db, said: list[str], policy: Policy | None = None) -> tuple[Declared, list[dict], list[dict]]:
    """返事（何回分でも）から、番号・部署・名前と、名前が一致した人の候補を取り出す。"""
    policy = policy or Policy.default()
    text = " ".join(said)
    dept, alias = hr.dept_in_text(db, text) if "dept" in policy.clues else ("", "")
    number = ""
    numbers: list[str] = []
    for t in said:   # あとの返事を優先
        runs = _digit_runs(t, alias)
        numbers += runs
        for run in sorted(runs, key=len, reverse=True):
            # 部署コードとして使える数字は部署に。それ以外のいちばん長い数字を従業員番号に
            if "dept" in policy.clues and not dept and hr.dept_by_code(db, run):
                if not (run.isdigit() and hr.lookup(db, run)):
                    dept, alias = hr.dept_by_code(db, run), ""
                    continue
            if "employee_no" in policy.clues and len(run) >= 3:
                number = run
                break
    full, surname = hr.names_in_text(db, text, dept) if "name" in policy.clues else ([], [])
    name = ""
    for t in said:
        name = _declared_name(t, numbers, alias, full, surname) or name
    return Declared(number, name, dept, list(said)), full, surname


# ---------------------------------------------------------------- 照合


@dataclass
class Result:
    person: dict | None
    matched: list[str]
    note: str
    declared: Declared
    ask: list[str]           # 次に聞くもの（確定に足りないもの）
    ask_any: bool = False    # True なら「どれか1つ」、False なら「全部」
    partial_name: bool = False   # 姓だけ言われた（フルネームを聞く）

    def question(self) -> str:
        return follow_up(self.ask, self.ask_any, self.partial_name)

    @property
    def missing(self) -> list[str]:   # 前の名前（互換）
        return self.ask

    def requester(self, base: dict) -> dict:
        d = self.declared
        if self.person:
            r = {**self.person, **base, "verified": "人事データと一致"}
        else:
            r = {"employee_no": d.employee_no, "name": d.name, "dept": d.dept, "dept_code": "", **base,
                 "verified": "照合できず"}
        return {**r, "matched": self.matched, "declared": d.as_dict(), "note": self.note}


def check(db, said: list[str], policy: Policy | None = None) -> Result:
    """方針の組み合わせのどれかが同じ1人を指したら、その人と確定する。"""
    policy = policy or Policy.default()
    d, full, surname = parse(db, said, policy)
    by_no = hr.lookup(db, d.employee_no) if d.employee_no else None
    text_key = hr.name_key(" ".join(said))

    # 名前が合った人：フルネームの一致。姓だけの一致は、方針で数えるときだけ
    named = {p["employee_no"]: p for p in full}
    if not named and policy.surname_counts:
        named = {p["employee_no"]: p for p in surname}
    if by_no and not full and policy.surname_counts and by_no["employee_no"] not in named:
        sur = hr.name_key(hr.surname_of(by_no["name"]))
        if sur and sur in text_key:   # 姓の検索は部署で絞っているので、番号の人の姓はここで確かめる
            named[by_no["employee_no"]] = by_no

    people = dict(named)
    if by_no:
        people[by_no["employee_no"]] = by_no
    scored = []
    for no, p in people.items():
        m = set()
        if by_no and no == by_no["employee_no"]:
            m.add("employee_no")
        if no in named:
            m.add("name")
        if d.dept and p["dept"] == d.dept:
            m.add("dept")
        scored.append((p, m))
    hits = [(p, m) for p, m in scored if any(set(c) <= m for c in policy.confirm)]
    best = max((len(m) for _, m in hits), default=0)
    sure = [(p, m) for p, m in hits if len(m) == best]

    if len(sure) == 1:
        p, m = sure[0]
        note = ""
        if d.employee_no and "employee_no" not in m:
            note = "言われた従業員番号は一致しませんでした（聞き間違いの可能性）"
        return Result(p, [MATCH_LABELS[c] for c in CLUES if c in m], note, d, [])

    # どの手がかりが「言われたのに合わなかった」か
    given = d.given()
    found = set()
    if by_no:
        found.add("employee_no")
    if named:
        found.add("name")
    if d.dept:
        found.add("dept")
    failed = given - found
    if "name" in given and not full and surname and not policy.surname_counts:
        failed.discard("name")   # 姓だけ合った：名前は「合わなかった」ではなく「足りない」
        partial_name = True
    else:
        partial_name = False

    if len(sure) > 1 or len(named) > 1 or (partial_name and len(surname) > 1):
        note = "候補が複数います（同姓同名など）"
    elif not people and not surname and (d.name or d.employee_no):
        note = "人事データに見当たりません（新人など未登録の可能性）"
    elif people or surname:
        note = "確定に足りる手がかりがそろいませんでした"
    else:
        note = "本人確認に使える内容を聞き取れませんでした"

    ask, ask_any = _next_ask(policy, found, failed, ambiguous=len(sure) > 1 or len(named) > 1)
    return Result(None, [], note, d, ask, ask_any, partial_name)


def _next_ask(policy: Policy, found: set[str], failed: set[str], ambiguous: bool) -> tuple[list[str], bool]:
    """確定に足りないものを、返ってきた言葉に合わせて選ぶ。
    合わなかった手がかりを含む組み合わせは使わない（名前がヒットしなければ、番号の組み合わせへ）。"""
    needs = []
    for combo in policy.confirm:
        if set(combo) & failed:
            continue
        need = [c for c in combo if c not in found]
        if ambiguous and not need:
            # 候補が複数：組み合わせはそろっているので、まだ言われていない手がかりで絞る
            need = [c for c in policy.clues if c not in found and c not in failed][:1]
        if need:
            needs.append(need)
    if not needs:
        return [], False
    shortest = min(len(n) for n in needs)
    options = [n for n in needs if len(n) == shortest]
    if shortest == 1:
        # 「お名前か従業員番号」のように、どれか1つでよい
        singles = [c for c in policy.clues if [c] in options]
        return singles, len(singles) > 1
    return [c for c in policy.clues if c in options[0]], False


def follow_up(ask: list[str], any_of: bool = False, partial_name: bool = False) -> str:
    labels = {**LABELS, "name": "お名前（フルネーム）"} if partial_name else LABELS
    items = ("か" if any_of else "と").join(labels[k] for k in ask)
    return f"恐れ入りますが、{items}を教えていただけますか？（わからなければ「わからない」で大丈夫です）"


# 前の版との互換（既定の方針の最初の聞き方）
QUESTION = PRESETS["標準"]["question"]
