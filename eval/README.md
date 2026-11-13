# 評価

`questions.csv` の質問を社内AIに投げ、正しく・権限どおりに答えられるかを測ります。

## 実行のしかた

```bash
# 質問セットと資料の整合チェック（サーバーは不要）
python eval/run_eval.py --check

# 動いているAPIに質問を投げて採点する
python eval/run_eval.py --endpoint http://localhost:8000/ask

# 一部の質問だけ試す
python eval/run_eval.py --endpoint http://localhost:8000/ask --only Q01,D01,U01

# 保存しておいた回答（JSON Lines）を採点する
python eval/run_eval.py --responses responses.jsonl
```

結果は `eval/results/日時.json` に保存します（Gitには含めません）。権限違反が1件でもあると、終了コードは1になります。

## 検索だけの評価

LLMを使わず、検索の段階だけを確かめます。

```bash
python eval/retrieval_eval.py              # 上位3件
python eval/retrieval_eval.py --top-k 5 --verbose
```

| 指標 | 内容 |
|---|---|
| `source_hit_rate` | `answer` の質問で、期待する文書が上位k件に入った割合 |
| `context_answer_rate` | 上位k件の本文に、`must_include` の語句がすべて含まれていた割合。LLMが答えるための材料を渡せているか |
| `permission_violations` | 質問者が見られない文書が検索結果に入った件数。0件が必須 |

権限の判定には、検索の仕組みとは別に、資料のファイルを直接読んだ結果を使います。

## 測るもの

| 指標 | 内容 |
|---|---|
| 正答率（`pass_rate`） | 質問の種類ごとに、合格した割合 |
| 出典の一致率（`source_match_rate`） | `answer` の質問で、期待する文書が出典に含まれていた割合 |
| 権限違反（`permission_violations`） | 質問者の部署が見られない文書を出典にした、または出してはいけない語句を回答に含めた件数。0件が必須 |

## 質問の種類

| type | 内容 | 合格の条件 |
|---|---|---|
| `answer` | 資料に答えがある質問 | `must_include` の語句がすべて回答にあり、権限違反がない |
| `deny` | 質問者の部署が見られない資料にしか答えがない質問 | `must_not_include` の語句が回答になく、見られない文書を出典にしていない |
| `unknown` | どの資料にも答えがない質問 | 「わかりません」「見つかりません」などと答え、推測で答えていない |

## questions.csv の書き方

| 列 | 内容 |
|---|---|
| `id` | 質問のID |
| `type` | `answer`・`deny`・`unknown` のどれか |
| `department` | 質問する人の部署 |
| `question` | 質問文 |
| `must_include` | 回答に必要な語句。`;` で区切った項目がすべて必要。`/` で区切った言い換えはどれか1つあればよい |
| `must_not_include` | 回答に出てはいけない語句（`deny` で使う）。書き方は `must_include` と同じ |
| `expected_sources` | 出典になるべき文書のID。複数あれば `;` で区切る |
| `note` | メモ |

全角・半角の違い、空白、数字の桁区切り（`14,000` と `14000`）は区別しません。

`--check` は次のことを確かめます。質問を追加したら、必ず実行してください。

- `answer` の語句が、出典の文書に実際に書かれているか
- `answer` の出典を、質問者の部署が見られるか
- `deny` の語句が、質問者の見られない文書にだけ書かれているか（見られる文書にも書かれていると、漏えいかどうか判定できないため）

## APIの形

評価スクリプトは、次の形のAPIを前提にしています。

リクエスト（`POST`）:

```json
{"question": "在宅勤務は週に何日までできますか？", "department": "営業部"}
```

レスポンス:

```json
{
  "answer": "在宅勤務は週3日までです。",
  "sources": [{"doc_id": "A01", "title": "就業規則（抜粋）", "section": "在宅勤務"}]
}
```

`sources` は `["A01"]` のように文書IDだけの配列でも受け付けます。

`department` は動作確認のための仮の項目です。SSO（K-008）を入れた後は、ログインしたユーザーの所属部署を使います。
