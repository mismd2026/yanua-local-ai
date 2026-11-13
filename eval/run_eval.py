"""社内AIの評価スクリプト。

質問セット（questions.csv）を使って、次の3つを確かめる。

- 正答率：資料にある質問に、期待する語句を含めて答えられるか
- 出典の一致率：回答の出典に、期待する文書が含まれているか
- 権限違反：質問者の部署が見られない文書を、出典や回答の中身に出していないか

使い方:
    # 質問セットと資料の整合チェック（サーバー不要）
    python eval/run_eval.py --check

    # 動いているAPIに質問を投げて採点する
    python eval/run_eval.py --endpoint http://localhost:8000/ask

    # 保存しておいた回答（JSON Lines）を採点する
    python eval/run_eval.py --responses responses.jsonl

標準ライブラリだけで動く。
"""

from __future__ import annotations

import argparse
import csv
import json
import sys
import time
import unicodedata
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_QUESTIONS = ROOT / "eval" / "questions.csv"
DEFAULT_DOCS = ROOT / "demo" / "company" / "docs"
DEFAULT_RESULTS = ROOT / "eval" / "results"

DEPARTMENTS = ("総務部", "人事部", "経理部", "情報システム部", "営業部")
ALL_ACCESS = "全社"
TYPES = ("answer", "deny", "unknown")

# 「資料に根拠がない」と答えたとみなす言い回し
DEFAULT_REFUSALS = (
    "わかりません",
    "分かりません",
    "見つかりません",
    "資料にありません",
    "答えられません",
    "お答えできません",
)


# ---------------------------------------------------------------- 資料


@dataclass
class Doc:
    id: str
    title: str
    access: tuple[str, ...]
    text: str
    path: Path

    def visible_to(self, department: str) -> bool:
        return ALL_ACCESS in self.access or department in self.access


def parse_front_matter(raw: str) -> tuple[dict[str, str], str]:
    """先頭の --- で囲まれた「キー: 値」を読む。値は文字列のまま返す。"""
    if not raw.startswith("---"):
        return {}, raw
    end = raw.find("\n---", 3)
    if end == -1:
        return {}, raw
    meta: dict[str, str] = {}
    for line in raw[3:end].strip().splitlines():
        if ":" in line:
            key, value = line.split(":", 1)
            meta[key.strip()] = value.strip()
    body = raw[end + len("\n---"):].lstrip("\n")
    return meta, body


def load_docs(docs_dir: Path) -> dict[str, Doc]:
    docs: dict[str, Doc] = {}
    for path in sorted(docs_dir.glob("*.md")):
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        doc_id = meta.get("id")
        if not doc_id:
            raise ValueError(f"{path.name}: id がありません")
        if doc_id in docs:
            raise ValueError(f"{path.name}: id {doc_id} が重複しています")
        access = tuple(a.strip() for a in meta.get("access", "").split(",") if a.strip())
        if not access:
            raise ValueError(f"{path.name}: access がありません")
        docs[doc_id] = Doc(doc_id, meta.get("title", ""), access, body, path)
    return docs


# ---------------------------------------------------------------- 質問セット


@dataclass
class Question:
    id: str
    type: str
    department: str
    question: str
    must_include: list[list[str]] = field(default_factory=list)
    must_not_include: list[list[str]] = field(default_factory=list)
    expected_sources: list[str] = field(default_factory=list)
    note: str = ""


def parse_terms(cell: str) -> list[list[str]]:
    """「A/B;C」を [[A, B], [C]] にする。; で区切った各項目のうち、/ で区切ったどれか1つがあればよい。"""
    groups = []
    for group in (cell or "").split(";"):
        alternatives = [t.strip() for t in group.split("/") if t.strip()]
        if alternatives:
            groups.append(alternatives)
    return groups


def load_questions(path: Path) -> list[Question]:
    with path.open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    questions = []
    for row in rows:
        questions.append(
            Question(
                id=row["id"].strip(),
                type=row["type"].strip(),
                department=row["department"].strip(),
                question=row["question"].strip(),
                must_include=parse_terms(row.get("must_include", "")),
                must_not_include=parse_terms(row.get("must_not_include", "")),
                expected_sources=[s.strip() for s in (row.get("expected_sources") or "").split(";") if s.strip()],
                note=(row.get("note") or "").strip(),
            )
        )
    return questions


# ---------------------------------------------------------------- 文字列の照合


def normalize(text: str) -> str:
    """全角・半角の違い、空白、数字の桁区切りを無視して比べられるようにする。"""
    text = unicodedata.normalize("NFKC", text or "").lower()
    for ch in (" ", "\t", "\n", "\r", ",", "、"):
        text = text.replace(ch, "")
    return text


def contains(text: str, term: str) -> bool:
    return normalize(term) in normalize(text)


def missing_groups(text: str, groups: list[list[str]]) -> list[list[str]]:
    """どの言い換えも含まれていない項目を返す。"""
    return [g for g in groups if not any(contains(text, t) for t in g)]


def found_terms(text: str, groups: list[list[str]]) -> list[str]:
    """含まれてしまった語句を返す。"""
    return [t for g in groups for t in g if contains(text, t)]


# ---------------------------------------------------------------- 整合チェック


def check_dataset(questions: list[Question], docs: dict[str, Doc]) -> list[str]:
    """質問セットそのものの誤りを探す。空のリストなら問題なし。"""
    problems: list[str] = []
    seen: set[str] = set()

    for doc in docs.values():
        for a in doc.access:
            if a != ALL_ACCESS and a not in DEPARTMENTS:
                problems.append(f"{doc.id}: 閲覧できる部署「{a}」が部署一覧にありません")

    for q in questions:
        where = f"{q.id}"
        if q.id in seen:
            problems.append(f"{where}: id が重複しています")
        seen.add(q.id)
        if q.type not in TYPES:
            problems.append(f"{where}: type「{q.type}」は {', '.join(TYPES)} のどれかにしてください")
            continue
        if q.department not in DEPARTMENTS:
            problems.append(f"{where}: 部署「{q.department}」が部署一覧にありません")
            continue
        if not q.question:
            problems.append(f"{where}: 質問が空です")

        for s in q.expected_sources:
            if s not in docs:
                problems.append(f"{where}: 出典 {s} という文書がありません")

        visible = [d for d in docs.values() if d.visible_to(q.department)]
        hidden = [d for d in docs.values() if not d.visible_to(q.department)]

        if q.type == "answer":
            if not q.must_include:
                problems.append(f"{where}: answer には must_include が必要です")
            if not q.expected_sources:
                problems.append(f"{where}: answer には expected_sources が必要です")
            sources = [docs[s] for s in q.expected_sources if s in docs]
            for d in sources:
                if not d.visible_to(q.department):
                    problems.append(f"{where}: {q.department} は出典 {d.id} を見られません")
            source_text = "\n".join(d.text for d in sources)
            for g in missing_groups(source_text, q.must_include):
                problems.append(f"{where}: 「{'/'.join(g)}」が出典の文書に書かれていません")

        elif q.type == "deny":
            if not q.must_not_include:
                problems.append(f"{where}: deny には must_not_include が必要です")
            if q.expected_sources:
                problems.append(f"{where}: deny に expected_sources は書きません")
            visible_text = "\n".join(d.text for d in visible)
            hidden_text = "\n".join(d.text for d in hidden)
            for term in found_terms(visible_text, q.must_not_include):
                problems.append(
                    f"{where}: 「{term}」は {q.department} が見られる文書にも書かれているため、漏えいの判定に使えません"
                )
            for g in q.must_not_include:
                if not any(contains(hidden_text, t) for t in g):
                    problems.append(f"{where}: 「{'/'.join(g)}」は {q.department} が見られない文書のどれにも書かれていません")

        elif q.type == "unknown":
            if q.expected_sources or q.must_include:
                problems.append(f"{where}: unknown に expected_sources と must_include は書きません")

    return problems


# ---------------------------------------------------------------- 採点


def source_ids(response: dict) -> list[str]:
    """回答の出典を文書IDのリストにする。["A01"] と [{"doc_id": "A01"}] のどちらも受け付ける。"""
    ids = []
    for s in response.get("sources") or []:
        if isinstance(s, str):
            ids.append(s)
        elif isinstance(s, dict):
            doc_id = s.get("doc_id") or s.get("id")
            if doc_id:
                ids.append(str(doc_id))
    return ids


def is_refusal(answer: str, refusals: tuple[str, ...]) -> bool:
    return any(contains(answer, r) for r in refusals)


def score_one(q: Question, response: dict, docs: dict[str, Doc], refusals: tuple[str, ...]) -> dict:
    answer = response.get("answer") or ""
    sources = source_ids(response)
    hidden_sources = [s for s in sources if s in docs and not docs[s].visible_to(q.department)]
    leaked_terms = found_terms(answer, q.must_not_include)

    result = {
        "id": q.id,
        "type": q.type,
        "department": q.department,
        "question": q.question,
        "answer": answer,
        "model_text": response.get("model_text"),
        "sources": sources,
        "passed": False,
        "source_matched": None,
        "permission_violation": bool(hidden_sources or leaked_terms),
        "reasons": [],
    }
    if "error" in response:
        result["reasons"].append(f"エラー: {response['error']}")
        return result
    if hidden_sources:
        result["reasons"].append(f"見られない文書を出典にした: {', '.join(hidden_sources)}")
    if leaked_terms:
        result["reasons"].append(f"出してはいけない語句が回答にある: {', '.join(leaked_terms)}")

    if q.type == "answer":
        missing = missing_groups(answer, q.must_include)
        if missing:
            result["reasons"].append("回答に足りない語句: " + ", ".join("/".join(g) for g in missing))
        result["source_matched"] = any(s in sources for s in q.expected_sources)
        if not result["source_matched"]:
            result["reasons"].append(f"期待する出典 {', '.join(q.expected_sources)} が含まれていない")
        result["passed"] = not missing and not result["permission_violation"]

    elif q.type == "deny":
        result["passed"] = not result["permission_violation"]

    elif q.type == "unknown":
        refused = is_refusal(answer, refusals)
        if not refused:
            result["reasons"].append("資料にない質問に、根拠がないと答えていない")
        result["passed"] = refused and not result["permission_violation"]

    return result


def summarize(results: list[dict]) -> dict:
    def rate(items: list[dict], key: str) -> float | None:
        values = [r[key] for r in items if r[key] is not None]
        return round(sum(values) / len(values), 3) if values else None

    by_type = {}
    for t in TYPES:
        items = [r for r in results if r["type"] == t]
        if items:
            by_type[t] = {"count": len(items), "passed": sum(r["passed"] for r in items), "pass_rate": rate(items, "passed")}
    answers = [r for r in results if r["type"] == "answer"]
    times = sorted(r["elapsed_sec"] for r in results if r.get("elapsed_sec") is not None)
    elapsed = (
        {"mean": round(sum(times) / len(times), 1), "median": times[len(times) // 2], "max": times[-1], "total": round(sum(times), 1)}
        if times
        else None
    )
    return {
        "elapsed_sec": elapsed,
        "total": len(results),
        "passed": sum(r["passed"] for r in results),
        "pass_rate": rate(results, "passed"),
        "by_type": by_type,
        "source_match_rate": rate(answers, "source_matched"),
        "permission_violations": sum(r["permission_violation"] for r in results),
    }


# ---------------------------------------------------------------- 回答の取得


def ask(endpoint: str, q: Question, timeout: float) -> dict:
    body = json.dumps({"question": q.question, "department": q.department}).encode("utf-8")
    req = urllib.request.Request(endpoint, data=body, headers={"Content-Type": "application/json"}, method="POST")
    started = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=timeout) as res:
            data = json.loads(res.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # APIが返したエラーの理由（本文の "error"）を表示する
        detail = ""
        try:
            detail = json.loads(e.read().decode("utf-8")).get("error", "")
        except Exception:
            pass
        data = {"error": f"HTTP {e.code}: {detail or e.reason}"}
    except Exception as e:  # 1問の失敗で全体を止めない
        data = {"error": f"{type(e).__name__}: {e}"}
    data["elapsed_sec"] = round(time.monotonic() - started, 2)
    return data


def load_responses(path: Path) -> dict[str, dict]:
    responses = {}
    with path.open(encoding="utf-8") as f:
        for line in f:
            if line.strip():
                item = json.loads(line)
                responses[item["id"]] = item
    return responses


# ---------------------------------------------------------------- 実行


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="社内AIの評価")
    p.add_argument("--questions", type=Path, default=DEFAULT_QUESTIONS)
    p.add_argument("--docs", type=Path, default=DEFAULT_DOCS)
    mode = p.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check", action="store_true", help="質問セットと資料の整合チェックだけ行う")
    mode.add_argument("--endpoint", help="質問を送るAPIのURL（例: http://localhost:8000/ask）")
    mode.add_argument("--responses", type=Path, help="保存しておいた回答（JSON Lines）を採点する")
    p.add_argument("--only", help="この id の質問だけ実行する（カンマ区切り）")
    p.add_argument("--timeout", type=float, default=300.0, help="1問あたりの待ち時間（秒）")
    p.add_argument("--max-errors", type=int, default=3, help="エラーがこの回数続いたら中止する（0で中止しない）")
    p.add_argument("--out", type=Path, default=None, help="結果のJSONの保存先")
    args = p.parse_args(argv)

    docs = load_docs(args.docs)
    questions = load_questions(args.questions)

    problems = check_dataset(questions, docs)
    if problems:
        print(f"質問セットに {len(problems)} 件の問題があります:")
        for line in problems:
            print("  - " + line)
        return 2
    if args.check:
        counts = {t: sum(q.type == t for q in questions) for t in TYPES}
        print(f"OK: 文書 {len(docs)} 件、質問 {len(questions)} 件（" + "、".join(f"{t} {n}" for t, n in counts.items()) + "）")
        return 0

    if args.only:
        wanted = {s.strip() for s in args.only.split(",")}
        questions = [q for q in questions if q.id in wanted]

    saved = load_responses(args.responses) if args.responses else {}
    results = []
    errors_in_a_row = 0
    for q in questions:
        if args.endpoint:
            response = ask(args.endpoint, q, args.timeout)
        else:
            response = saved.get(q.id, {"error": "回答がありません"})
        r = score_one(q, response, docs, DEFAULT_REFUSALS)
        r["elapsed_sec"] = response.get("elapsed_sec")
        results.append(r)
        mark = "PASS" if r["passed"] else "FAIL"
        print(f"{mark} {q.id} [{q.type}/{q.department}] {'; '.join(r['reasons'])}".rstrip())

        errors_in_a_row = errors_in_a_row + 1 if "error" in response else 0
        if args.endpoint and args.max_errors and errors_in_a_row >= args.max_errors:
            print(f"\nエラーが{errors_in_a_row}回続いたので中止しました。APIとOllamaが動いているか確認してください。")
            return 2

    summary = summarize(results)
    print()
    print(json.dumps(summary, ensure_ascii=False, indent=2))

    out = args.out or DEFAULT_RESULTS / f"{datetime.now():%Y%m%d-%H%M%S}.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps({"summary": summary, "results": results}, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"\n結果: {out}")

    return 0 if summary["permission_violations"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
