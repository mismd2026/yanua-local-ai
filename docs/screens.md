# Web画面の全体設計（K-040）

基本の操作は、すべて Web 画面からできるようにします。この文書では、画面の一覧、使う人ごとの入口、画面と API の分担を決めます。
機能ごとの分担（サーバー・Web画面・クライアントアプリ）は、課題表の「機能の分担」シートに書いてあります。この文書はそれを画面と API の単位まで細かくしたものです。

- 状態：確定（2026-11-14）。技術の選び方は「8. 決めたこと」
- 関連：K-041 API の整備、K-042 設定の確認・変更画面、K-049 画面と操作の権限、K-008 SSO、K-048 クライアントアプリ

## 1. 前提

| 前提 | 中身 |
|---|---|
| 重い処理と機密データはサーバーに置く | LLM・OCR・文字起こし、社内文書、人事データ、記録は社内のサーバーだけに置く。画面とクライアントアプリは表示と入力だけを受け持つ |
| 画面は API を呼ぶだけ | 画面だけの機能は作らない。CLI も同じ処理を呼ぶ（`yanua/` の関数を API と CLI の両方から使う） |
| 権限は API の側で確かめる | 画面でボタンを隠すのは使いやすさのためで、守りにはならない。API は呼んだ人の役割を毎回確かめる |
| 設定はファイルのまま | 仕分けルール・様式・routes.csv・kb.csv・対応表・network.json は今のファイルのまま使い、画面から読み書きする |
| 社外向けは分ける | 受付端末は社外の人が触る。受付の API だけを呼べる別の入口にし、社内の画面には移れないようにする |

## 2. 使う人と役割

**部署**と**役割**は別のものとして扱います。

- **部署**（営業部・経理部など）：どの**文書**を見られるかを決める（K-003。今と同じ）。SSO のグループか人事データから受け取る
- **役割**：どの**画面と操作**を使えるかを決める（K-049）。1人が複数の役割を持てる

| 役割 | 使う人 | できること |
|---|---|---|
| 社員 | ログインした全員 | 質問・ヘルプデスク、自分あてのお知らせ、来訪予定の登録、議事録の作成 |
| 総務 | 総務部 | PDF の仕分け・変換、全社の来訪予定、受付の記録、議事録の様式 |
| 情シス | 情報システム部 | 問い合わせ票の対応、切り分け手順・障害情報の編集 |
| 人事 | 人事の担当 | 人事データの取り込み・対応表 |
| 管理者 | Yanua の管理者 | 文書管理、すべての設定、通信の許可リストとモデル、役割の割り当て、監査ログ、評価 |
| 受付端末 | 受付に置くタブレット（人ではなく端末） | 受付の会話、コードの照合、入館票の印刷だけ |

役割の割り当ては、SSO のグループから決めます。グループがないときは、管理者が画面で割り当てます。

## 3. 入口

| 入口 | URL | ログイン | 使う人 |
|---|---|---|---|
| 社内の画面 | `/` | SSO（自宅からは SSO＋MFA） | 社員・総務・情シス・人事・管理者 |
| 受付端末 | `/kiosk` | 端末の登録（管理者が発行する端末トークン） | 来訪者（受付端末） |
| API | `/api/...` | 社内の画面と同じセッション。受付端末は端末トークン | 画面・CLI・クライアントアプリ |

- 社内の画面は1つにまとめ、役割に応じてメニューを出し分けます
- 受付端末は別の画面です。社内の画面のログインは使えず、受付端末のトークンは `/api/kiosk/...` 以外では拒否されます
- 電話・Web の受付は、受付端末と同じ受付の API を使います（電話は前身の ai-callcenter-ollama の音声の仕組みからつなぐ）

## 4. 画面の一覧

段階は「7. 作る順番」の番号です。

### 共通

| ID | 画面 | 役割 | 主な操作 | 呼ぶ API | 関連 | 段階 |
|---|---|---|---|---|---|---|
| S-01 | ログイン | 全員 | SSO へ移る、戻ってきたらホームへ | `GET /api/auth/login`、`GET /api/auth/callback`、`POST /api/auth/logout` | K-008 | 1 |
| S-02 | ホーム | 全員 | 役割に応じたメニュー、未読のお知らせの件数、実行中のジョブ | `GET /api/me`、`GET /api/notifications?unread=1`、`GET /api/jobs?mine=1` | K-049 | 1 |
| S-03 | ジョブの一覧 | 全員（自分のもの）、管理者（すべて） | 進み具合、結果のダウンロード、取り消し | `GET /api/jobs`、`GET /api/jobs/{id}`、`DELETE /api/jobs/{id}` | K-041 | 1 |

### 社員

| ID | 画面 | 役割 | 主な操作 | 呼ぶ API | 関連 | 段階 |
|---|---|---|---|---|---|---|
| S-10 | チャット | 社員 | 質問→出典つきの回答（逐次表示）。出典を開く。ヘルプデスクの会話（本人確認〜問い合わせ票）も同じ画面 | `POST /api/ask`、`POST /api/chat/sessions`、`POST /api/chat/sessions/{id}/messages`、`GET /api/documents/{doc_id}#section` | K-007, K-015 | 1 |
| S-11 | お知らせ | 社員 | 自分あて・部署あての一覧、既読にする | `GET /api/notifications`、`POST /api/notifications/read` | K-045, K-024 | 2 |
| S-12 | 来訪予定（自分の） | 社員 | 来訪予定の登録→コードと案内文を表示（コードはこのとき1回だけ）、一覧、取り消し | `POST /api/visits`、`GET /api/visits?host=me`、`DELETE /api/visits/{id}` | K-018 | 2 |
| S-13 | 議事録 | 社員・総務 | 録画のアップロード→進み具合→話し手・決まったこと・宿題の確認と修正→Word 出力。話し手が「不明」の発言をまとめて直す | `POST /api/meetings`（ジョブ）、`GET /api/meetings/{id}`、`PUT /api/meetings/{id}`、`GET /api/meetings/{id}/minutes.docx` | K-046, K-021 | 3 |

### 総務

| ID | 画面 | 役割 | 主な操作 | 呼ぶ API | 関連 | 段階 |
|---|---|---|---|---|---|---|
| S-20 | PDF の仕分け | 総務 | 受信箱の PDF→仕分けの予定（dry-run）を確認→ルールを選び直す→実行→記録 | `GET /api/paper/inbox`、`POST /api/paper/plans`（ジョブ）、`POST /api/paper/plans/{id}/execute`、`GET /api/paper/history` | K-047, K-020 | 3 |
| S-21 | PDF の変換 | 総務 | PDF を選ぶ→Word / Excel / 会社の様式に変換→ダウンロード | `POST /api/paper/convert`（ジョブ） | K-020 | 3 |
| S-22 | 来訪予定（全社） | 総務 | 日付ごとの一覧、CSV・カレンダーの取り込みと結果（リスケ・取り消しの反映） | `GET /api/visits?date=`、`POST /api/visits/import` | K-018, K-019 | 2 |
| S-23 | 受付の記録 | 総務 | 電話・Web・来訪の記録の一覧、対応済みにする | `GET /api/reception/records`、`POST /api/reception/records/{id}/done` | K-045, K-017 | 2 |

### 情シス

| ID | 画面 | 役割 | 主な操作 | 呼ぶ API | 関連 | 段階 |
|---|---|---|---|---|---|---|
| S-30 | 問い合わせ票 | 情シス | 一覧（優先度・状態で絞り込み）、中身と会話の記録、対応済みにする | `GET /api/tickets`、`GET /api/tickets/{id}`、`PATCH /api/tickets/{id}` | K-045, K-015 | 2 |

切り分け手順（kb.csv）・障害情報（outages.csv）の編集は、S-52 の設定画面で行います（情シスはこの2種類だけ編集できる）。

### 受付端末（`/kiosk`）

| ID | 画面 | 主な操作 | 呼ぶ API | 関連 | 段階 |
|---|---|---|---|---|---|
| S-40 | 受付 | 挨拶→「コードをお持ちの方」「お持ちでない方」→コードの入力、または用件の会話 | `POST /api/kiosk/sessions`、`POST /api/kiosk/sessions/{id}/messages` | K-043, K-017 | 2 |
| S-41 | 予定の表示と入館票 | 照合できた予定の表示、入館票の印刷、担当者へのお知らせ | `POST /api/kiosk/check-in`、`GET /api/kiosk/passes/{id}.html` | K-043, K-018 | 2 |

- 受付端末の画面にはメニューもリンクも置かず、一定時間触られなければ最初の画面に戻ります
- 違う日・間違い・取り消しのコードは同じ返事にします（今の visits.py と同じ）。API も同じ形・同じ時間で返します

### 管理者

| ID | 画面 | 役割 | 主な操作 | 呼ぶ API | 関連 | 段階 |
|---|---|---|---|---|---|---|
| S-50 | 文書管理 | 管理者 | 文書の一覧、登録・差し替え・削除、見られる部署（社外公開を含む）の設定 | `GET /api/documents`、`POST /api/documents`、`PUT /api/documents/{doc_id}`、`DELETE /api/documents/{doc_id}` | K-010, K-002 | 4 |
| S-51 | 人事データ | 人事・管理者 | ファイルを選ぶ→対応表の下書き→確認→取り込み→結果（在籍・退職・エラー） | `POST /api/hr/mapping-draft`、`POST /api/hr/imports`、`GET /api/hr/imports` | K-016 | 4 |
| S-52 | 設定 | 管理者（種類によって総務・情シス・人事も） | 設定の種類を選ぶ→今の中身→編集→検証（エラーの表示）→保存。変更の履歴と前の版への戻し | `GET /api/settings`、`GET /api/settings/{kind}`、`POST /api/settings/{kind}/validate`、`PUT /api/settings/{kind}`、`GET /api/settings/{kind}/history` | K-042, K-009 | 4 |
| S-53 | 通信とモデル | 管理者 | 許可リストの宛先、モデルの一覧・版・ハッシュ、入手（pull）・照合（verify） | `GET /api/models`、`POST /api/models/{name}/pull`（ジョブ）、`POST /api/models/{name}/verify`、設定の `network` | K-023, K-011 | 4 |
| S-54 | 役割の割り当て | 管理者 | 人ごとの役割、SSO のグループとの対応、受付端末の登録・取り消し | `GET /api/roles`、`PUT /api/roles/{employee_no}`、`POST /api/kiosk/devices`、`DELETE /api/kiosk/devices/{id}` | K-049 | 4 |
| S-55 | 監査ログ | 管理者 | 誰が・いつ・何をしたか（質問・出典・設定変更・ログイン）の検索 | `GET /api/audit` | K-009 | 4 |
| S-56 | 評価 | 管理者 | 質問セットでの採点の実行、モデルごとの比較 | `POST /api/eval/runs`（ジョブ）、`GET /api/eval/runs` | K-005, K-006 | 5 |

### S-52 で扱う設定の種類

| 種類（kind） | 今のファイル | 編集できる役割 | 保存の前の検証 |
|---|---|---|---|
| `paper-rules` | 仕分けルール（JSON） | 総務・管理者 | `paper/rules.py` の読み込み。正規表現が読めるか、項目名が使えるか |
| `paper-templates` | 会社の様式（Word・Excel）と差し込み位置 | 総務・管理者 | `paper/form.py` で見本を差し込んでみる |
| `reception-routes` | routes.csv | 総務・管理者 | 列がそろっているか、引き継ぎ先の部署があるか |
| `helpdesk-kb` | kb.csv | 情シス・管理者 | 列がそろっているか、手順が空でないか |
| `helpdesk-outages` | outages.csv | 情シス・管理者 | 日時が読めるか |
| `hr-mapping` | 人事の対応表（JSON） | 人事・管理者 | `hr.py` の load_mapping |
| `meeting-tools` | 会議ツールごとの名前の位置・枠の色 | 総務・管理者 | 値の範囲。デモ動画で読み取りを試せるようにする |
| `meeting-glossary` | 用語集 | 総務・管理者 | 1行に1語 |
| `network` | config/network.json | 管理者だけ | `netpolicy.py` の load_policy。宛先の書き方 |
| `models` | 使う LLM の名前、文字起こしのモデル | 管理者だけ | Ollama にあるか、ハッシュが記録と合うか |

## 5. API の一覧

今の CLI のコマンドとの対応です。「ジョブ」の欄が○のものは、すぐには終わらないのでジョブとして受け付けます（「6. 共通の決まり」）。

| API | CLI | 役割 | ジョブ |
|---|---|---|---|
| `POST /api/ask` | `ask` | 社員 | |
| `GET /api/search` | `search` | 社員 | |
| `POST /api/chat/sessions`、`.../messages` | `helpdesk` | 社員 | |
| `GET/POST/PUT/DELETE /api/documents` | `ingest`、`docs` | 社員（一覧は見られる文書だけ）、管理者（変更） | 取り込みは○ |
| `POST /api/hr/mapping-draft`、`POST /api/hr/imports` | `hr-mapping-draft`、`hr-import` | 人事・管理者 | |
| `POST /api/kiosk/sessions`、`.../messages`、`POST /api/kiosk/check-in` | `reception` | 受付端末 | |
| `GET /api/reception/records` | （新規） | 総務 | |
| `POST/GET/DELETE /api/visits`、`POST /api/visits/import` | `visit-create`、`visit-list`、`visit-import` | 社員（自分の）、総務（全社） | |
| `GET /api/notifications`、`POST /api/notifications/read` | `notifications` | 社員 | |
| `GET /api/tickets`、`PATCH /api/tickets/{id}` | （新規） | 情シス | |
| `POST /api/paper/plans`、`.../execute`、`GET /api/paper/history` | `pdf-sort`（`--dry-run` が plans） | 総務 | ○ |
| `POST /api/paper/convert` | `pdf-convert` | 総務 | ○ |
| `POST /api/paper/rule-draft` | `pdf-rule-draft` | 総務 | ○ |
| `POST /api/paper/text` | `pdf-text` | 総務 | ○ |
| `POST /api/meetings`、`GET/PUT /api/meetings/{id}` | `meeting` | 社員・総務 | ○ |
| `GET /api/models`、`POST .../pull`、`POST .../verify` | `models list / pull / verify` | 管理者 | pull は○ |
| `GET/PUT /api/settings/{kind}`、`.../validate`、`.../history` | （新規。今はファイルを直接編集） | 種類による | |
| `GET /api/roles`、`PUT /api/roles/{employee_no}` | （新規） | 管理者 | |
| `GET /api/audit` | （新規） | 管理者 | |
| `POST /api/eval/runs` | `eval/run_eval.py` | 管理者 | ○ |
| `GET /api/jobs`、`GET/DELETE /api/jobs/{id}` | （新規） | 本人・管理者 | |
| `GET /api/me`、`/api/auth/*` | （新規） | 全員 | |
| `GET /health` | `serve` | だれでも（中身は件数だけ） | |

今の `POST /ask` は、`/api/ask` に移すまで残します。`department` を本文で渡す仕組みは、開発用の設定のときだけ使えるようにします（下の「ログイン」）。

## 6. 共通の決まり

### ログインと権限

- 本番は SSO（OIDC を先に、SAML は後で）。ログインできたら、サーバーがセッションのクッキーを発行します（HttpOnly・Secure・SameSite=Lax）
- 開発用に「SSO なし」の設定を用意し、そのときだけ画面で社員を選べるようにします。この設定は 127.0.0.1 で待ち受けているときだけ有効にし、本番で誤って使えないようにします
- API はすべて、最初に「この役割でこの操作をしてよいか」を確かめます。表に書いていない操作は拒否します（許可するものだけを書く）
- 文書の権限は今と同じく、検索の段階で質問者の部署によって除きます

### 会話（ヘルプデスク・受付）

- 会話は、サーバーで「会話の番号」ごとに状態を持ちます。今の LangGraph の `interrupt` で質問を返し、次のメッセージで再開します
- 今は `InMemorySaver` なので、サーバーを再起動すると会話が消えます。API にするときは SQLite に保存する仕組みに替え、一定時間（例：30分）使われなかった会話は消します
- 返事は逐次表示（Server-Sent Events）にします。小さなモデルでも、待たされている感じを減らすためです

### ジョブ（重い処理）

- 文字起こし・OCR・仕分けの予定・モデルの入手は、受け付けたらすぐ `202` とジョブの番号を返します
- `GET /api/jobs/{id}` で、状態（待ち・実行中・完了・失敗・取り消し）、進み具合（%と今の段階）、結果を返します
- GPU がないので、重いジョブは同時に1つずつ実行します（待ち行列）。同時に流す数は設定で変えられるようにします
- 結果のファイル（議事録の Word、変換した Excel など）は、サーバーに一定期間だけ置いて、期限が来たら消します

### ファイルの受け渡し

- アップロードしたファイル（録画・PDF）は、サーバーの作業用フォルダに置きます。大きさの上限を決めます（録画は例として 2GB）
- ダウンロードは、その人が見てよいものだけ。リンクを他の人に渡しても開けないよう、毎回セッションで確かめます
- クライアントアプリ（K-048）は、開いたファイルを、利用者が自分で保存したとき以外は手元に残しません

### 設定の保存

- 画面で開いたときの版（ファイルのハッシュ）を一緒に送り、その間にだれかが変えていたら保存を断ります（上書きの事故を防ぐ）
- 保存の前に必ず検証し、エラーは行や項目の位置つきで返します。正しい設定だけを保存します
- 保存のたびに前の版を残し、監査ログに「誰が・いつ・どの設定を・どう変えたか」を残します

### エラーの形

すべての API で同じ形にします。

```json
{"error": {"code": "forbidden", "message": "この操作をする権限がありません"}}
```

`message` はそのまま画面に出せる日本語にします。中の詳しい理由（どの権限が足りないかなど）は監査ログにだけ残し、受付端末には出しません。

### 監査ログに残すもの

ログイン・ログアウト、質問と出典の文書番号（本文は残すかどうかを設定で選ぶ）、設定の変更、文書の変更、役割の変更、ジョブの実行、モデルの入手、受付端末の登録。

## 7. 作る順番

| 段階 | 作るもの | 画面 | 課題 |
|---|---|---|---|
| 1 | API の土台（ルーティング・ログイン（開発用）・権限の確認・ジョブ・エラーの形）、チャットとヘルプデスク | S-01〜S-03、S-10 | K-041, K-049, K-007 |
| 2 | 受付端末・来訪予定・お知らせ・問い合わせ票・受付の記録 | S-11, S-12, S-22, S-23, S-30, S-40, S-41 | K-043, K-045, K-018 |
| 3 | 総務：PDF の仕分け・変換、議事録 | S-13, S-20, S-21 | K-046, K-047 |
| 4 | 管理：文書・人事・設定・通信とモデル・役割・監査ログ、本物の SSO | S-50〜S-55 | K-010, K-042, K-009, K-008 |
| 5 | 評価の画面、クライアントアプリ | S-56 | K-006, K-048 |

段階1 が終わった時点で、画面の録画を撮ってデモを公開できます（K-014）。

## 8. 決めたこと

| # | 決めたこと | 内容 | 理由 |
|---|---|---|---|
| 1 | API の作り | FastAPI に移す（決定） | 今の http.server ではルーティング・アップロード・逐次表示・ジョブを全部自前で書くことになる。FastAPI なら API の一覧（OpenAPI）が自動ででき、テストも書きやすい。依存は fastapi・uvicorn・python-multipart の3つ |
| 2 | 画面の作り | React＋TypeScript（Vite）で作り、組み立てた静的ファイルを同じサーバーから配る（決定） | 画面の数が多く、チャットの逐次表示や議事録の修正画面など動きのある画面がある。サーバーは1つのままで済む |
| 3 | クライアントアプリ | 同じ画面を Tauri で包む（決定） | Electron より小さく、OS の WebView を使う。画面を2つ作らずに済む。接続先は社内のサーバーだけにし、データは手元に置かない |
| 4 | 会話の状態の保存先 | SQLite（今の data/ と同じ場所）。複数台で動かすことになったら見直す | 1台で動かす前提に合う。サーバーを再起動しても会話が続く |
