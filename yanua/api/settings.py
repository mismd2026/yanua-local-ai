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
    ingest_roots: list[Path] = field(default_factory=lambda: [Path("demo/company")])
    job_workers: int = 1                 # 重いジョブを同時に流す数（GPU がないので既定は1）

    # 受付端末（K-043）
    kiosk_min_seconds: float = 0.6       # 受付の API はこの時間より早く返さない（コードが正しいかを時間の違いから分からせない）
    kiosk_max_failures: int = 10         # 1分にこの回数を超えてコードを間違えた端末は、しばらく照合しない
    visits_import_max_bytes: int = 2_000_000   # 来訪予定の取り込み（CSV）の大きさの上限
    today: date | None = None            # テスト用に「今日」を決める。None なら本当の今日
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
