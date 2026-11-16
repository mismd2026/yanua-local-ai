"""API の設定。"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from pathlib import Path

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")
WEB_DIST = Path(__file__).resolve().parents[2] / "web" / "dist"


@dataclass
class Settings:
    db_path: Path = Path("data/index.db")
    # 会話の途中の状態（LangGraph のチェックポイント）。社内文書の保存先とは別のファイルにする
    chat_db_path: Path = Path("data/chat.db")
    helpdesk_data: Path = Path("demo/company/flows/helpdesk")
    reception_data: Path = Path("demo/company/flows/reception")   # 受付の引き継ぎ先（routes.csv）
    roles_path: Path = Path("config/roles.json")
    identity_path: Path = Path("config/identity.json")   # 本人確認の方針
    top_k: int = 5

    # 待ち受けるアドレス。開発用ログインは、このマシンの中（127.0.0.1）で待ち受けるときだけ使える
    host: str = "127.0.0.1"
    # 開発用ログイン（SSO なしで社員を選んでログインする）。本番では使わない（K-008 で SSO に替える）
    dev_login: bool = False

    session_idle_minutes: int = 480      # 操作がないままこの時間が過ぎたらログアウト
    session_max_hours: int = 12          # ログインからこの時間が過ぎたらログアウト
    chat_idle_minutes: int = 30          # 返事がないままこの時間が過ぎた会話は消す

    # 文書の取り込み（管理者のジョブ）で指定できるフォルダ。ここの下にあるフォルダだけを取り込む
    # 既定はデモの社内文書と社外公開の文書だけ（人事データ・会話のデータ・紙の書類のフォルダは文書として取り込まない）
    ingest_roots: list[Path] = field(default_factory=lambda: [Path("demo/company/docs"), Path("demo/company/public")])
    job_workers: int = 1                 # 重いジョブを同時に流す数（GPU がないので既定は1）

    # 受付端末（K-043）
    kiosk_min_seconds: float = 0.6       # 受付の API はこの時間より早く返さない（コードが正しいかを時間の違いから分からせない）
    kiosk_max_failures: int = 10         # 1分にこの回数を超えてコードを間違えた端末は、しばらく照合しない
    visits_import_max_bytes: int = 2_000_000   # 来訪予定の取り込み（CSV）の大きさの上限
    today: date | None = None            # テスト用に「今日」を決める。None なら本当の今日

    # 段階3：ファイルの受け渡し（K-046・K-047）
    work_dir: Path = Path("data/work")           # アップロードと結果のファイルを置く作業用フォルダ
    file_keep_hours: int = 24                    # アップロード・結果のファイルを残す時間。過ぎたら消す
    pdf_max_bytes: int = 50_000_000              # PDF のアップロードの上限（50MB）
    recording_max_bytes: int = 2_000_000_000     # 録画・録音のアップロードの上限（2GB）
    segments_max_bytes: int = 5_000_000          # 文字起こし済みの発言（JSON）の上限
    work_max_bytes: int = 20_000_000_000         # 作業用フォルダの合計の上限（20GB）。超えるアップロードは断る
    # PDF の仕分け・変換
    paper_inbox: Path = Path("demo/company/paper/inbox")     # 受信箱（スキャナーが PDF を置くフォルダ）
    paper_out: Path = Path("data/paper/sorted")              # 仕分けの保存先（_仕分け記録.csv もここ）
    paper_rules: Path = Path("demo/company/paper/rules.json")  # 仕分けルール（会社の様式の場所もここに書く）
    # 議事録
    meeting_template: Path = Path("demo/company/meeting/templates/議事録.docx")   # 議事録の様式（Word）
    whisper_model: str = "whisper-small"         # 文字起こしのモデル（models pull で入手したもの）
    models_dir: Path = Path("data/models")
    # 組み立てた Web画面（cd web && npm run build）。なければ API だけを動かす
    web_dist: Path | None = WEB_DIST

    @property
    def local_only(self) -> bool:
        return self.host in LOCAL_HOSTS

    @property
    def cookie_secure(self) -> bool:
        # 開発用（このマシンの中の http）以外では、クッキーを HTTPS のときだけ送らせる
        return not (self.dev_login and self.local_only)

    def check(self) -> None:
        if self.dev_login and not self.local_only:
            raise ValueError(
                f"開発用ログインは、127.0.0.1 で待ち受けるときだけ使えます（今の設定: {self.host}）。"
                "社内に公開するときは SSO（K-008）を設定してください"
            )
