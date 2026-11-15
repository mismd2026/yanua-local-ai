"""API の設定。"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

LOCAL_HOSTS = ("127.0.0.1", "localhost", "::1")


@dataclass
class Settings:
    db_path: Path = Path("data/index.db")
    # 会話の途中の状態（LangGraph のチェックポイント）。社内文書の保存先とは別のファイルにする
    chat_db_path: Path = Path("data/chat.db")
    helpdesk_data: Path = Path("demo/company/flows/helpdesk")
    roles_path: Path = Path("config/roles.json")
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
