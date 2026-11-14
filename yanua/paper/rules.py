"""PDFの仕分けルール。どの種類の書類か決め、ファイル名と保存先を作る。

ルールはJSONで書く（会社ごとに作り、選べる）:

    {
      "self_names": ["デモ商事"],                 // 自社の名前（相手先の会社名から除く）
      "rules": [
        {
          "name": "請求書",
          "keywords": {"any": ["請求書", "ご請求"], "all": [], "none": ["見積"]},
          "fields": {"invoice_no": "請求番号[:：\\s]*([A-Za-z0-9\\-]+)"},   // 独自の項目（正規表現の1つ目のかっこ）
          "filename": "{date}_請求書_{company}_{amount}円.pdf",
          "folder": "経理/請求書/{yyyy}/{mm}",
          "priority": 0,
          "templates": {"xlsx": "templates/請求書.xlsx"}   // 会社の様式（任意）。差し込み方は form.py
        }
      ],
      "unmatched": {"folder": "未分類", "filename": "{original}"}
    }

ファイル名と保存先で使える項目:
    {date}（YYYYMMDD）{yyyy} {mm} {dd} {amount}（数字だけ）{company}（相手先）{to}（「御中」の宛先）
    {rule}（ルール名）{original}（元のファイル名、拡張子なし）と、fields に書いた独自の項目
読み取れなかった項目は「不明」になる。

当てはまるルールの選び方:
    all の言葉がすべてあり、any の言葉が1つ以上あり（any が空なら問わない）、none の言葉がないルールが候補。
    priority が高い順、同じなら any の言葉が多く当たった順、書類の上のほう（表題）に言葉があるものを優先。
    どれにも当たらないときは、LLM（社内）にルール名の中から選ばせることもできる（選択肢以外の答えは使わない）。
"""

from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from ..llm import ChatModel

UNKNOWN = "不明"

# よくある書類の種類（ルールの下書きを作るときの手がかり）
KNOWN_TYPES = ["請求書", "見積書", "納品書", "領収書", "注文書", "発注書", "契約書", "覚書", "議事録", "申請書",
               "報告書", "通知書", "案内", "検収書", "支払通知書", "履歴書", "稟議書"]

DATE_LABELS = r"(?:発行日|請求日|作成日|日付|開催日|納品日|契約日|見積日|発注日)"
DATE_PATTERNS = [
    r"(20\d{2})\s*[年/.\-]\s*(\d{1,2})\s*[月/.\-]\s*(\d{1,2})\s*日?",
    r"令和\s*(元|\d{1,2})\s*年\s*(\d{1,2})\s*月\s*(\d{1,2})\s*日",
]
AMOUNT = re.compile(r"(?:合計|請求金額|ご請求金額|御請求金額|総額|税込合計|お見積金額|見積金額)[^\d¥￥\n]{0,10}[¥￥]?\s*([\d,]{3,})")
COMPANY = re.compile(r"((?:株式会社|有限会社|合同会社)\s?[^\s　、。,（(御様]+|[^\s　、。,（(]+?(?:株式会社|有限会社|合同会社))")
BAD_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


class RuleError(ValueError):
    pass


@dataclass
class Rule:
    name: str
    any: list[str] = field(default_factory=list)
    all: list[str] = field(default_factory=list)
    none: list[str] = field(default_factory=list)
    fields: dict[str, str] = field(default_factory=dict)
    filename: str = "{date}_{rule}_{company}.pdf"
    folder: str = "{rule}/{yyyy}"
    priority: int = 0
    templates: dict[str, Path] = field(default_factory=dict)   # 形式（xlsx / docx）→ 様式のファイル


@dataclass
class RuleSet:
    rules: list[Rule]
    self_names: list[str] = field(default_factory=list)
    unmatched_folder: str = "未分類"
    unmatched_filename: str = "{original}"

    def names(self) -> list[str]:
        return [r.name for r in self.rules]

    def get(self, name: str) -> Rule:
        for r in self.rules:
            if r.name == name:
                return r
        raise RuleError(f"ルール「{name}」はありません（ある: {'、'.join(self.names())}）")


def load_rules(path: Path) -> RuleSet:
    data = json.loads(path.read_text(encoding="utf-8"))
    rules = []
    for i, r in enumerate(data.get("rules", []), start=1):
        if not r.get("name"):
            raise RuleError(f"{path.name}: {i}番目のルールに name がありません")
        kw = r.get("keywords", {})
        templates = {}
        for fmt, t in r.get("templates", {}).items():
            if fmt not in ("xlsx", "docx"):
                raise RuleError(f"{path.name}: ルール「{r['name']}」の様式は xlsx か docx にしてください（{fmt}）")
            templates[fmt] = (path.parent / t).resolve()   # ルールのファイルからの相対パス
        rule = Rule(r["name"], kw.get("any", []), kw.get("all", []), kw.get("none", []), r.get("fields", {}),
                    r.get("filename", Rule.filename), r.get("folder", Rule.folder), int(r.get("priority", 0)), templates)
        for key, pattern in rule.fields.items():
            try:
                if re.compile(pattern).groups < 1:
                    raise RuleError(f"{path.name}: ルール「{rule.name}」の {key} の正規表現に、かっこ（取り出す部分）がありません")
            except re.error as e:
                raise RuleError(f"{path.name}: ルール「{rule.name}」の {key} の正規表現が正しくありません: {e}") from e
        rules.append(rule)
    if len({r.name for r in rules}) != len(rules):
        raise RuleError(f"{path.name}: 同じ名前のルールがあります")
    un = data.get("unmatched", {})
    return RuleSet(rules, data.get("self_names", []), un.get("folder", "未分類"), un.get("filename", "{original}"))


def norm(text: str) -> str:
    return unicodedata.normalize("NFKC", text or "")


# ------------------------------------------------------------------ 項目の読み取り


def find_date(text: str) -> tuple[str, str, str] | None:
    t = norm(text)
    labeled = re.search(DATE_LABELS + r"[^\n\d令]{0,6}(" + "|".join(DATE_PATTERNS) + ")", t)
    candidates = [labeled.group(1)] if labeled else []
    for line in t.splitlines():
        candidates.append(line)
    for c in candidates:
        m = re.search(DATE_PATTERNS[0], c)
        if m:
            y, mo, d = int(m[1]), int(m[2]), int(m[3])
        else:
            m = re.search(DATE_PATTERNS[1], c)
            if not m:
                continue
            y, mo, d = 2018 + (1 if m[1] == "元" else int(m[1])), int(m[2]), int(m[3])
        if 1 <= mo <= 12 and 1 <= d <= 31:
            return f"{y:04d}", f"{mo:02d}", f"{d:02d}"
    return None


def find_fields(text: str, rule: Rule | None, self_names: list[str]) -> dict[str, str]:
    t = norm(text)
    out: dict[str, str] = {}
    date = find_date(t)
    if date:
        out.update(yyyy=date[0], mm=date[1], dd=date[2], date="".join(date))
    m = AMOUNT.search(t)
    if m:
        out["amount"] = m[1].replace(",", "")
    to = re.search(r"([^\s　]+?)\s*御中", t)
    if to:
        out["to"] = to[1]
    selfs = [norm(s) for s in self_names]
    for c in COMPANY.findall(t):
        c = c.strip()
        after = t[t.find(c) + len(c): t.find(c) + len(c) + 3]
        if "御中" in after or any(s and s in c for s in selfs):
            continue
        out["company"] = c
        break
    if rule:
        for key, pattern in rule.fields.items():
            m = re.search(pattern, t)
            if m and m.group(1).strip():
                out[key] = m.group(1).strip()
    return out


def render(template: str, values: dict[str, str]) -> str:
    def sub(m: re.Match) -> str:
        v = values.get(m[1]) or UNKNOWN
        return BAD_CHARS.sub("_", v).strip(" .") or UNKNOWN
    return re.sub(r"\{(\w+)\}", sub, template)


# ------------------------------------------------------------------ ルールの選択


@dataclass
class Match:
    rule: Rule | None
    score: int = 0
    by: str = ""     # rule / llm / forced / none


def _score(rule: Rule, text: str) -> int | None:
    t = norm(text)
    if any(norm(w) in t for w in rule.none):
        return None
    if not all(norm(w) in t for w in rule.all):
        return None
    hits = [w for w in rule.any if norm(w) in t]
    if rule.any and not hits:
        return None
    head = "\n".join(t.splitlines()[:3])
    title = 5 if any(norm(w) in head for w in rule.any + rule.all) else 0
    return rule.priority * 100 + len(hits) + title


def choose(rs: RuleSet, text: str, llm: ChatModel | None = None, forced: str | None = None) -> Match:
    if forced:
        return Match(rs.get(forced), by="forced")
    scored = [(s, i, r) for i, r in enumerate(rs.rules) if (s := _score(r, text)) is not None]
    if scored:
        s, _, r = max(scored, key=lambda x: (x[0], -x[1]))
        return Match(r, s, "rule")
    if llm is not None and rs.rules:
        names = rs.names()
        prompt = ("次の書類が、どの種類に当たるかを1つだけ選んでください。当たるものがなければ「なし」と答えてください。"
                  f"答えは種類の名前だけにしてください。\n種類: {'、'.join(names)}\n\n書類の最初の部分:\n{norm(text)[:600]}")
        try:
            answer = llm.chat([{"role": "user", "content": prompt}]).strip()
        except Exception:  # noqa: BLE001  LLMは補助なので、失敗しても止めない
            answer = ""
        for n in names:
            if answer == n or answer.strip("「」。 ") == n:
                return Match(rs.get(n), by="llm")
    return Match(None, by="none")


# ------------------------------------------------------------------ ルールの下書き


def draft_rule(text: str, name: str | None = None, llm: ChatModel | None = None) -> dict:
    """見本のPDFの文字から、ルールの下書きを作る。下書きは人が確かめてからルールのファイルに足す。"""
    t = norm(text)
    head = "\n".join(t.splitlines()[:6])
    kind = name or next((k for k in KNOWN_TYPES if k in head), None) or next((k for k in KNOWN_TYPES if k in t), None)
    keywords = [kind] if kind and kind in t else []
    if llm is not None:
        prompt = ("次の書類を見分けるのに使える言葉（書類の中にそのまま出てくる短い言葉）を、3つまで挙げてください。"
                  "1行に1つずつ、言葉だけを書いてください。\n\n" + t[:800])
        try:
            for line in llm.chat([{"role": "user", "content": prompt}]).splitlines():
                w = line.strip(" ・-*0123456789.、「」")
                if 2 <= len(w) <= 15 and w in t and w not in keywords:   # 書類にない言葉は採らない
                    keywords.append(w)
        except Exception:  # noqa: BLE001
            pass
    kind = kind or "（種類の名前）"
    return {
        "name": kind,
        "keywords": {"any": keywords[:4], "all": [], "none": []},
        "fields": {},
        "filename": "{date}_" + kind + "_{company}.pdf",
        "folder": kind + "/{yyyy}/{mm}",
        "priority": 0,
    }
