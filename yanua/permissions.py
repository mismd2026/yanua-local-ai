"""権限の一覧（docs/access-model.md「権限の一覧（案）」）。権限はコードで決め、画面からは増やせない。

役割（権限の束）・グループ・対象・メンバーはデータとして持つ（yanua/access.py）。
"""

from __future__ import annotations

from dataclasses import dataclass

# 効く対象の種類
FEATURE, DEPARTMENT, GROUP, SYSTEM, SERVER = "feature", "department", "group", "system", "server"
TARGET_KINDS = {FEATURE: "機能", DEPARTMENT: "部署", GROUP: "グループ", SYSTEM: "システム", SERVER: "サーバー"}


@dataclass(frozen=True)
class Permission:
    name: str
    feature: str        # 機能（画面の区切り）
    does: str           # できること
    targets: tuple[str, ...]   # 効く対象の種類


def _p(name, feature, does, *targets) -> Permission:
    return Permission(name, feature, does, targets)


PERMISSIONS: dict[str, Permission] = {p.name: p for p in [
    _p("ask", "チャット", "社内文書に質問する", FEATURE),
    _p("search", "文書", "文書を検索する", FEATURE),
    _p("documents.view", "文書", "文書を見る・回答の出典に使う（前の documents.list）", DEPARTMENT, GROUP),
    _p("documents.manage", "文書", "文書を取り込む・公開先と要る権限を決める（前の documents.ingest）", DEPARTMENT, GROUP),
    _p("helpdesk", "ヘルプデスク", "相談する", FEATURE),
    _p("helpdesk.handle", "ヘルプデスク", "受けた票を見て対応する（前の tickets）", DEPARTMENT, GROUP),
    _p("notifications", "お知らせ", "お知らせを見る", FEATURE),
    _p("jobs.own", "ジョブ", "自分のジョブを見る・取り消す", FEATURE),
    _p("jobs.all", "ジョブ", "ほかの人のジョブを見る・取り消す", DEPARTMENT, GROUP),
    _p("meetings", "議事録", "議事録を作る・自分のものを直す", FEATURE),
    _p("visits.own", "来訪", "自分の来訪予定を登録する", FEATURE),
    _p("visits.all", "来訪", "全社の来訪予定を見る・取り込む", DEPARTMENT),
    _p("reception.records", "受付", "受付の記録を見る", FEATURE),
    _p("kiosk.devices", "受付", "受付端末を登録・取り消す", SERVER),
    _p("paper", "PDF の仕分け", "予定を作る・実行する・変換する", FEATURE),
    _p("paper.rules", "PDF の仕分け", "ルールを直す", FEATURE),
    _p("hr.view", "人事", "人事データを見る（S-51）", DEPARTMENT),
    _p("hr.import", "人事", "人事データを取り込む", FEATURE),
    _p("settings.view", "設定", "設定を見る（S-52）", FEATURE),
    _p("settings.edit", "設定", "設定を変える（S-52）", FEATURE),
    _p("network.view", "通信", "通信先とモデルを見る（S-53）", FEATURE),
    _p("network.edit", "通信", "通信先とモデルを変える（S-53）", FEATURE),
    _p("audit.view", "監査", "監査ログを見る（S-55）", FEATURE),
    _p("roles.view", "権限", "グループ・役割・対象を見る（S-54）", FEATURE),
    _p("roles.assign", "権限", "グループの役割とメンバーを変える", GROUP),
    # IaC（将来）。作っておくだけで、どのグループにも与えない
    _p("infra.docs.view", "IaC（将来）", "構成書を見る", SYSTEM),
    _p("infra.scan", "IaC（将来）", "構成を読み取る", SYSTEM),
    _p("infra.plan", "IaC（将来）", "変更の計画（plan）を作る", SYSTEM),
    _p("infra.approve", "IaC（将来）", "変更を承認する", SYSTEM),
    _p("infra.apply", "IaC（将来）", "変更を実行する", SYSTEM),
    _p("infra.targets", "IaC（将来）", "接続する対象を登録する", FEATURE),
]}

# 文書を見るのに要る権限として選べるもの（S-50）。既定は documents.view
DOCUMENT_PERMISSIONS = ("documents.view", "infra.docs.view")

# 移し替えで名前を変えた権限（前の名前 → 新しい名前）
RENAMED = {"documents.list": "documents.view", "documents.ingest": "documents.manage", "tickets": "helpdesk.handle"}

# 既定の役割（docs/access-model.md「役割と既定のグループ」）。初めての起動のとき、DB が空なら作る
DEFAULT_ROLES: dict[str, tuple[str, list[str]]] = {
    "社員": ("全員が持つ", ["ask", "search", "documents.view", "helpdesk", "notifications", "jobs.own", "meetings", "visits.own"]),
    "総務": ("PDF の仕分け・来訪・受付", ["paper", "paper.rules", "visits.all", "reception.records"]),
    # 表では jobs.all も入るが、今の「情シス」はほかの人のジョブを見られないので、動きを変えないために入れていない（PR の説明）
    "ヘルプデスク担当": ("問い合わせ票の対応（前の「情シス」）", ["helpdesk.handle"]),
    "人事": ("人事データ", ["hr.view", "hr.import"]),
    "文書管理": ("文書の取り込み・公開先", ["documents.manage"]),
    "管理者": ("設定・通信・受付端末・権限", ["settings.view", "settings.edit", "network.view", "network.edit", "kiosk.devices",
                                         "roles.view", "roles.assign", "jobs.all"]),
    "監査": ("監査ログ（前は管理者に含まれていた）", ["audit.view", "roles.view"]),
    # IaC（将来）：依頼・承認・実行を別の役割に分ける。どのグループにも与えない
    "インフラ技術者": ("構成の読み取りと変更の計画（将来）", ["infra.docs.view", "infra.scan", "infra.plan"]),
    "インフラ承認者": ("変更の承認（将来）", ["infra.docs.view", "infra.approve"]),
    "インフラ実行者": ("変更の実行（将来）", ["infra.docs.view", "infra.apply"]),
    "インフラ管理": ("接続する対象の登録（将来）", ["infra.docs.view", "infra.targets"]),
}


def unknown(names) -> list[str]:
    return [n for n in names if n not in PERMISSIONS]
