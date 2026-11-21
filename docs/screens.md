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
- 端末トークンは `Authorization: Bearer ...` で送ります。`/api/kiosk/...` は端末トークン専用で、社内のログインのクッキーでは使えません（例外なし）。端末トークンでほかの API を呼ぶと拒否します。端末の登録は管理者の `/api/admin/kiosk-devices` です
- 入館票は、受付端末の画面が照合の返事（予定）から作って印刷します。サーバーには入館票を残しません
- コードを1分に10回を超えて間違えた端末は、しばらく照合しません。返事は間違えたときとまったく同じにし、止めているかどうかを返事から分からせません。受付端末の画面は、自分で数えた失敗の回数（1分に3回から）で「受付の者をお呼びください」の案内を出します
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
| S-10 | チャット | 社員 | 質問→出典つきの回答（進み具合を逐次表示）。出典を開く。ヘルプデスクの会話（本人確認〜問い合わせ票）も同じ画面 | `POST /api/ask/stream`、`POST /api/chat/sessions`、`POST /api/chat/sessions/{id}/messages`、`GET /api/documents/{doc_id}#section` | K-007, K-015 | 1 |
| S-11 | お知らせ | 社員 | 自分あて・部署あての一覧、既読にする | `GET /api/notifications`、`POST /api/notifications/read` | K-045, K-024 | 2 |
| S-12 | 来訪予定（自分の） | 社員 | 来訪予定の登録→コードと案内文を表示（コードはこのとき1回だけ）、一覧、取り消し | `POST /api/visits`、`GET /api/visits?host=me`、`DELETE /api/visits/{id}` | K-018 | 2 |
| S-13 | 議事録 | 社員・総務 | 録画のアップロード→進み具合→話し手・決まったこと・宿題の確認と修正→Word 出力。話し手が「不明」の発言をまとめて直す | `POST /api/uploads?kind=recording`（文字起こし済みの発言は `kind=segments`）、`POST /api/meetings`（ジョブ）、`GET /api/meetings`、`GET /api/meetings/{id}`、`PUT /api/meetings/{id}`、`GET /api/meetings/{id}/minutes.docx` | K-046, K-021 | 3 |

### 総務

| ID | 画面 | 役割 | 主な操作 | 呼ぶ API | 関連 | 段階 |
|---|---|---|---|---|---|---|
| S-20 | PDF の仕分け | 総務 | 受信箱の PDF→仕分けの予定（dry-run）を確認→ルールを選び直す→実行→記録 | `GET /api/paper/inbox`、`GET /api/paper/rules`、`POST /api/paper/plans`（ジョブ）、`GET /api/paper/plans/{id}`、`PUT /api/paper/plans/{id}/items/{index}`（ルールを選び直す）、`POST /api/paper/plans/{id}/execute`、`GET /api/paper/history` | K-047, K-020 | 3 |
| S-21 | PDF の変換 | 総務 | PDF を選ぶ→Word / Excel / 会社の様式に変換→ダウンロード | `POST /api/uploads?kind=pdf`、`POST /api/paper/convert`（ジョブ）、`GET /api/files/{file_id}` | K-020 | 3 |
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
| S-41 | 予定の表示と入館票 | 照合できた予定の表示、入館票の印刷、担当者へのお知らせ | `POST /api/kiosk/check-in`（入館票は画面が返事から作る） | K-043, K-018 | 2 |

- 受付端末の画面にはメニューもリンクも置かず、一定時間触られなければ最初の画面に戻ります
- 違う日・間違い・取り消しのコードは同じ返事にします（今の visits.py と同じ）。API も同じ形・同じ時間で返します（受付の API は、決まった時間（既定0.6秒）より早くは返しません）

### 管理者

| ID | 画面 | 役割 | 主な操作 | 呼ぶ API | 関連 | 段階 |
|---|---|---|---|---|---|---|
| S-50 | 文書（/admin/documents） | `documents.manage` を持つ人（ほかは 403。メニューにも出さない）。扱えるのは documents.manage の対象に含まれる部署（持ち主）の文書だけ | 文書ごとに「だれに見せるか」を決める（docs/access-model.md）。一覧：題名・持ち主の部署・公開先・要る権限・取り込んだ日。持ち主の部署・公開先で絞り込む。「取り込んだもの」「外したもの」。公開先（部署・グループ・全社から1つ以上）・見るのに要る権限（documents.view・infra.docs.view）・社外公開を、中身を変えずに直す（検索の単位は作り直さない）。外す（検索に出さない）・戻す。変える前に、見られるようになる人・見られなくなる人をグループごとの人数で出す（試しに当てるだけ）。取り込み（今のジョブ）：フォルダと、新しい文書の持ち主・公開先・要る権限（ファイルの先頭の書き方か、ここで選ぶ）。変更・取り込み・外す・戻すは監査ログに中身つき（本文は入れない）。前の版の管理は K-055 | `GET /api/admin/documents`（`?removed=true`）、`POST /api/admin/documents/preview`、`PATCH /api/admin/documents/{doc_id}`、`POST /api/admin/documents/{doc_id}/remove`・`/restore`、`GET /api/documents/ingest/sources`、`POST /api/documents/ingest` | K-010, K-002 | 4 |
| S-51 | 人事データ（/admin/hr） | 取り込み `hr.import`、一覧 `hr.view`（どちらもない人は 403。メニューにも出さない） | **取り込み**：ファイル（Excel・CSV・JSON）を上げる（作業用フォルダの決まり：上限 500MB・24時間で消す）→ 列の対応づけ（社員番号・氏名・よみ・部署名・部署コード・在籍状態・メールアドレスに、ファイルの列を割り当てる。上げたファイルの列名に合う保存した対応づけを選んでおく。名前を付けて保存すると次から選べる。保存は settingfiles.py を通し、版と監査ログが残る）→ 在籍状態の対応（ファイルに出てくる値を在籍・休職・退職に当てる。下書きは決まった言葉から）→ 取り込みの種類（**一部のファイル**：ファイルにいない人は変えない。既定／**全員のファイル**：ファイルにいない人はいなくなる人。いなくなる人が今の人数の 10% を超えるときは「いなくなる人が N人（全体の M%）います」と出して、もう一度押させる。種類は試しの結果・記録・監査ログに出す）。「使わない」にした任意の列（よみ・部署コード・メールアドレス・在籍状態）は、今いる人の値を残し、変わる人にも数えない（新しく入る人だけ空）→ **試す**（保存しない：新しく入る人・変わる人（部署・在籍状態・名前など）・いなくなる人の件数と先頭5件、新しく入る人・部署や在籍状態が変わる人・いなくなる人の入るグループ・外れるグループをグループごとの人数で。誤り（社員番号の重複・必要な列がない・空の欄・知らない在籍状態）は行番号つき。誤りがあるか、確定すると roles.assign を持つ人がいなくなるときは確定できない）→ **確定**（まとめて1回で入れ替える。途中で失敗したら前のまま。そのあと部署の一覧・権限の結果を作り直す。試したあとに対応づけかファイルを変えたら、もう一度試すまで確定できない）。読むのは割り当てた列だけ（住所・給与などは読まない）。**前の取り込み**：記録（日時・したこと・ファイル・した人・件数）と、直近3回分の控え。「1つ前に戻す」は、まず試しの結果（グループの数え方も取り込みと同じ）を出してから戻す（戻したことも、また戻せる）。記録の名前は「元のファイル名（戻した）」（何回戻しても1つだけ）。**一覧**：社員番号・氏名・部署・在籍状態・最後に変わった日。番号・名前の一部・部署で絞り込む（100人ずつ）。見られるのは hr.view の対象の部署の人だけ。人事データに誤りがあっても起動は止めず、画面の上に理由を出す（読めない対応づけ・まだ取り込んでいないなど）。休職中の人の扱いは S-52 の「人事」 | `GET /api/hr/status`、`POST /api/hr/inspect`、`PUT/DELETE /api/hr/mappings/{name}`、`POST /api/hr/preview`（ジョブ）、`POST /api/hr/imports`（ジョブ）、`POST /api/hr/restore/preview`・`POST /api/hr/restore`（ジョブ）、`GET /api/hr/employees?q=&dept=&offset=` | K-016, K-054 | 4 |
| S-52 | 設定 | 管理者（`settings.view`・`settings.edit`。ほかは 403） | 設定ごとに1つの区切り：今使っているもの（既定／画面で保存したもの）→編集→確かめる（誤りは欄の下）→保存。版の一覧と「この版に戻す」「既定に戻す」（ルールの画面と同じ形）。再起動待ちの設定は画面の上にまとめる。起動の指定が勝つ項目は変えられない。本人確認は言い方を1行試せる（保存しない）。版の「したこと」と監査ログの説明に、変えた項目を短く書く（例「型 標準→社員コード必須」。決まった項目だけ）。仕分けルールは S-20 へのリンクだけ | `GET /api/admin/settings`、`GET/PUT /api/admin/settings/{key}`、`POST .../{key}/validate`、`POST .../{key}/history/{版}/restore`、`POST .../{key}/reset`、`POST /api/admin/settings/identity/try` | K-042, K-009 | 4 |
| S-53 | 通信とモデル | 管理者（`network.view`・`network.edit`。ほかは 403） | 許可する宛先（既定は ON/OFF だけ・追加は足す・消す・ON/OFF。新しい既定・外れた既定の知らせ）、更新の確認の予定（しない・手動だけ・毎日・毎週。ON の宛先だけ見に行き、知らせるだけ）、モデルの目録（名前・種類・用途・固定した版・記録したハッシュ／digest・状態）、照合（通信しない）、入れ替え（押したときだけ。入手→入手元のハッシュと照合→記録を更新）。版・既定に戻す | `GET/PUT /api/admin/network`、`POST .../validate`、`POST .../history/{版}/restore`、`POST .../reset`、`POST /api/admin/network/check`、`GET /api/admin/models`、`POST /api/admin/models/verify`、`POST /api/admin/models/{name}/replace`（ジョブ） | K-052, K-011, K-023 | 4 |
| S-54 | 権限（/admin/access） | 見る `roles.view`、変える `roles.assign`（どちらもない人は 403。メニューにも出さない） | 考え方は docs/access-model.md（役割 × 対象 × グループ）。4つのタブ：**グループ**（一覧：名前・入り方（決まり／手）・メンバー数・与えている役割 × 対象。中身：手で入れたメンバーを社員番号か名前の一部で探して足す・外す（期限は日付）、決まり（全員・部署）で入るグループは決まりの中身と人数を出し、出し入れのボタンは出さない、役割 × 対象（か対象のまとまり）を足す・外す。作る・説明を直す。メンバーも役割もないグループだけ消せる）、**役割**（一覧と中身。permissions.py の一覧を機能ごとのチェックで出す（名前と「できること」）。作る。どのグループにも与えていない役割だけ消せる）、**対象**（部署・グループ・機能は人事データ・グループ・permissions.py から自動でそろえ、手では作らない。システム・サーバーは手で作る：名前・説明・つながる宛先（S-53 の宛先から）・接続情報の参照名（環境変数の名前だけ。値はどこにも出さない）。使っていないものだけ消せる。対象のまとまりを作る・中身を直す）、**この人は何ができるか**（社員番号か名前で探し、権限ごとに、どのグループのどの役割から来たか・対象・期限。システムアカウントも選べる。受付端末は、移し替えまでは端末トークンで /api/kiosk/ だけを使えることを出す）。役割を与えるときの対象の選び肢は「グループ：人事」「機能：人事」のように種類を付けて出す。**変える前の確かめ**：試しに当てるだけの API で「だれのどの権限が増える・減るか」を出してから、もう一度押して保存（200人より多くに効くときはグループごと）。自分の権限が減るときは「外すと、あなた自身では戻せません。ほかの roles.assign を持つ人に頼むことになります」と出して、もう一度押させる。断られる変更（自分の権限が増える・決まりのグループに手で入れる・最後の roles.assign 保持者を外す）は、理由を欄の近くに出す。画面専用の抜け道はない（変更はすべて下の API）。戻せなくなったときは、サーバーで `python -m yanua access`（README）。受付端末の登録・取り消しと端末トークンの移し替えは次の PR 以降。SSO のグループとの対応は K-008 | `GET /api/admin/access`、`GET /api/admin/access/people?q=`、`GET /api/admin/access/explain?q=`、`POST /api/admin/access/preview`、`POST /api/admin/access/groups`、`PATCH/DELETE /api/admin/access/groups/{id}`、`POST/DELETE /api/admin/access/groups/{id}/members`、`POST /api/admin/access/groups/{id}/grants`、`DELETE /api/admin/access/grants/{id}`、`POST /api/admin/access/roles`、`PUT/DELETE /api/admin/access/roles/{id}`、`POST /api/admin/access/targets`、`PATCH/DELETE /api/admin/access/targets/{id}`、`POST /api/admin/access/target-sets`、`PUT/DELETE /api/admin/access/target-sets/{id}`、`POST/DELETE /api/admin/kiosk-devices`（受付端末。画面は次の PR 以降） | K-049 | 4 |
| S-55 | 監査ログ | 管理者（`audit.view`） | 誰が・いつ・何をしたかを、期間・した人（社員番号・名前の一部・端末）・操作の種類（`auth` のような頭か、名前そのもの）・結果（成功・失敗・拒否）で絞り込む。新しい順・ページ送り。見たことも記録する。消す・書き換える操作はない。CSV の書き出しはまだ作らない（K-042 と一緒に考える） | `GET /api/audit` | K-009 | 4 |
| S-56 | 評価 | 管理者 | 質問セットでの採点の実行、モデルごとの比較 | `POST /api/eval/runs`（ジョブ）、`GET /api/eval/runs` | K-005, K-006 | 5 |

### S-52 で扱う設定の種類

今あるもの（K-042 の最初の PR）。どれも管理者だけ。既定は `config/` のファイル、画面で保存した写しは `data/settings/<key>.json`。どれも再起動なしで効く（保存したものと動いている値が違うとき、例えば写しを手で直したときは「再起動すると使われます」と出す）。

| key | 中身 | 保存の前の検証 | 起動の指定が勝つ項目 |
|---|---|---|---|
| `identity` | 本人確認の方針（既定 `config/identity.json`）：型・最初の聞き方・使う手がかり・確定する組み合わせ・姓だけを数えるか・聞き返しの上限 | `identity.policy_problems`（項目ごとに全部）。誤りがあると起動しない決まりはそのまま（画面からは検証を通ったものしか保存できない） | — |
| `audit` | 監査ログ（既定 `config/audit.json`）：保存期間（1〜3650日）・質問の本文を残すか（on にするときは画面の中で確かめる） | 範囲・型 | `--audit-keep-days`・`--audit-body` |
| `models` | モデル（既定 `config/models.json`）：チャット（`chat`）・議事録の要約（`summary`） | 目録（`config/network.json` の models、`kind: ollama`）にあり、その用途（`use`）で、登録した digest と手元の Ollama の digest が同じか（目録にないもの・埋め込み用・入っていない・登録していない・digest が違うものは断る。Ollama に問い合わせられないときは保存しない） | `--model`・`--summary-model` |
| `files` | 作業用フォルダ（既定 `config/files.json`）：合計の上限（1〜10000GB）・ファイルを消すまでの時間（1〜720時間） | 範囲・単位 | — |
| `hr` | 休職中の人（既定 `config/hr.json`。K-054）：ログインできる・できない（`on_leave_login`）、権限「そのまま」か「社員の役割だけ」（`on_leave_permissions`）。既定は今のまま（ログインできる・そのまま） | 型と選び肢 | — |

これから足す予定のもの（前の計画）：

| 種類（kind） | 今のファイル | 編集できる役割 | 保存の前の検証 |
|---|---|---|---|
| `paper-rules` | 仕分けルール（JSON） | 総務・管理者 | `paper/rules.py` の読み込み。正規表現が読めるか、項目名が使えるか |
| `paper-templates` | 会社の様式（Word・Excel）と差し込み位置 | 総務・管理者 | `paper/form.py` で見本を差し込んでみる |
| `reception-routes` | routes.csv | 総務・管理者 | 列がそろっているか、引き継ぎ先の部署があるか |
| `helpdesk-kb` | kb.csv | 情シス・管理者 | 列がそろっているか、手順が空でないか |
| `helpdesk-outages` | outages.csv | 情シス・管理者 | 日時が読めるか |
| `meeting-tools` | 会議ツールごとの名前の位置・枠の色 | 総務・管理者 | 値の範囲。デモ動画で読み取りを試せるようにする |
| `meeting-glossary` | 用語集 | 総務・管理者 | 1行に1語 |
| `network` | config/network.json | 管理者だけ | `netpolicy.py` の load_policy。宛先の書き方 |
| `models`（続き） | 文字起こしのモデル | 管理者だけ | ハッシュが記録と合うか |

## 5. API の一覧

今の CLI のコマンドとの対応です。「ジョブ」の欄が○のものは、すぐには終わらないのでジョブとして受け付けます（「6. 共通の決まり」）。

| API | CLI | 役割 | ジョブ |
|---|---|---|---|
| `POST /api/ask`、`POST /api/ask/stream`（進み具合を逐次表示） | `ask` | 社員 | |
| `GET /api/search` | `search` | 社員 | |
| `POST /api/chat/sessions`、`.../messages` | `helpdesk` | 社員 | |
| `GET /api/documents`（見られる文書と、扱える部署の文書。documents.manage が全社に効く人はすべてと置き場所）、`GET /api/documents/{doc_id}`（見られる文書だけ。ほかは 404） | `docs` | 社員（`documents.view`） | |
| `GET /api/documents/ingest/sources`、`POST /api/documents/ingest`（`source`・`mode`：`file`（ファイルの先頭の owner・access）か `chosen`（`owner`・`targets`・`required`・`external`。新しく足す文書だけ）。扱えない部署の文書は取り込まず、消しもしない） | `ingest` | `documents.manage` | ○ |
| `GET /api/admin/documents`（扱える部署の文書・公開先に選べるもの・要る権限に選べるもの・扱える部署）、`POST /api/admin/documents/preview`（`doc_id`・`action`：`publish`（`targets`・`required`・`external`）・`remove`・`restore`。見られるようになる人・見られなくなる人の数とグループごとの人数。保存しない）、`PATCH /api/admin/documents/{doc_id}`（`targets`・`required`・`external`。本文は作り直さない）、`POST /api/admin/documents/{doc_id}/remove`・`/restore` | （新規） | `documents.manage`（持ち主の部署に効くこと。ほかの部署の文書は 404） | |
| `GET /api/hr/status`、`POST /api/hr/inspect`（列名・シート・下書き・在籍状態の列の値）、`PUT/DELETE /api/hr/mappings/{name}`（列の対応づけ。名前を付けて保存）、`POST /api/hr/preview`（試し。保存しない。`scope`：`partial`（既定）か `all`）、`POST /api/hr/imports`（確定。`scope`、いなくなる人が多いときは `confirm_removal: true` が要る）、`POST /api/hr/restore/preview`・`POST /api/hr/restore`（1つ前に戻す。全員のファイルと同じ扱い）、`GET /api/hr/employees`（hr.view の対象の部署の人だけ） | `hr-mapping-draft`、`hr-import`（同じ処理を通る。監査ログには「サーバーの操作」で残す） | `hr.import`（一覧は `hr.view`） | preview・imports・restore は○ |
| `POST /api/kiosk/sessions`、`.../messages`、`POST /api/kiosk/check-in` | `reception` | 受付端末 | |
| `GET /api/reception/records` | （新規） | 総務 | |
| `POST/GET/DELETE /api/visits`、`POST /api/visits/import` | `visit-create`、`visit-list`、`visit-import` | 社員（自分の）、総務（全社） | |
| `GET /api/notifications`、`POST /api/notifications/read` | `notifications` | 社員 | |
| `GET /api/tickets`、`PATCH /api/tickets/{id}` | （新規） | 情シス | |
| `GET /api/paper/inbox`・`rules`、`POST /api/paper/plans`、`PUT .../items/{index}`、`.../execute`、`GET /api/paper/history` | `pdf-sort`（`--dry-run` が plans） | 総務 | plans は○ |
| `POST /api/paper/convert` | `pdf-convert` | 総務 | ○ |
| `POST /api/paper/rule-drafts`（予定の1件か受信箱の PDF から）、`POST /api/paper/rules/validate`、`POST /api/paper/rules`（足して保存）、`GET /api/paper/rules/status`、`GET /api/paper/rules/history`、`GET .../history/{版}`、`POST .../history/{版}/restore`、`POST /api/paper/rules/reset`（すでに既定なら `{"version": null, "already_default": true, "message": "すでに既定です"}`。版も監査ログも増やさない） | `pdf-rule-draft`（`--add-to` が保存） | 総務（`paper.rules`） | rule-drafts は○ |
| `POST /api/paper/text` | `pdf-text` | 総務 | ○ |
| `POST /api/meetings`、`GET /api/meetings`、`GET/PUT /api/meetings/{id}`、`GET .../minutes.docx` | `meeting` | 社員・総務（自分のものだけ） | ○ |
| `POST /api/uploads`、`GET /api/files/{file_id}` | （新規） | 種類による（PDF は総務、録画は社員）。開けるのは作った本人だけ | |
| `GET/PUT /api/admin/network`（宛先・知らせ・更新の確認・版・前の確認の結果）、`POST .../validate`、`POST .../history/{版}/restore`、`POST .../reset`、`POST /api/admin/network/check`（ON の宛先だけ見に行く。「しない」なら 409） | （新規） | 管理者（`network.view`・`network.edit`） | |
| `GET /api/admin/models`（目録と状態）、`POST /api/admin/models/verify`（通信しない）、`POST /api/admin/models/{name}/replace`（更新の確認で見つけた新しい版に入れ替える） | `models list / verify`（入手は `models pull`） | 管理者 | replace は○ |
| `GET /api/admin/settings`（4つの区切り・`pending_restart`・リンク）、`GET/PUT /api/admin/settings/{key}`（値・状態・起動の指定が勝つ項目・版・選び肢）、`POST .../{key}/validate`、`POST .../{key}/history/{版}/restore`、`POST .../{key}/reset`（すでに既定なら版を増やさない）、`POST /api/admin/settings/identity/try`（保存前の方針で言い方を試す。特定できたか・一致した手がかりの種類・聞き返す項目だけを返す） | （新規。前はファイルを直接編集） | 管理者（見るのは `settings.view`、変えるのは `settings.edit`） | |
| `GET /api/admin/access`（権限の一覧・役割（与えているグループ）・グループ（入り方・人数・メンバー・割り当て）・対象（自動か・使っているか）・対象のまとまり・アカウント・部署・S-53 の宛先）。開くたびに、部署・グループ・機能の対象を自動でそろえる。`GET /api/admin/access/people?q=`（社員番号の頭か名前の一部。在籍している人とシステムアカウント。20人まで）、`GET /api/admin/access/explain?q=`（画面の欄と同じ q。返すのは `candidates` と、1人に決まったときの `result`：権限ごとの出どころ（グループ・役割・対象・期限）と `note`（受付端末は「今は端末トークンで /api/kiosk/ だけを使えます（この仕組みへの移し替えはまだ）」）。q がなくても 400 にしない） | （新規） | `roles.view` | |
| `POST /api/admin/access/preview`（`{"op": "member.add", "args": {...}}`。args は保存の API と同じ中身にパスの番号を足したもの。試しに当てるだけで、保存も監査ログもしない。返すのは `detail`（変える中身）・`refused`（断られる理由）・`self`（自分の増える・減る権限）・`self_loss`・`people`（権限が変わる人と増える・減る権限）・`too_many` と `groups`（200人より多くに効くとき、グループごと）） | （新規） | `roles.assign` | |
| メンバー：`POST /api/admin/access/groups/{id}/members`（`account_id`・`expires_at`）、`DELETE .../members/{account}`。割り当て：`POST /api/admin/access/groups/{id}/grants`（`role_id` と `target_id` か `set_id`）、`DELETE /api/admin/access/grants/{id}`。グループ：`POST /api/admin/access/groups`（`name`・`description`・`rule`：なし／`{"all": true}`／`{"departments": [...]}`）、`PATCH .../groups/{id}`（`description`）、`DELETE .../groups/{id}`（メンバーも役割もないときだけ）。役割：`POST /api/admin/access/roles`、`PUT .../roles/{id}`（`permissions`・`description`）、`DELETE .../roles/{id}`（どこにも与えていないときだけ）。対象：`POST /api/admin/access/targets`（`kind` は system か server・`name`・`description`・`host`（S-53 の宛先）・`secret_ref`（環境変数の名前））、`PATCH/DELETE .../targets/{id}`。対象のまとまり：`POST /api/admin/access/target-sets`（`name`・`members`）、`PUT/DELETE .../target-sets/{id}` | `access add-member`・`access add-grant`（復旧用。サーバーに入れる人だけ） | `roles.assign`（触れるグループに効くこと。自分の権限が増える・締め出し・決まりで入るグループの手での出し入れ・使っているものを消すのは 409。理由を日本語で返し、監査ログに「拒否」） | |
| `GET /api/audit`（`since`・`until`：YYYY-MM-DD か ISO の時刻、`actor`、`action`、`result`、`page`、`per_page`（200まで））。返すのは `entries`・`total`・`actions`（記録にある操作の名前）・`keep_days`・`keep_body`。消す・書き換える API はない | （新規） | 管理者（`audit.view`。ほかは 403） | |
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
- 逐次表示で送るのは**進み具合だけ**です（「社内の文書を探しています」「回答を作っています」「回答を確かめています」）。LLM の文は、ルールで確かめ終わってから（答えられないときの判定・出典の付け直しのあと）まとめて送ります。確かめる前の文は画面に出しません（2026-11-16 決定）

### ジョブ（重い処理）

- 文字起こし・OCR・仕分けの予定・モデルの入手は、受け付けたらすぐ `202` とジョブの番号を返します
- `GET /api/jobs/{id}` で、状態（待ち・実行中・完了・失敗・取り消し）、進み具合（%と今の段階）、結果を返します
- GPU がないので、重いジョブは同時に1つずつ実行します（待ち行列）。同時に流す数は設定で変えられるようにします
- 結果のファイル（議事録の Word、変換した Excel など）は、サーバーに一定期間だけ置いて、期限が来たら消します

### ファイルの受け渡し

- アップロードしたファイル（録画・PDF）は、サーバーの作業用フォルダに置きます。大きさの上限を決めます（録画は例として 2GB）
- ダウンロードは、その人が見てよいものだけ。リンクを他の人に渡しても開けないよう、毎回セッションで確かめます
- 段階3で決めたこと（2026-11-16）：
  - アップロードは `POST /api/uploads?kind=pdf|recording|segments&name=...` に、本文にファイルをそのまま送ります（multipart は使わない）。受け取りながら大きさを数え、上限（PDF 50MB・録画 2GB・発言の JSON 5MB）を超えたらその場で止めて消します。PDF は中身の先頭（`%PDF-`）も確かめます
  - 置き場所の名前はサーバーが決めます。送られた名前は表示にだけ使い、置き場所のパスは画面にもジョブの内容にも出しません
  - アップロードしたファイルも結果のファイルも、既定で24時間（`file_keep_hours`）で消します（起動したとき・アップロードのとき・10分ごと）
  - 作業用フォルダの合計にも上限（`work_max_bytes`、既定 20GB）を決めます。置いているファイルと受け取り中のファイルの合計が上限を超えるアップロードは、`507`（`work_full`）で断り、受け取りかけたファイルを消します。ジョブの結果（変換したファイル）は小さいので、この上限では止めません
  - 仕分けの予定・アップロード・変換の結果は作った人だけが見られます。仕分けの記録（`GET /api/paper/history`）は、保存先が総務で共通なので、総務の全員が見られます
  - ジョブが失敗したときに画面に出す文は、利用者に見せてよい文だけにします（`JobFailed` の文。例：「文字起こしのモデルが準備されていません。管理者に連絡してください」）。直すためのコマンドや、例外の中身・サーバーの置き場所は、サーバーのログにだけ残します。思わぬ失敗は「処理の途中で問題が起きました。続くときは、管理者に連絡してください（ジョブ番号 …）」とし、ジョブ番号でログを探せるようにします
  - 仕分けの予定の表では、名前に使う項目が読み取れずに「不明」になる行と、置き先が同じ行に注意を出します。実行のときに同じ名前のファイルがあれば上書きせず、`_2`・`_3` … を付けて分けます（名前は、なければ作る1回の操作で押さえるので、同時に実行してもぶつかりません）。番号を付けて置いたものは実行の結果に出します
  - 仕分けルールの下書き（K-047）：S-20 の予定の行（未分類のものなど）か、受信箱で選んだ1件から、`pdf-rule-draft` と同じ処理（`draft_rule`）をジョブで動かします。予定の行からなら、予定を作ったときの文字を使います（OCR し直さない）。下書きは画面で直し、保存の前にルールの読み込みと同じ検証（`parse_rules`。名前の重なり・言葉の形・正規表現のかっこ・ファイル名と保存先で使える項目・優先度）をかけます。誤りはすべて位置つきで返し、正しいものだけを保存します。保存したルールは、その場で予定を作り直したとき・選び直したときに使われます。画面からは様式（templates）は書けません
  - ルールの置き場所と版：既定の `rules.json`（リポジトリ）は書き換えず、画面から保存したルール一式は `data/settings/rules.json` に全部の写しとして書きます。読むときは写しがあれば写しだけ、なければ既定を使います。様式のファイルは既定のフォルダから探します。版の記録も `data/settings/history/rules.json/` に残し、前の版に戻せます（戻すのも新しい版。戻す版も今の検証にかけます）。写しを直接直していたら、その中身も「（ファイル）」の版として先に残します。`POST /api/paper/rules/reset` で写しを消して既定に戻せます。写しのもとになった既定がそのあと変わったら、`GET /api/paper/rules/status` の `default_changed` で、仕分けの画面に「既定のルールが更新されています」と知らせます。起動のときに今使うルールを検証し、読み込めなければ位置つきで出します（起動は止めない）
  - この仕組みは共通の部品 `yanua/settingfiles.py`（`SettingFile`：既定を読む・画面から保存した写しを書く・版を残して戻す・既定の更新を知らせる・保存の前に検証）にしてあり、段階4の設定の画面（S-52、K-042）で、本人確認の方針・routes.csv・kb.csv なども同じ部品で直せるようにします
  - 開けるのは、そのファイルを作った人で、しかもその種類の権限（PDF は総務、議事録は社員）を今も持っている人だけ。ほかの人・期限切れ・ないものは同じ 404 にします
  - 仕分けの予定・議事録も、作った人だけが見られます（総務・管理者でも、ほかの人のものは見えない）
  - 仕分けの予定には取り出した文字も残し、ルールを選び直すときは PDF を読み直しません（OCR をし直さない）。実行のときは、予定を作ったあとにファイルが変わっていないかを確かめ、変わっていたら置きません
  - 議事録の Word は、出力するたびに今の内容から作ります（ファイルとして残さない）
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

入力の誤りが項目ごとに分かるときは、`details` に位置つきで並べます（例：仕分けルールの保存）。画面は `where` の項目の横に `message` を出します。

```json
{"error": {"code": "invalid_rule", "message": "ルールに直すところがあります",
           "details": [{"where": "fields.invoice_no", "message": "正規表現に、かっこ（取り出す部分）がありません"}]}}
```

仕分けルールの「確かめる」（`POST /api/paper/rules/validate`）は、誤りがなければ `{"ok": true, "warnings": [...]}` を返します。`sample_text`（下書きのもとの PDF の文字。下書きのジョブの結果の `text`）を渡すと、ファイル名・保存先に、その PDF から読み取れない項目を使っているところを `warnings` に同じ形（`where`・`message`）で並べます。誤りではないので、保存はできます。

予定を作ったあとに、使ったルールがなくなった（版に戻した・既定に戻した）ときは、`.../execute` は何も置かずに `409 rules_gone` を返します（`details` の `where` はファイル名）。画面は予定の作り直しを促します。

`message` はそのまま画面に出せる日本語にします。中の詳しい理由（どの権限が足りないかなど）は監査ログにだけ残し、受付端末には出しません。

### 監査ログに残すもの（K-009）

置き場所は `data/audit.db`（SQLite。`serve --audit-db` で変えられる）。1件の形：時刻・した人（社員番号と名前。受付端末なら端末番号と端末名。ログイン前は「なし」、サーバー自身は「システム」）・操作の名前・対象・結果（成功・失敗・拒否）・接続元の IP・短い説明。

| 操作の名前 | いつ | 対象 |
|---|---|---|
| `ask.chat`・`ask.helpdesk` | 質問（チャット・ヘルプデスク）。説明に出典の文書の番号と題名、答えたか断ったか（文書が見つからない＝失敗・権限がない＝拒否）、部署の権限で見せなかった文書の件数（題名は残さない）。断ったときに見せなかった文書が1件以上あれば、見られる文書に弱く当たっていても「権限がない」。ヘルプデスクが社内文書を使わず決まった手順で案内したときは「手順で案内」＝成功 | 出典の文書の番号 |
| `auth.login`・`auth.logout` | ログイン（開発用ログインを含む）・ログアウト・ログインの失敗（結果「失敗」、した人は「なし」） | 社員番号 |
| `access.denied` | 役割の確かめで断った（403）。説明に断った操作の名前（例 `audit.view`） | メソッドとパス |
| `api.bearer`・`api.csrf` | 入口で断った（端末トークンで受付以外の API を呼んだ・`X-Yanua-Client` がない） | メソッドとパス |
| `job.start`・`job.done`・`job.failed`・`job.cancel`・`job.interrupted` | ジョブの開始・終わり・失敗・取り消し・再起動での中断（文書の取り込み・議事録・仕分けの予定・変換・ルールの下書き） | ジョブ番号 |
| `kiosk.register`・`kiosk.revoke`・`kiosk.locked` | 受付端末の登録・取り消し、コードを続けて間違えた端末の照合を止めた | 端末番号 |
| `paper.rules.save`・`.restore`・`.reset` | 設定の保存・版に戻す・既定に戻す（`yanua/settingfiles.py` を通るものはすべて。成功も失敗も） | 設定の名前 |
| `documents.ingest`・`documents.publish`・`documents.remove`・`documents.restore` | 文書の取り込み（足した・取り込み直した・消した文書の番号と、新しい文書の公開先）・公開先と要る権限を直した（例「就業規則（抜粋）の公開先 全社→経理部」）・外した・戻した。本文は残さない | 文書の番号・フォルダ |
| `paper.execute` | PDF の仕分けの実行（何件・移したか写したか・置いた先のフォルダ）。消えたルールで止めたときは「失敗」 | 予定の番号 |
| `file.download`・`meeting.download` | ファイル（変換の結果など）・議事録の Word のダウンロード | ファイル番号・議事録の番号 |
| `audit.view` | 監査ログを見た（どんな条件で見たか） | |
| `audit.purge` | 保存期間を過ぎた記録を消した（何件、いつより前） | |

残さないもの：パスワード・端末トークン・セッションのトークン・受付端末で入力されたコードは、どの設定でも残さない。質問の文、文書や PDF の中身は、既定では残さない（質問の文は `--audit-body` のときだけ `body` に残す）。部署の権限で見せなかった文書があることは、質問した人への返事には出さない。仕分けの実行は、書類の中身から作るファイル名を残さず、フォルダだけを残す。

通信とモデル（S-53）：`settings.network.init`（初めての起動で既定の宛先を ON にした。システム。版は増やさない）・`settings.network.save`・`.restore`・`.reset`（ほかの設定とそろえた名前。説明は「huggingface.co を OFF」など。PR #23 のときに `network.save` などで残った記録は、そのまま）・`network.check`（宛先・結果・版）・`models.verify`・`models.replace`（前の版→新しい版）。

人事データ（S-51）：`hr.import`（確定。対象は上げたときのファイル名。説明は「一部のファイル：入る 0人・変わる 1人（部署 1）・いなくなる 0人。社員番号：10004」のように件数と社員番号だけ。氏名・メールアドレスは入れない。誤りで確定しなかったときは「失敗」）・`hr.restore`（1つ前に戻した。説明は同じ形）・`hr.mappings.save`（列の対応づけを保存した・消した）・`roles.member.remove`（退職した人・人事データにいなくなった人を、手で入れたグループから外した。対象は社員番号）。試し（preview）は残さない。コマンド（`hr-import`）で取り込んだときは、した人の種類を「サーバーの操作」で残す。

役割と権限（S-54）：`roles.migrate`（初めての起動で roles.json から作った。システム）・`roles.member.add`・`.remove`・`roles.grant.add`・`.remove`・`roles.role.create`・`.update`・`.delete`・`roles.group.create`・`.update`・`.delete`・`roles.target.create`・`.update`・`.delete`・`roles.set.create`・`.update`・`.delete`（説明は「総務 に 山田 太郎 を追加（期限 2026-12-31）」「役割「人事」の権限：hr.import を外した」のように中身つき。断ったときは「拒否」）。試しに当てただけ（preview）は残さない。復旧用のコマンド（`python -m yanua access add-member`・`add-grant`）で変えたときは、同じ操作の名前で、した人の種類を「サーバーの操作」、名前に OS ユーザーを残す（記録できなければ変えない）。

これから足すもの：文書の変更（K-010）、SSO のログイン（K-008）。

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
