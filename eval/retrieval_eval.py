"""検索だけの評価（LLMなし）。

質問セットの各質問を、質問者の部署の権限で検索し、次を測る。

- 出典の命中率（hit@k）：answer の質問で、期待する文書が上位k件に入ったか
- 答えの材料の命中率：上位k件の本文に、must_include の語句がすべて含まれていたか
  （LLMが正しく答えるための材料が、検索で渡せているか）
- 権限違反：質問者が見られない文書が、検索結果に1件でも入ったか（0件が必須）

権限の判定には、検索とは別に資料のファイルを直接読んだ結果を使う。
検索の仕組みが自分で自分を採点しないようにするため。

使い方:
    python eval/retrieval_eval.py            # 上位3件
    python eval/retrieval_eval.py --top-k 5
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "eval"))

import run_eval as ev  # noqa: E402
from yanua.search import search  # noqa: E402
from yanua.store import Store  # noqa: E402


def evaluate(top_k: int, docs_dir: Path = ev.DEFAULT_DOCS, questions_path: Path = ev.DEFAULT_QUESTIONS) -> dict:
    docs = ev.load_docs(docs_dir)
    questions = ev.load_questions(questions_path)

    with tempfile.TemporaryDirectory() as tmp:
        store = Store(Path(tmp) / "index.db")
        report = store.ingest(docs_dir)
        if report.errors:
            raise SystemExit("取り込みに失敗しました: " + "; ".join(report.errors))

        results = []
        for q in questions:
            hits = search(store, q.question, q.department, top_k)
            hit_ids = [h.doc_id for h in hits]
            context = "\n".join(h.text for h in hits)
            hidden = [d for d in hit_ids if not docs[d].visible_to(q.department)]
            r = {
                "id": q.id,
                "type": q.type,
                "department": q.department,
                "hits": hit_ids,
                "permission_violation": bool(hidden),
            }
            if q.type == "answer":
                r["source_hit"] = any(s in hit_ids for s in q.expected_sources)
                r["context_has_answer"] = not ev.missing_groups(context, q.must_include)
            if q.type == "deny":
                r["leaked_terms"] = ev.found_terms(context, q.must_not_include)
                r["permission_violation"] = r["permission_violation"] or bool(r["leaked_terms"])
            results.append(r)
        store.close()

    answers = [r for r in results if r["type"] == "answer"]

    def rate(key: str) -> float:
        return round(sum(r[key] for r in answers) / len(answers), 3)

    summary = {
        "top_k": top_k,
        "questions": len(results),
        "source_hit_rate": rate("source_hit"),
        "context_answer_rate": rate("context_has_answer"),
        "permission_violations": sum(r["permission_violation"] for r in results),
    }
    return {"summary": summary, "results": results}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="検索だけの評価")
    p.add_argument("--top-k", type=int, default=3)
    p.add_argument("--verbose", action="store_true", help="外れた質問を表示する")
    args = p.parse_args(argv)

    out = evaluate(args.top_k)
    for r in out["results"]:
        missed = r["type"] == "answer" and not (r["source_hit"] and r["context_has_answer"])
        if r["permission_violation"] or (args.verbose and missed):
            print(f"{'VIOLATION' if r['permission_violation'] else 'MISS'} {r['id']} [{r['department']}] -> {r['hits']}")
    print(json.dumps(out["summary"], ensure_ascii=False, indent=2))
    return 0 if out["summary"]["permission_violations"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
