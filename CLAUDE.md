# CLAUDE.md — yanua-local-ai

社内で動かすローカルAI「Yanua」。社内文書の検索と回答、ヘルプデスク、社外向けの受付、紙の書類の仕分け、打ち合わせの記録を、外部のAIサービスを使わずに社内のマシンだけで行う。全体は README.md、画面と API の設計は docs/screens.md。

## 話し方・書き方

- やりとりは日本語。利用者向けの文言・README・コメントも日本語で、やさしい言葉で書く（カタカナ語や略語を増やさない）
- コミットメッセージは英語でよい

## 環境

- WSL（Ubuntu）の `~/WORKSPACE/yanua-local-ai`。Python 3.12
- 仮想環境はこのリポジトリの `.venv`。**`langgraph-lab` の .venv と混同しない**。作業の前に `source .venv/bin/activate` し、`which python` が `~/WORKSPACE/yanua-local-ai/.venv/bin/python` になっていることを確かめる
- LLM は Ollama（http://localhost:11434）。モデルは `qwen2.5:3b-instruct`（既定）と `qwen2.5:7b`（要約は 7b が安定）。GPU なし
- OCR：tesseract-ocr・tesseract-ocr-jpn・poppler-utils・fonts-ipafont-gothic を apt で導入済み
- `sqlite3` コマンドは入っていない。SQLite の中身は Python で読む

## よく使うコマンド

```bash
python -m pytest -q                         # テスト（Ollama は使わない。偽のモデルで確かめる）
python eval/run_eval.py --check             # 評価の質問セットの整合チェック
python eval/retrieval_eval.py --top-k 5     # 検索の評価（権限違反0件であること）
pip-audit -r requirements.txt               # 依存の脆弱性
python -m yanua serve --dev-login           # API（http://127.0.0.1:8000/api/docs）
python -m yanua helpdesk --demo 1           # ヘルプデスクのデモ（Ollama を使う）
python eval/bench_identity.py --employees 1000000   # 本人確認の速さ（100万人）
```

API を /api/docs から試すときは、右上の Authorize で `X-Yanua-Client` に何か入れてから、dev-login（例 `{"employee_no": "10004"}`）する。

## 作業の手順（必ず守る）

1. `git switch main && git pull && git switch -c <ブランチ>`（main で直接作業しない）
2. 変更 → `python -m pytest -q` がすべて通ることを確かめる
3. **コミットの前に `git branch --show-current` でブランチを確かめる**（main に誤ってコミットしたことがある）
4. `git push -u origin <ブランチ>` → `gh pr create --base main --head <ブランチ> --title ... --body ...`（`--fill` は入力待ちで止まることがあるので使わない）
5. `gh pr checks --watch` で CI が ✓ になってからマージ：`gh pr merge --merge --delete-branch`
6. マージは、利用者が実機で確かめてから。勝手にマージしない

## 設計の原則（利用者と合意済み）

- **小さなモデルの出力は、決まったルールで補正・検証する。** LLM の答えをそのまま信じない。業務に響く判断（本人確認・セキュリティ・影響の範囲・優先度）はルールで固める
- **社外向けの処理は、社内の情報を出さない作りにする。** 検索の段階で除く。返事の違い（文言・形・時間）から社員の在籍などを推測させない
- **権限は API の側で必ず確かめる。** 画面で隠すだけにしない。部署は「見られる文書」、役割は「使える画面と操作」を決める
- **通信は「限定して安全に」。** config/network.json の許可リストの宛先だけ、明示したコマンドのときだけ。モデルは版を固定し SHA-256 で照合
- **本人確認は「方針」で決める**（config/identity.json）。言い方のパターンは網羅しない。名前はフルネームのときだけ手がかりに数える。確定しなくても依頼は受け、申告内容を票に残す。「ない・わからない」と言われたものは聞き直さない
- 印刷機・電話機の問い合わせは情報システム部（ヘルプデスク）。会議室の予約はカレンダーに任せる。稟議は保留
- 個人情報は必要な列だけ取り込む（人事データの生年月日などは捨てる）。インビテーションコードはハッシュだけを保存

## 課題の管理

- 課題表：Google ドライブ「Yanua 課題表」の「課題（yanua-local-ai）」シート（K-001〜K-051）と「機能の分担」シート
- 作業を始める・終えるときに、該当する課題の「進捗」（未処理・計画中・進行中・レビュー待ち・完了・保留）と備考を更新する
- PR の説明に課題番号（K-0xx）を書く

## 構成の要点

- `yanua/api/`：FastAPI。ログイン（今は開発用のみ。SSO は K-008）、役割（config/roles.json）、CSRF（GET 以外は `X-Yanua-Client` ヘッダー必須）、エラーの形 `{"error": {"code", "message"}}`、ジョブ（SQLite・同時1つ）、ヘルプデスクの会話（data/chat.db にチェックポイント）
- `yanua/identity.py`：本人確認。`yanua/hr.py`：人事データ（照合用の索引つき）
- `yanua/flows/internal/helpdesk`・`yanua/flows/external/reception`：LangGraph の会話
- `yanua/paper/`：PDF の仕分け・変換。`yanua/meeting/`：議事録
- `yanua/server.py` は前の API（`serve-legacy`）。評価のために残している
- `data/` は実行時のデータ（git に入れない）
