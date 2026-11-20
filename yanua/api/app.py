"""API の本体。画面の一覧と API の一覧は docs/screens.md。

段階1（K-041）で作ったもの：
    GET    /health
    POST   /api/auth/dev-login     開発用ログイン（--dev-login で起動したときだけ）
    GET    /api/auth/login         SSO へ（K-008 までは「未設定」を返す）
    POST   /api/auth/logout
    GET    /api/me                 ログイン中の人・役割・できる操作
    POST   /api/ask                出典つきの回答（部署はログインした人のもの）
    POST   /api/ask/stream         同じ回答を Server-Sent Events で。途中は進み具合だけを送り、文は確かめてから送る
    GET    /api/search             部署の権限つきの検索
    GET    /api/documents          見られる文書の一覧
    GET    /api/documents/{doc_id} 文書の中身（出典を開く。見られる文書だけ）
    POST   /api/documents/ingest   文書の取り込み（管理者・ジョブ）
    GET    /api/documents/ingest/sources  取り込めるフォルダの一覧（管理者。設定の ingest_roots とその直下）
    POST   /api/chat/sessions                  ヘルプデスクの会話を始める
    POST   /api/chat/sessions/{id}/messages    返事を送る
    DELETE /api/chat/sessions/{id}             会話をやめる
    GET    /api/jobs, GET/DELETE /api/jobs/{id}
    GET    /, /chat など           Web画面（web/ を組み立てた web/dist。組み立てていなければ配らない）

段階3（K-046・K-047）：
    POST   /api/uploads?kind=pdf|recording|segments&name=...      アップロード（本文にファイルをそのまま。大きさの上限つき）
    GET    /api/files/{file_id}                                   ダウンロード（作った本人で、その種類の権限がある人だけ）
    GET    /api/paper/inbox, GET /api/paper/rules                 受信箱の PDF、仕分けルール（総務）
    POST   /api/paper/plans（ジョブ）, GET /api/paper/plans/{id}   仕分けの予定
    PUT    /api/paper/plans/{id}/items/{index}                    ルールを選び直す（PDF は読み直さない）
    POST   /api/paper/plans/{id}/execute, GET /api/paper/history  実行、仕分けの記録
    POST   /api/paper/convert（ジョブ）                            PDF を Word・Excel・会社の様式に変換
    POST   /api/meetings（ジョブ）, GET /api/meetings, GET/PUT /api/meetings/{id}   議事録（作った人だけ）
    GET    /api/meetings/{id}/minutes.docx                        議事録の Word

段階4（K-009）：
    GET    /api/audit                                             監査ログ（管理者。絞り込み・ページ送り。見たことも記録する）

段階2（K-045・K-018・K-043）：
    GET    /api/notifications, POST /api/notifications/read      自分あて・自分の部署あてのお知らせ
    POST   /api/visits, GET /api/visits, DELETE /api/visits/{id}  来訪予定（社員は自分の。総務は全社）
    POST   /api/visits/import                                     来訪予定の取り込み（総務。CSV を本文で送る）
    GET    /api/reception/records, POST .../{id}/done             受付の記録（総務）
    GET    /api/tickets, GET/PATCH /api/tickets/{id}              問い合わせ票（情シス）
    GET/POST /api/admin/kiosk-devices, DELETE .../{id}            受付端末の登録（管理者。社内のログインで使う）
    POST   /api/kiosk/sessions, .../{id}/messages                 受付の会話（受付端末のトークンだけ）
    POST   /api/kiosk/check-in                                    コードの照合と受付（受付端末のトークンだけ）

画面から呼ぶ前提の決まり：
- 状態を変えるリクエスト（GET 以外）には、ヘッダー X-Yanua-Client を付ける。
  別のサイトのページからは、このヘッダーを付けて送れない（CSRF 対策）
- 権限はすべてこの API の側で確かめる（画面で隠すだけにしない）
- 受付端末は Authorization: Bearer <端末トークン> で呼ぶ。/api/kiosk/... は端末トークン専用（例外なし）。
  端末トークンでほかの API を呼ぶと拒否する。/api/kiosk/... は社内のログインのクッキーを見ない
"""

from __future__ import annotations

import asyncio
import json
import logging
import queue
import sqlite3
import tempfile
import threading
import time
from datetime import date, datetime

Date = date   # 引数名 date（?date=...）に隠されないための別名
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, Response as RawResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field, ValidationError

from .. import audit, hr, hrimport, models, visits
from ..answer import ANSWERED, NO_PERMISSION, NOT_FOUND, Answer, answer_question
from ..llm import ChatModel, LLMError, OllamaChat
from ..search import search
from ..store import Store
from . import adminsettings, auth, desk, errors, files, kiosk, meetings, netsettings, paperjobs
from .auth import User
from .chat import ChatDB, HelpdeskSessions, ReceptionSessions
from .errors import ApiError, forbidden, not_found, not_logged_in
from ..paper.extract import OCRUnavailable
from ..settingfiles import SettingError
from .jobs import FINISHED, JobContext, JobFailed, JobQueue
from ..netpolicy import NetworkBlocked
from .. import identity
from ..identity import load_policy
from .. import access
from ..permissions import DOCUMENT_PERMISSIONS, PERMISSIONS
from ..store import DocScope, DocSettings
from .roles import load_roles
from .settings import Settings

CSRF_HEADER = "X-Yanua-Client"
MAX_QUESTION = 2000
STREAM_PING_SEC = 10     # 回答を作っている間も、途中の機器に接続を切られないよう空の行を送る

log = logging.getLogger("yanua.api")

# 逐次表示で画面に出す進み具合（answer_question の段階 → 画面の文言）
# 監査ログに書くジョブの種類の名前
JOB_NAMES = {"ingest": "文書の取り込み", "paper-plan": "仕分けの予定", "paper-convert": "PDF の変換", "meeting": "議事録",
             "paper-rule-draft": "仕分けルールの下書き", "model-replace": "モデルの入れ替え"}


def audit_bound(value: str | None, end: bool) -> str | None:
    """監査ログの期間の絞り込み。YYYY-MM-DD はサーバーの地域の日付（end なら、その日の終わりまで）、それ以外は ISO の時刻。"""
    from datetime import datetime, timedelta

    if not value:
        return None
    try:
        if len(value) == 10:
            t = datetime.fromisoformat(value).astimezone()
            return audit.iso(t + timedelta(days=1) if end else t)
        t = datetime.fromisoformat(value)
        return audit.iso(t if t.tzinfo else t.astimezone())
    except ValueError as e:
        raise ApiError(400, "invalid_date", "期間は YYYY-MM-DD か、ISO 8601 の時刻で指定してください") from e


ASK_STAGES = {
    "search": "社内の文書を探しています",
    "answer": "回答を作っています",
    "check": "回答を確かめています",
}


def sse(event: str, data: dict) -> str:
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


# ------------------------------------------------------------ リクエストの形


class DevLogin(BaseModel):
    employee_no: str = Field(min_length=1, max_length=32)


class Ask(BaseModel):
    question: str = Field(min_length=1, max_length=MAX_QUESTION)


class Ingest(BaseModel):
    source: str = Field(min_length=1, max_length=500)
    # 新しく足す文書の持ち主の部署・公開先・要る権限。mode が file ならファイルの先頭（access・owner）を使う
    mode: str = Field(default="file", pattern="^(file|chosen)$")
    owner: str = Field(default="", max_length=100)
    targets: list[str] = Field(default_factory=list, max_length=100)
    required: str = Field(default="documents.view", max_length=64)
    external: bool = False


class DocPublication(BaseModel):
    targets: list[str] = Field(max_length=100)                     # 公開先：special:all・dept:部署・group:グループ
    required: str = Field(default="documents.view", max_length=64)  # 見るのに要る権限
    external: bool = False                                         # 社外公開（社外向けの受付で使う）


class DocChange(BaseModel):
    doc_id: str = Field(min_length=1, max_length=100)
    action: str = Field(default="publish", max_length=20)          # publish・remove・restore
    targets: list[str] | None = Field(default=None, max_length=100)
    required: str | None = Field(default=None, max_length=64)
    external: bool = False


class ChatStart(BaseModel):
    flow: str = "helpdesk"
    # 開発用ログインのときだけ使える：代表アカウント（eigyo-common など）からの問い合わせを試す
    account: str | None = Field(default=None, max_length=64)


class ChatMessage(BaseModel):
    text: str = Field(min_length=1, max_length=MAX_QUESTION)


class ReadNotifications(BaseModel):
    ids: list[int] = Field(default_factory=list, max_length=1000)
    all: bool = False


class VisitCreate(BaseModel):
    date: str = Field(min_length=10, max_length=10)
    time: str = Field(min_length=1, max_length=20)
    room: str = Field(min_length=1, max_length=100)
    visitor_company: str = Field(min_length=1, max_length=100)
    visitor_name: str = Field(min_length=1, max_length=100)
    purpose: str = Field(default="", max_length=500)
    host_contact: str = Field(default="", max_length=100)
    host_employee_no: str | None = Field(default=None, max_length=32)   # 総務がほかの人の予定を登録するときだけ


class TicketUpdate(BaseModel):
    status: str | None = None
    note: str | None = Field(default=None, max_length=2000)


class DeviceCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class PaperPlanCreate(BaseModel):
    files: list[str] = Field(default_factory=list, max_length=500)   # 空なら受信箱のすべて
    use_llm: bool = False                                           # どのルールにも当たらないとき、LLM（社内）に選ばせる


class PaperRechoose(BaseModel):
    rule: str | None = Field(default=None, max_length=100)          # 空ならルールで自動に決め直す


class PaperExecute(BaseModel):
    move: bool = False                                              # 既定は受信箱に残す（コピー）


class PaperRuleDraft(BaseModel):
    plan_id: str | None = Field(default=None, max_length=64)        # 予定の1件から（予定を作ったときの文字を使う）
    index: int | None = Field(default=None, ge=0)
    inbox: str | None = Field(default=None, max_length=255)         # 受信箱の PDF から（読み直す。スキャンは OCR）
    name: str | None = Field(default=None, max_length=60)           # 書類の種類の名前（なければ書類から推測）
    use_llm: bool = False                                           # 見分ける言葉を、社内の LLM にも挙げさせる


class MemberAdd(BaseModel):
    account_id: str = Field(min_length=1, max_length=64)            # 社員番号か sys:<名前>
    expires_at: str | None = Field(default=None, max_length=10)     # YYYY-MM-DD（その日の終わりまで）。空なら期限なし


class GrantAdd(BaseModel):
    role_id: str = Field(min_length=1, max_length=64)
    target_id: str | None = Field(default=None, max_length=128)
    set_id: str | None = Field(default=None, max_length=128)


class RoleUpdate(BaseModel):
    permissions: list[str] = Field(max_length=200)
    description: str | None = Field(default=None, max_length=200)


class RoleCreate(BaseModel):
    name: str = Field(max_length=100)
    description: str = Field(default="", max_length=200)
    permissions: list[str] = Field(default_factory=list, max_length=200)


class GroupCreate(BaseModel):
    name: str = Field(max_length=100)
    description: str = Field(default="", max_length=200)
    rule: dict | None = None                                        # None：手で入れる。{"all": true}・{"departments": [...]}：決まり


class GroupUpdate(BaseModel):
    description: str = Field(default="", max_length=200)


class TargetCreate(BaseModel):
    kind: str = Field(max_length=20)                                # system / server（部署・グループ・機能は自動）
    name: str = Field(max_length=100)
    description: str = Field(default="", max_length=200)
    host: str = Field(default="", max_length=255)                   # つながる宛先（S-53 の宛先から）
    secret_ref: str = Field(default="", max_length=64)              # 接続情報の参照名（環境変数の名前だけ。値は持たない）


class TargetUpdate(BaseModel):
    description: str = Field(default="", max_length=200)
    host: str = Field(default="", max_length=255)
    secret_ref: str = Field(default="", max_length=64)


class SetBody(BaseModel):
    name: str = Field(default="", max_length=100)
    members: list[str] = Field(max_length=500)


class AccessPreview(BaseModel):
    op: str = Field(max_length=40)                                  # member.add など（監査ログの roles.<op>）
    args: dict = Field(default_factory=dict)


class SettingValues(BaseModel):
    values: dict                                                    # 設定の画面（S-52）の値


class IdentityTry(BaseModel):
    values: dict                                                    # 保存前の本人確認の方針
    said: str = Field(min_length=1, max_length=300)                 # 試す言い方（1行）


class PaperRuleSave(BaseModel):
    rule: dict                                                      # 足すルール（下書きを直したもの）
    sample_text: str | None = Field(default=None, max_length=200_000)  # 下書きのもとの PDF の文字（読み取れない項目の注意に使う）


class PaperConvert(BaseModel):
    file_id: str | None = Field(default=None, max_length=64)        # アップロードした PDF
    inbox: str | None = Field(default=None, max_length=255)         # 受信箱の PDF
    to: str = Field(pattern="^(docx|xlsx)$")
    form: bool = False                                              # 会社の様式に差し込む（仕分けルールの様式）
    rule: str | None = Field(default=None, max_length=100)          # 様式を使うルールを指定する


class MeetingCreate(BaseModel):
    upload_id: str = Field(min_length=1, max_length=64)             # 録画・録音
    segments_upload_id: str | None = Field(default=None, max_length=64)   # 文字起こし済みの発言（JSON。任意）
    title: str = Field(min_length=1, max_length=200)
    date: str | None = Field(default=None, max_length=10)
    place: str = Field(default="", max_length=200)
    tool: str = Field(default="zoom", pattern="^(zoom|teams|meet|none)$")
    participants: list[str] = Field(default_factory=list, max_length=100)
    summary: bool = True


UPLOAD_KINDS = {
    # 種類 → (要る操作, 上限の設定の名前, 受け付ける拡張子)
    "pdf": ("paper", "pdf_max_bytes", (".pdf",)),
    "recording": ("meetings", "recording_max_bytes", (".mp4", ".m4a", ".mp3", ".wav", ".webm", ".mov", ".mkv", ".aac")),
    "segments": ("meetings", "segments_max_bytes", (".json",)),
    "hr": ("hr.import", "hr_max_bytes", (".csv", ".xlsx", ".xlsm", ".json")),   # 人事データ（S-51）
}


class HrInspect(BaseModel):          # S-51
    file_id: str = Field(min_length=1, max_length=64)
    sheet: str | None = Field(default=None, max_length=100)
    status_column: str | None = Field(default=None, max_length=200)


class HrMapping(BaseModel):
    columns: dict[str, str] = Field(max_length=20)
    status_values: dict[str, str] | None = Field(default=None, max_length=200)
    sheet: str | None = Field(default=None, max_length=100)


class HrRun(BaseModel):
    file_id: str = Field(min_length=1, max_length=64)
    mapping: HrMapping


class CheckIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)


VISIT_CSV_COLUMNS = ("date", "time", "company", "visitor", "host_employee_no", "room")


# ------------------------------------------------------------ 本体


def create_app(settings: Settings, llm: ChatModel, transcriber_factory=None, summary_llm: ChatModel | None = None,
               model_catalog=None, model_downloader=None, clock=None, ollama_tags=None) -> FastAPI:
    """summary_llm は議事録の要約に使うモデル。渡さないとき、llm が Ollama なら settings.summary_model を同じ Ollama で使う。"""
    if summary_llm is None:
        summary_llm = OllamaChat(settings.summary_model, llm.base_url) if isinstance(llm, OllamaChat) else llm
    """transcriber_factory：文字起こしを作る関数（テストでは偽のものを渡す）。既定は社内の faster-whisper。
    model_catalog：設定の画面で選べるモデルの一覧を返す関数（テストでは偽のものを渡す）。既定は、目録（config/network.json）・
    登録した digest（data/models/models.lock.json）・手元の Ollama の digest から作る（yanua/models.py の catalog）。
    model_downloader：モデルの入れ替え（S-53）で使う入手の関数（テスト用。既定は Hugging Face のライブラリ）。
    clock：今の時刻を返す関数（更新の確認の予定のテスト用）。
    ollama_tags：手元の Ollama のモデル → digest を返す関数（テスト用。既定は手元の Ollama に聞く）。"""
    # 設定の画面（S-52）で保存した写しがあれば使う（起動の指定が勝つ項目は除く）。誤りのある写しは使わず既定で動く
    loaded, bad_settings = adminsettings.startup(settings)
    for key, found in bad_settings.items():
        log.error("設定「%s」の写しが読み込めないので、既定で動きます。直すところ：%s", adminsettings.LABELS[key],
                  "／".join(f"{w}: {m}" if w else m for w, m in found))
    if "models" in loaded:
        llm.name = settings.chat_model
        if summary_llm is not llm:
            summary_llm.name = settings.summary_model
    if model_catalog is None:
        def model_catalog() -> list[dict]:
            policy = net_policy()
            if policy is None:
                raise models.ModelError(net_view()["error"])
            return models.catalog(policy, settings.models_dir, local_tags())
    settings.check()
    roles = load_roles(settings.roles_path)
    # 本人確認の方針（画面で保存した写しがあれば写し）。誤りがあれば起動しない。設定の画面で保存すると入れ替わる
    policy_state = {"policy": load_policy(adminsettings.identity_path(settings))}
    local = threading.local()

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(str(settings.db_path), timeout=10)

    # 権限（役割 × 対象 × グループ。docs/access-model.md）。DB が空なら roles.json から作る（移し替え）。
    # 権限の設定に誤りがあれば起動しない（権限と本人確認は止める、の決まり）
    with closing(connect()) as db:
        access_migrated = access.migrate_if_empty(db, roles)
        access_upgraded = access.upgrade(db)   # 前の版のデータを今の版に合わせる（社員の対象 → メンバー自身の所属）
        access.sync_targets(db)          # 部署・グループ・機能の対象をそろえる
        found = access.problems(access.load(db))
    if found:
        raise access.AccessError("権限の設定に誤りがあるので起動しません：" + "／".join(f"{w}: {m}" for w, m in found), found)
    engine = access.Engine(connect, leave_policy=lambda: settings.on_leave_permissions)

    def can(user: User, permission: str, target: tuple[str, str] | None = None) -> bool:
        """判定の1か所（yanua/access.py の Engine.can）。target が None なら、その機能を使えるか。"""
        return engine.can(user.employee_no, user.dept, permission, target).allowed

    def doc_scope(user: User):
        """文書を見てよい範囲（DocScope。一覧・開く・検索・質問・ヘルプデスクの検索で、LLM に渡す前に絞る）。
        決まりは yanua/access.py（見るのに要る権限を、公開先を含む対象で持っているときだけ）。"""
        return engine.doc_scope(user.employee_no, user.dept)

    def manage_scope(user: User):
        """文書を扱ってよい持ち主の部署（documents.manage の対象）。全社なら None。"""
        depts = engine.departments(user.employee_no, user.dept, "documents.manage")
        return None if depts == access.ALL else depts

    def name_of(employee_no: str) -> str | None:
        with closing(connect()) as db:
            return (hr.lookup(db, employee_no) or {}).get("name")

    # 監査ログ（K-009）。記録に失敗しても、元の操作は止めない（Auditor.record が例外を出さない）
    auditor = audit.Auditor(settings.audit_path, settings.audit_keep_days, settings.audit_keep_body, names=name_of)

    if access_migrated:
        auditor.record("roles.migrate", actor=audit.SYSTEM, actor_name=audit.SYSTEM, kind=audit.SYSTEM, ip="",
                       detail="初めての起動：roles.json から役割・グループ・対象を作った（docs/access-model.md）")
    for done in access_upgraded:
        auditor.record("roles.migrate", actor=audit.SYSTEM, actor_name=audit.SYSTEM, kind=audit.SYSTEM, ip="", detail=done)

    def audit_by(user: User, action: str, **kw) -> None:
        auditor.record(action, actor=user.employee_no, actor_name=user.name, **kw)

    ASK_SCREENS = {"chat": "チャット", "helpdesk": "ヘルプデスク"}
    ASK_RESULTS = {ANSWERED: audit.OK, NOT_FOUND: audit.FAILED, NO_PERMISSION: audit.DENIED}

    def audit_question(user: User, screen: str, question: str, ans: Answer | None, ip: str | None = None,
                       procedure: str | None = None) -> None:
        """質問の記録：どの画面で・出典の文書の番号と題名・答えたか断ったか。部署の権限で見せなかった文書は件数だけ。
        procedure は、ヘルプデスクが社内文書を使わず決まった手順で案内したときの手順の名前（「手順で案内」＝成功）。
        質問の文は、本文を残す設定（--audit-body）のときだけ body に入る。"""
        if procedure:
            detail = f"{ASK_SCREENS[screen]}：手順で案内しました（手順：{procedure}）"
            if ans and ans.hidden:
                detail += f"。部署の権限で見せなかった文書 {ans.hidden}件"
            auditor.record(f"ask.{screen}", actor=user.employee_no, actor_name=user.name, result=audit.OK,
                           detail=detail, body=question, ip=ip)
            return
        if ans.refused:
            detail = f"{ASK_SCREENS[screen]}：断りました（{ans.reason or NOT_FOUND}）"
        else:
            sources = "、".join(f"{src['doc_id']}「{src['title']}」" for src in ans.sources)
            detail = f"{ASK_SCREENS[screen]}：答えました（出典：{sources}）"
        if ans.hidden:
            detail += f"。部署の権限で見せなかった文書 {ans.hidden}件"
        auditor.record(f"ask.{screen}", actor=user.employee_no, actor_name=user.name,
                       target="、".join(src["doc_id"] for src in ans.sources), result=ASK_RESULTS.get(ans.reason, audit.OK),
                       detail=detail, body=question, ip=ip)

    # ------------------------------------------------ 通信の設定（S-53。K-052）

    def net_defaults() -> tuple[dict | None, str]:
        return netsettings.load_defaults(settings.network_config)

    def net_store():
        defaults, _ = net_defaults()
        return netsettings.store(settings, (defaults or {}).get("allowed_hosts", []), audit=audit_setting)

    def net_state() -> dict:
        try:
            return netsettings.read_state(net_store())
        except (OSError, ValueError, KeyError, AttributeError) as e:
            # 写しを手で壊したときなど：既定の状態で動く（既定の宛先はすべて ON・追加なし・手動だけ）
            log.error("通信の設定の状態を読めません（既定で動きます）：%s", e)
            return netsettings.read_state_text(Path(settings.network_state_config).read_text(encoding="utf-8"))

    def known_file() -> Path:
        return settings.settings_dir / netsettings.KNOWN_NAME

    def read_known() -> list[str] | None:
        try:
            data = json.loads(known_file().read_text(encoding="utf-8"))
            return [h for h in data.get("hosts", []) if isinstance(h, str)]
        except (OSError, ValueError, AttributeError):
            return None

    def write_known(hosts_: list[str]) -> None:
        """覚えた既定の宛先を書く（版にはしない）。このあと増えた既定は OFF で加わる。"""
        settings.settings_dir.mkdir(parents=True, exist_ok=True)
        tmp = known_file().with_suffix(".tmp")
        tmp.write_text(json.dumps({"_説明": "覚えた既定の宛先（S-53）。これにない既定の宛先は、新しい既定として OFF で加わる",
                                   "hosts": list(hosts_)}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        tmp.replace(known_file())

    def net_policy():
        """動いている許可リスト（ON の宛先だけ）。network.json が読めなければ None（社外にはどこにも通信しない）。"""
        defaults, _ = net_defaults()
        return netsettings.effective(defaults, net_state(), read_known())

    def net_view() -> dict:
        defaults, error = net_defaults()
        return {**netsettings.view(defaults, net_state(), read_known()), "error": error}

    def local_tags() -> dict[str, str]:
        if ollama_tags is not None:
            return ollama_tags()
        base = llm.base_url if isinstance(llm, OllamaChat) else models.DEFAULT_OLLAMA_URL
        return models.ollama_tags(base)

    def audit_setting(action: str, by: str, result: str, detail: str) -> None:
        auditor.record(action, actor=by, target=action.rsplit(".", 1)[0], result=result, detail=detail)

    JOB_EVENTS = {"start": ("job.start", audit.OK, "始めました"), "done": ("job.done", audit.OK, "終わりました"),
                  "failed": ("job.failed", audit.FAILED, "失敗しました"), "cancelled": ("job.cancel", audit.OK, "取り消しました"),
                  "interrupted": ("job.interrupted", audit.FAILED, "サーバーの再起動で中断しました")}

    def job_event(event: str, job: dict) -> None:
        action, result, what = JOB_EVENTS[event]
        # 何のジョブかと、どうなったかだけを残す（params の中身や結果は残さない）
        detail = f"{JOB_NAMES.get(job['kind'], job['kind'])}：{what}"
        if event == "failed" and job.get("error"):
            detail += f"（{job['error']}）"
        auditor.record(action, actor=job["owner"], target=job["job_id"], result=result, detail=detail)

    jobs = JobQueue(settings.db_path, settings.job_workers, listener=job_event)

    def store() -> Store:
        # SQLite の接続はスレッドをまたいで使えないので、スレッドごとに開く
        if not hasattr(local, "store"):
            local.store = Store(settings.db_path)
        return local.store

    with closing(connect()) as db:
        auth.ensure_schema(db)
        kiosk.ensure_schema(db)
        desk.ensure_schema(db)
        visits.ensure_schema(db)
        files.ensure_schema(db)
        paperjobs.ensure_schema(db)
        meetings.ensure_schema(db)
        files.cleanup(db, settings.work_dir)
    limiter = kiosk.FailureLimiter(settings.kiosk_max_failures)
    def rule_store():
        # 仕分けルール（既定のファイルと、画面から保存した写し）。設定を変えたらすぐ使えるよう、使うたびに作る（ロックは置き場所ごとに共通）
        return paperjobs.rule_store(settings, audit=audit_setting)

    def today() -> date:
        return settings.today or date.today()

    # ------------------------------------------------ ジョブの種類

    def ingest_job(ctx: JobContext, params: dict) -> dict:
        ctx.progress(5, "取り込みの準備")
        chosen = DocSettings(**params["chosen"]) if params.get("chosen") else None
        owners = set(params["owners"]) if params.get("owners") is not None else None
        s = Store(settings.db_path)
        try:
            report = s.ingest(Path(params["source"]), chosen, owners)
        finally:
            s.close()
        if params.get("by"):
            # 取り込んだこと（どの文書を、どの公開先で）を監査ログに残す。中身（本文）は残さない
            r = report.as_dict()
            how = "ファイルの先頭の書き方" if chosen is None else (
                f"持ち主 {chosen.owner}、公開先 {targets_text(chosen.targets, target_names())}、要る権限 {chosen.required}"
                + ("、社外公開" if chosen.external else ""))
            counts = "、".join(f"{label} {len(r[k])}件" + (f"（{'・'.join(r[k][:20])}）" if r[k] else "")
                               for k, label in (("added", "足した"), ("updated", "取り込み直した"), ("deleted", "消した"), ("errors", "取り込めなかった")))
            auditor.record("documents.ingest", actor=params["by"], target=Path(params["source"]).name,
                           result=audit.FAILED if r["errors"] and not (r["added"] or r["updated"]) else audit.OK,
                           detail=f"{counts}（新しい文書の公開先：{how}）", ip="")
        ctx.progress(100, "完了")
        return report.as_dict()

    jobs.register("ingest", ingest_job)

    def whisper():
        from ..meeting.transcribe import WhisperTranscriber

        try:
            policy = net_policy()
            pinned = (policy.models.get(settings.whisper_model, {}).get("revision") or None) if policy else None
            return WhisperTranscriber(models.verify(settings.whisper_model, settings.models_dir, revision=pinned))
        except models.ModelError as e:
            # 社員の画面には連絡のお願いだけを出す。入手のコマンドと理由は、管理者がログで見る
            raise JobFailed("文字起こしのモデルが準備されていません。管理者に連絡してください",
                            f"python -m yanua models pull {settings.whisper_model} で入手してください（{e}）") from e

    def paper_plan_job(ctx: JobContext, params: dict) -> dict:
        with closing(connect()) as db:
            return paperjobs.make_plan(db, plan_id=ctx.job_id, owner=params["owner"], inbox=settings.paper_inbox,
                                       names=params["files"], rs=paper_rules(), llm=llm if params["use_llm"] else None,
                                       progress=ctx.progress)

    def job_file(owner: str, file_id: str, kind: str) -> dict:
        # ジョブの params にはファイルの番号だけを入れる（置き場所のパスは、ジョブの一覧で本人にも見せない）
        with closing(connect()) as db:
            f = files.get(db, file_id, owner, kind)
        if not f:
            raise JobFailed("アップロードしたファイルが見つかりません（期限が過ぎて消えた可能性があります）。もう一度アップロードしてください")
        return f

    def paper_convert_job(ctx: JobContext, params: dict) -> dict:
        if params.get("file_id"):
            src = Path(job_file(params["owner"], params["file_id"], "pdf")["path"])
        else:
            src = paperjobs.inbox_path(settings.paper_inbox, params["inbox"])
            if not src:
                raise JobFailed("受信箱のファイルが見つかりません（ほかの人が仕分けて移した可能性があります）")
        out_dir = settings.work_dir / "results" / ctx.job_id
        try:
            out, note = paperjobs.convert_pdf(src, out_dir, params["to"], paper_rules() if params["form"] else None,
                                              params.get("rule"), progress=ctx.progress)
        except OCRUnavailable as e:
            raise JobFailed(paperjobs.OCR_MISSING, str(e)) from e
        name = f"{Path(params['source_name']).stem}{''.join(out.suffixes[-1:])}"
        with closing(connect()) as db:
            f = files.add(db, file_id=files.new_id(), owner=params["owner"], kind="result", perm="paper", name=name,
                          path=out, keep_hours=settings.file_keep_hours, job_id=ctx.job_id)
        ctx.progress(100, "完了")
        return {"files": [f], "message": note}

    def meeting_job(ctx: JobContext, params: dict) -> dict:
        media = Path(job_file(params["owner"], params["upload_id"], "recording")["path"])
        seg = params.get("segments_upload_id")
        seg_path = Path(job_file(params["owner"], seg, "segments")["path"]) if seg else None
        with closing(connect()) as db:
            return meetings.process(db, params["meeting_id"], media=media, segments_file=seg_path,
                                    transcriber_factory=transcriber_factory or whisper, llm=summary_llm,
                                    use_summary=params["summary"], progress=ctx.progress, job_id=ctx.job_id)

    jobs.register("paper-plan", paper_plan_job)
    jobs.register("paper-convert", paper_convert_job)
    jobs.register("meeting", meeting_job)

    def paper_rule_draft_job(ctx: JobContext, params: dict) -> dict:
        # pdf-rule-draft と同じ処理（yanua/paper/rules.py の draft_rule）。予定の1件からなら、予定を作ったときの文字を使う
        from ..paper import rules as paper_rule_file
        from ..paper.extract import read_pdf

        ctx.progress(10, "PDF の文字を読んでいます")
        if params.get("plan_id"):
            with closing(connect()) as db:
                text = paperjobs.plan_text(db, params["plan_id"], params["index"])
            source = params["source"]
        else:
            src = paperjobs.inbox_path(settings.paper_inbox, params["inbox"])
            if not src:
                raise JobFailed("受信箱のファイルが見つかりません（ほかの人が仕分けて移した可能性があります）")
            try:
                text = read_pdf(src).text
            except OCRUnavailable as e:
                raise JobFailed(paperjobs.OCR_MISSING, str(e)) from e
            source = src.name
        ctx.progress(60, "下書きを作っています")
        try:
            self_names = paper_rules().self_names   # 自社の名前は相手先にしない
        except ApiError:
            self_names = []
        draft = paper_rule_file.draft_rule(text, params.get("name") or None, llm if params.get("use_llm") else None, self_names)
        ctx.progress(100, "完了")
        # text は「確かめる」で、ファイル名・保存先の項目がこの PDF から読み取れるかを見るのに使う
        return {"draft": draft, "source": source, "excerpt": paper_rule_file.norm(text)[:1500],
                "text": paper_rule_file.norm(text)[:200_000]}

    jobs.register("paper-rule-draft", paper_rule_draft_job)

    def paper_rules():
        from ..paper import rules as paper_rule_file

        try:
            # 画面から保存した写し（data/settings/rules.json）があればそれを、なければ既定のファイルを使う。
            # 様式のファイルは、既定のルールのファイルのフォルダから探す
            return paper_rule_file.parse_rules_text(rule_store().read_text(), settings.paper_rules.parent, rule_store().path().name)
        except (OSError, ValueError) as e:
            log.error("仕分けルール %s を読めません：%s", rule_store().path(), e)
            raise ApiError(500, "paper_rules", "仕分けルールを読めません。管理者に連絡してください") from e

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        path, problems = paperjobs.check_rules(settings)
        if problems:
            # 起動は止めない（ほかの機能は使える）。仕分け・変換の画面では「管理者に連絡してください」になる
            log.error("仕分けルール %s が読み込めません。直すところ：%s", path, "／".join(f"{w}: {m}" if w else m for w, m in problems))
        auditor.purge()      # 保存期間を過ぎた監査ログ（起動したときと、1日1回）
        init_network()
        jobs.start()
        chats = ChatDB(settings.chat_db_path, settings.chat_idle_minutes)
        app.state.helpdesk = HelpdeskSessions(chats, settings.db_path, settings.helpdesk_data, llm, settings.top_k, policy_state["policy"],
                                              on_answer=lambda u, q, a, proc: audit_question(u, "helpdesk", q, a, procedure=proc),
                                              scope_of=doc_scope)
        app.state.reception = ReceptionSessions(chats, settings.db_path, settings.reception_data, llm, settings.top_k,
                                                settings.today)

        last_purge = time.monotonic()

        async def sweep():
            # 期限の来たファイルを10分ごとに消す。監査ログの保存期間を過ぎたものは1日1回
            nonlocal last_purge
            while True:
                await asyncio.sleep(600)
                if time.monotonic() - last_purge >= 86400:
                    last_purge = time.monotonic()
                    await run_in_threadpool(auditor.purge)
                try:
                    def run():
                        with closing(connect()) as db:
                            files.cleanup(db, settings.work_dir)

                    await run_in_threadpool(run)
                except Exception:  # noqa: BLE001  消せなかったものは次の回に
                    log.exception("期限切れのファイルを消せませんでした")

        async def scheduled():
            # 更新の確認の予定（毎日・毎週）。1分ごとに、決まった時刻を過ぎたかを見る
            while True:
                await asyncio.sleep(60)
                try:
                    await run_in_threadpool(run_scheduled_check)
                except Exception:  # noqa: BLE001
                    log.exception("更新の確認（予定）に失敗しました")

        sweeper = asyncio.create_task(sweep())
        checker = asyncio.create_task(scheduled())
        yield
        sweeper.cancel()
        checker.cancel()
        jobs.stop()
        chats.close()

    app = FastAPI(title="Yanua API", version="0.1", lifespan=lifespan,
                  # API の一覧（OpenAPI）は開発用ログインのときだけ公開する
                  docs_url="/api/docs" if settings.dev_login else None,
                  redoc_url=None, openapi_url="/api/openapi.json" if settings.dev_login else None)
    app.state.settings = settings
    app.state.jobs = jobs
    app.state.audit = auditor
    errors.install(app)

    def openapi():
        # API の一覧（/api/docs）に X-Yanua-Client ヘッダーを書いておく。
        # 画面の「Authorize」で1回入れれば、試すときのすべてのリクエストに付く
        if app.openapi_schema:
            return app.openapi_schema
        from fastapi.openapi.utils import get_openapi

        schema = get_openapi(title=app.title, version=app.version, routes=app.routes)
        schema.setdefault("components", {})["securitySchemes"] = {
            "client": {"type": "apiKey", "in": "header", "name": CSRF_HEADER,
                       "description": "GET 以外のリクエストに必要（CSRF 対策）。値は何でもよい"}}
        schema["security"] = [{"client": []}]
        app.openapi_schema = schema
        return schema

    app.openapi = openapi

    @app.middleware("http")
    async def csrf(request: Request, call_next):
        path = request.url.path
        # 監査ログの接続元。プロキシのヘッダー（X-Forwarded-For）は、だれでも書けるので使わない
        audit.client_ip.set(request.client.host if request.client else "")
        # 端末トークン（Bearer）で使えるのは受付の API（/api/kiosk/...）だけ
        if path.startswith("/api/") and request.headers.get("authorization", "").lower().startswith("bearer ") \
                and not path.startswith("/api/kiosk/"):
            await run_in_threadpool(auditor.record, "api.bearer", target=f"{request.method} {path}", result=audit.DENIED,
                                    detail="端末トークンで、受付の API ではない API を呼ぼうとしました")
            return JSONResponse(errors.body("forbidden", "この操作をする権限がありません"), status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/") \
                and not request.headers.get(CSRF_HEADER):
            await run_in_threadpool(auditor.record, "api.csrf", target=f"{request.method} {path}", result=audit.DENIED,
                                    detail=f"{CSRF_HEADER} ヘッダーのないリクエストを断りました")
            return JSONResponse(errors.body("csrf", "画面からの操作として受け付けられません。画面を読み込み直してください"),
                                status_code=403)
        return await call_next(request)

    # ------------------------------------------------ ログイン中の人

    def current_user(request: Request) -> User:
        with closing(connect()) as db:
            user = auth.user_for_token(db, request.cookies.get(auth.COOKIE, ""), engine, settings.session_idle_minutes,
                                       settings.on_leave_login)
        if not user:
            raise not_logged_in()
        return user

    def deny(user: User, action: str, request: Request | None = None) -> ApiError:
        """権限が足りずに断る（403）。断った操作の名前を監査ログに残す。"""
        target = f"{request.method} {request.url.path}" if request is not None else ""
        audit_by(user, "access.denied", target=target, result=audit.DENIED,
                 detail=f"操作「{action}」の権限がありません（グループ：{'・'.join(user.roles)}）")
        return forbidden()

    def require(action: str):
        def check(request: Request, user: User = Depends(current_user)) -> User:
            if not can(user, action):
                raise deny(user, action, request)
            return user
        return check

    def current_device(request: Request) -> dict:
        # 受付の API は端末トークンだけを見る。社内のログインのクッキーがあっても使わない
        header = request.headers.get("authorization", "")
        token = header[7:].strip() if header.lower().startswith("bearer ") else ""
        with closing(connect()) as db:
            device = kiosk.device_for_token(db, token)
        if not device:
            raise ApiError(401, "kiosk_not_registered", "この端末は受付端末として登録されていません。管理者に問い合わせてください")
        return device

    async def padded(started: float) -> None:
        # 受付の API は、決まった時間より早くは返さない（コードが正しい・違う日・取り消し・間違いで、かかる時間をそろえる）。
        # 待つ間はスレッドを使わない（asyncio.sleep）。time.sleep だとスレッドをふさぎ、受付が重なるとほかの API が待たされる
        rest = settings.kiosk_min_seconds - (time.monotonic() - started)
        if rest > 0:
            await asyncio.sleep(rest)

    def set_cookie(response: Response, token: str) -> None:
        response.set_cookie(auth.COOKIE, token, httponly=True, secure=settings.cookie_secure, samesite="lax",
                            max_age=settings.session_max_hours * 3600, path="/")

    # ------------------------------------------------ 共通

    @app.get("/health")
    def health():
        return {"status": "ok", "model": llm.name, "documents": len(store().documents())}

    @app.post("/api/auth/dev-login")
    def dev_login(req: DevLogin, response: Response):
        if not settings.dev_login:
            auditor.record("auth.login", target=req.employee_no[:20], result=audit.FAILED,
                           detail="開発用ログインは使えない設定です")
            raise not_found()
        with closing(connect()) as db:
            person = hr.lookup(db, req.employee_no)
            if not person:
                # 入力された社員番号は対象に残す（パスワードはない。トークンは作っていない）
                auditor.record("auth.login", target=req.employee_no[:20], result=audit.FAILED,
                               detail="開発用ログイン：その社員番号ではログインできません")
                raise ApiError(401, "login_failed", "その社員番号ではログインできません")
            if person.get("status") == "休職" and not settings.on_leave_login:
                auditor.record("auth.login", target=req.employee_no[:20], result=audit.FAILED,
                               detail="開発用ログイン：休職中の人はログインできない設定です")
                raise ApiError(401, "login_failed", "その社員番号ではログインできません")
            token = auth.create_session(db, person["employee_no"], settings.session_max_hours)
        set_cookie(response, token)
        auditor.record("auth.login", actor=person["employee_no"], actor_name=person["name"], target=person["employee_no"],
                       detail="開発用ログイン")
        return {"employee_no": person["employee_no"], "name": person["name"]}

    @app.get("/api/auth/login")
    def login():
        if settings.dev_login:
            return {"method": "dev"}
        raise ApiError(501, "sso_not_configured", "SSO によるログインはまだ設定されていません（K-008）")

    @app.post("/api/auth/logout")
    def logout(request: Request, response: Response):
        token = request.cookies.get(auth.COOKIE, "")
        if token:
            with closing(connect()) as db:
                user = auth.user_for_token(db, token, engine, settings.session_idle_minutes)
                auth.delete_session(db, token)
            if user:
                audit_by(user, "auth.logout", target=user.employee_no)
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return {**user.as_dict(), "permissions": engine.permissions(user.employee_no, user.dept)}

    # ------------------------------------------------ 質問・検索・文書

    @app.post("/api/ask")
    def ask(req: Ask, user: User = Depends(require("ask"))):
        started = time.monotonic()
        try:
            result = answer_question(store(), llm, req.question, doc_scope(user), settings.top_k)
        except ValueError as e:
            raise ApiError(400, "invalid_question", str(e)) from e
        audit_question(user, "chat", req.question, result)
        return {**result.as_dict(), "elapsed_sec": round(time.monotonic() - started, 2)}

    @app.post("/api/ask/stream")
    def ask_stream(req: Ask, user: User = Depends(require("ask"))):
        # ログイン・権限・質問の形は、送り始める前に確かめる（ふつうのエラーの形で返す）
        events: queue.Queue[tuple[str, dict] | None] = queue.Queue()
        ip = audit.client_ip.get()     # 別のスレッドで答えるので、接続元をここで取っておく
        scope = doc_scope(user)        # 見てよい部署も、ここで決めておく

        def work():
            started = time.monotonic()
            s = Store(settings.db_path)
            try:
                result = answer_question(s, llm, req.question, scope, settings.top_k,
                                         progress=lambda st: events.put(("progress", {"stage": st, "message": ASK_STAGES[st]})))
                audit_question(user, "chat", req.question, result, ip=ip)
                events.put(("result", {**result.as_dict(), "elapsed_sec": round(time.monotonic() - started, 2)}))
            except ValueError as e:
                events.put(("error", errors.body("invalid_question", str(e))["error"]))
            except LLMError as e:
                log.error("LLM: %s", e)
                events.put(("error", errors.body("llm_unavailable",
                                                 "回答を作るAIに接続できません。しばらくしてからやり直してください")["error"]))
            except Exception:
                log.exception("想定していないエラー")
                events.put(("error", errors.body("internal", "サーバーでエラーが起きました")["error"]))
            finally:
                s.close()
                events.put(None)

        threading.Thread(target=work, name="yanua-ask", daemon=True).start()

        def stream():
            while True:
                try:
                    item = events.get(timeout=STREAM_PING_SEC)
                except queue.Empty:
                    yield ": ping\n\n"
                    continue
                if item is None:
                    return
                yield sse(*item)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    @app.get("/api/search")
    def search_docs(q: str, top_k: int = 5, user: User = Depends(require("search"))):
        if not q.strip() or len(q) > MAX_QUESTION:
            raise ApiError(400, "invalid_query", "検索する言葉を入れてください")
        return {"hits": [h.as_dict() for h in search(store(), q, doc_scope(user), max(1, min(top_k, 20)))]}

    @app.get("/api/documents")
    def documents(user: User = Depends(require("documents.view"))):
        s = store()
        # 文書管理（documents.manage）が全社に効く人は、すべての文書と置き場所を見る。
        # ほかは、見てよい文書と、扱ってよい部署（持ち主）の文書だけ
        manage = manage_scope(user)
        scope = doc_scope(user)
        docs = []
        for d in s.documents():
            if manage is None:
                docs.append(d)
            elif s.can_view(d["doc_id"], scope) or d["owner"] in manage:
                # 置き場所（サーバーのパス）は管理者にだけ見せる
                docs.append({k: v for k, v in d.items() if k not in ("path", "source")})
        return {"documents": docs}

    @app.get("/api/documents/{doc_id}")
    def document(doc_id: str, user: User = Depends(require("documents.view"))):
        s = store()
        # 見られない文書は、あるかどうかも分からせない（管理者でも、documents.view の効く部署で決める）
        doc = s.document_sections(doc_id) if s.can_view(doc_id, doc_scope(user)) else None
        if not doc:
            raise not_found("文書")
        return doc

    @app.get("/api/documents/ingest/sources")
    def ingest_sources(user: User = Depends(require("documents.manage"))):
        out, seen = [], set()
        for root in settings.ingest_roots:
            r = root.resolve()
            if not r.is_dir():
                continue
            for d in [r, *sorted(c for c in r.iterdir() if c.is_dir() and not c.name.startswith("."))]:
                if d in seen:
                    continue
                seen.add(d)
                try:
                    label = str(d.relative_to(Path.cwd().resolve()))
                except ValueError:
                    label = str(d)
                out.append({"path": str(d), "label": label})
        return {"sources": out}

    @app.post("/api/documents/ingest", status_code=202)
    def ingest(req: Ingest, user: User = Depends(require("documents.manage"))):
        path = Path(req.source).resolve()
        roots = [r.resolve() for r in settings.ingest_roots]
        if not any(path == r or path.is_relative_to(r) for r in roots) or not path.is_dir():
            raise ApiError(400, "invalid_source", "取り込めるフォルダではありません（設定の ingest_roots の下のフォルダだけ）")
        manage = manage_scope(user)
        chosen = None
        if req.mode == "chosen":
            owner = req.owner.strip()
            if not owner or (manage is not None and owner not in manage):
                raise ApiError(400, "invalid_document", "持ち主の部署は、あなたが扱える部署から選んでください",
                               [{"where": "owner", "message": "あなたが扱える部署から選んでください"}])
            chosen = {"owner": owner, "targets": check_publication(req.targets, req.required), "required": req.required,
                      "external": req.external}
        return jobs.submit("ingest", user.employee_no, {"source": str(path), "chosen": chosen, "by": user.employee_no,
                                                         "owners": None if manage is None else sorted(manage)})

    # ------------------------------------------------ 文書の画面（S-50。documents.manage）

    DOC_IMPACT_LIMIT = 20000        # 見られる人の増減を数える人数の上限（これより多ければ数えない）

    def target_names() -> dict[str, str]:
        with closing(connect()) as db:
            access.sync_targets(db)
            data = access.load(db)
        names = {tid: t["name"] for tid, t in data["targets"].items()}
        names[access.ALL] = "全社"
        return names

    def doc_publish_targets() -> list[dict]:
        """公開先に選べるもの：全社・部署・グループ（権限の対象と同じ番号）。"""
        with closing(connect()) as db:
            access.sync_targets(db)
            data = access.load(db)
        out = [{"target_id": access.ALL, "kind": "全社", "name": "全社"}]
        for kind, label in (("department", "部署"), ("group", "グループ")):
            out += [{"target_id": tid, "kind": label, "name": t["name"]} for tid, t in data["targets"].items() if t["kind"] == kind]
        return out

    def targets_text(targets: list[str], names: dict[str, str]) -> str:
        return "・".join(names.get(t, t) for t in targets) or "なし"

    def check_publication(targets: list[str], required: str) -> list[str]:
        allowed = {t["target_id"] for t in doc_publish_targets()}
        targets = list(dict.fromkeys(t for t in targets if isinstance(t, str)))
        problems = []
        if not targets:
            problems.append({"where": "targets", "message": "公開先を1つ以上選んでください（部署・グループ・全社）"})
        bad = [t for t in targets if t not in allowed]
        if bad:
            problems.append({"where": "targets", "message": f"公開先に選べないものです：{'・'.join(bad)}"})
        if required not in DOCUMENT_PERMISSIONS:
            problems.append({"where": "required", "message": f"見るのに要る権限は {'・'.join(DOCUMENT_PERMISSIONS)} のどれかにしてください"})
        if problems:
            raise ApiError(400, "invalid_document", problems[0]["message"], problems)
        return targets

    def managed_doc(user: User, doc_id: str) -> dict:
        """扱ってよい文書（持ち主の部署が documents.manage の対象）。ほかは、あるかどうかも分からせない。"""
        d = store().doc_settings(doc_id)
        manage = manage_scope(user)
        if not d or (manage is not None and d["owner"] not in manage):
            raise not_found("文書")
        return d

    def doc_view(d: dict, names: dict[str, str]) -> dict:
        return {**d, "target_names": [names.get(t, t) for t in d["targets"]]}

    @app.get("/api/admin/documents")
    def admin_documents(removed: bool = False, user: User = Depends(require("documents.manage"))):
        manage = manage_scope(user)
        names = target_names()
        docs = [doc_view(d, names) for d in store().documents(removed=removed) if manage is None or d["owner"] in manage]
        with closing(connect()) as db:
            depts = access._hr_departments(db)
        return {"documents": docs, "removed": removed, "publish_targets": doc_publish_targets(),
                "permissions": [{"name": p, "does": PERMISSIONS[p].does} for p in DOCUMENT_PERMISSIONS],
                "owners": depts if manage is None else sorted(manage)}

    def publication_detail(d: dict, targets: list[str], required: str, external: bool, names: dict[str, str]) -> str:
        parts = []
        if targets != d["targets"]:
            parts.append(f"公開先 {targets_text(d['targets'], names)}→{targets_text(targets, names)}")
        if required != d["required"]:
            parts.append(f"要る権限 {d['required']}→{required}")
        if external != d["external"]:
            parts.append(f"社外公開 {'あり' if d['external'] else 'なし'}→{'あり' if external else 'なし'}")
        return f"{d['title']}の" + "、".join(parts) if parts else ""

    def visibility_impact(before: tuple[str, list[str]] | None, after: tuple[str, list[str]] | None) -> dict:
        """見られるようになる人・見られなくなる人を、グループごとの人数で数える（保存しない）。None は「だれも見られない」（外した）。"""
        with closing(connect()) as db:
            people = access._active_people(db)
        if len(people) > DOC_IMPACT_LIMIT:
            return {"checked": len(people), "too_many": True, "gain": 0, "lose": 0, "groups": []}
        groups: dict[str, dict] = {}
        gain = lose = 0
        for no, dept in people:
            b = before is not None and engine.can_view_document(no, dept, before[0], before[1]).allowed
            a = after is not None and engine.can_view_document(no, dept, after[0], after[1]).allowed
            for name in engine.groups_of(no, dept):
                g = groups.setdefault(name, {"name": name, "members": 0, "gain": 0, "lose": 0, "can_see": 0})
                g["members"] += 1
                g["gain"] += int(a and not b)
                g["lose"] += int(b and not a)
                g["can_see"] += int(a)
            gain += int(a and not b)
            lose += int(b and not a)
        return {"checked": len(people), "too_many": False, "gain": gain, "lose": lose,
                "groups": [g for g in groups.values() if g["gain"] or g["lose"] or g["can_see"]]}

    def doc_change(user: User, req: "DocChange") -> tuple[dict, str, tuple | None, tuple | None, callable]:
        d = managed_doc(user, req.doc_id)
        names = target_names()
        now = None if d["removed_at"] else (d["required"], d["targets"])
        if req.action == "publish":
            targets = check_publication(req.targets or [], req.required or "documents.view")
            detail = publication_detail(d, targets, req.required or "documents.view", bool(req.external), names)
            if not detail:
                raise ApiError(400, "no_change", "変わったところがありません")
            later = None if d["removed_at"] else (req.required or "documents.view", targets)
            return d, detail, now, later, lambda: store().set_publication(d["doc_id"], targets, req.required or "documents.view",
                                                                          bool(req.external))
        if req.action == "remove":
            if d["removed_at"]:
                raise ApiError(409, "already_removed", "この文書は、もう外しています")
            return d, f"{d['title']}を外した（検索に出さない。公開先 {targets_text(d['targets'], names)}）", now, None, \
                lambda: store().set_removed(d["doc_id"], True, user.employee_no)
        if req.action == "restore":
            if not d["removed_at"]:
                raise ApiError(409, "not_removed", "この文書は外していません")
            return d, f"{d['title']}を戻した（公開先 {targets_text(d['targets'], names)}）", None, (d["required"], d["targets"]), \
                lambda: store().set_removed(d["doc_id"], False)
        raise ApiError(400, "invalid_request", "action は publish・remove・restore のどれかにしてください")

    @app.post("/api/admin/documents/preview")
    def admin_documents_preview(req: DocChange, user: User = Depends(require("documents.manage"))):
        """変える前の確かめ：見られるようになる人・見られなくなる人を、グループごとの人数で返す（保存しない）。"""
        d, detail, now, later, _ = doc_change(user, req)
        return {"doc_id": d["doc_id"], "detail": detail, **visibility_impact(now, later)}

    DOC_ACTIONS = {"publish": "documents.publish", "remove": "documents.remove", "restore": "documents.restore"}

    def doc_apply(user: User, req: "DocChange") -> dict:
        try:
            d, detail, _, _, write = doc_change(user, req)
        except ApiError as e:
            if e.status != 404:
                audit_by(user, DOC_ACTIONS.get(req.action, "documents.publish"), target=req.doc_id, result=audit.DENIED,
                         detail=f"変えませんでした：{e.message}")
            raise
        write()
        audit_by(user, DOC_ACTIONS[req.action], target=d["doc_id"], detail=detail)
        return {"ok": True, "detail": detail, "document": doc_view(store().doc_settings(d["doc_id"]), target_names())}

    @app.patch("/api/admin/documents/{doc_id}")
    def admin_document_publish(doc_id: str, req: DocPublication, user: User = Depends(require("documents.manage"))):
        # 中身（本文とチャンク）は変えずに、公開先・要る権限・社外公開だけを直す
        return doc_apply(user, DocChange(doc_id=doc_id, action="publish", **req.model_dump()))

    @app.post("/api/admin/documents/{doc_id}/remove")
    def admin_document_remove(doc_id: str, user: User = Depends(require("documents.manage"))):
        return doc_apply(user, DocChange(doc_id=doc_id, action="remove"))

    @app.post("/api/admin/documents/{doc_id}/restore")
    def admin_document_restore(doc_id: str, user: User = Depends(require("documents.manage"))):
        return doc_apply(user, DocChange(doc_id=doc_id, action="restore"))

    # ------------------------------------------------ ヘルプデスク

    @app.post("/api/chat/sessions", status_code=201)
    def chat_start(req: ChatStart, request: Request, user: User = Depends(require("helpdesk"))):
        if req.flow != "helpdesk":
            raise ApiError(400, "unknown_flow", "その会話は使えません")
        if req.account is not None and not settings.dev_login:
            raise deny(user, "helpdesk.account（ほかの人として話す）", request)
        return request.app.state.helpdesk.start(user, req.account)

    @app.post("/api/chat/sessions/{session_id}/messages")
    def chat_send(session_id: str, req: ChatMessage, request: Request, user: User = Depends(require("helpdesk"))):
        return request.app.state.helpdesk.send(user, session_id, req.text)

    @app.delete("/api/chat/sessions/{session_id}")
    def chat_end(session_id: str, request: Request, user: User = Depends(require("helpdesk"))):
        request.app.state.helpdesk.end(user, session_id)
        return {"ok": True}

    # ------------------------------------------------ ジョブ

    def visible_job(job_id: str, user: User) -> dict:
        job = jobs.get(job_id)
        # 他の人のジョブは、あるかどうかも分からせない
        if not job or (job["owner"] != user.employee_no and not can(user, "jobs.all")):
            raise not_found("ジョブ")
        return job

    @app.get("/api/jobs")
    def job_list(request: Request, all: bool = False, user: User = Depends(require("jobs.own"))):
        if all and not can(user, "jobs.all"):
            raise deny(user, "jobs.all", request)
        return {"jobs": jobs.list(None if all else user.employee_no)}

    @app.get("/api/jobs/{job_id}")
    def job_get(job_id: str, user: User = Depends(require("jobs.own"))):
        return visible_job(job_id, user)

    @app.delete("/api/jobs/{job_id}")
    def job_cancel(job_id: str, user: User = Depends(require("jobs.own"))):
        job = visible_job(job_id, user)
        if job["status"] in FINISHED:
            raise ApiError(409, "finished", "このジョブは終わっています")
        # 取り消した人を残す（ジョブの持ち主の「取り消し」は、ジョブの出来事として別に残る）
        if job["owner"] != user.employee_no:
            audit_by(user, "job.cancel", target=job_id, detail=f"ほかの人のジョブ（{job['owner']}）を取り消しました")
        return jobs.cancel(job_id)

    # ------------------------------------------------ お知らせ（S-11）

    def note_out(n: dict, user: User) -> dict:
        return {"notification_id": n["notification_id"], "created_at": n["created_at"], "kind": n["kind"],
                "message": n["message"], "ref": n["ref"], "read": bool(n["read"]),
                "to": "あなた" if n["employee_no"] == user.employee_no else n["dept"]}

    @app.get("/api/notifications")
    def notification_list(unread: int = 0, limit: int = 100, user: User = Depends(require("notifications"))):
        with closing(connect()) as db:
            items = visits.notifications(db, user.employee_no, user.dept, unread_only=bool(unread),
                                         limit=max(1, min(limit, 500)))
            count = len(visits.notifications(db, user.employee_no, user.dept, unread_only=True))
        return {"notifications": [note_out(n, user) for n in reversed(items)], "unread": count}

    @app.post("/api/notifications/read")
    def notification_read(req: ReadNotifications, user: User = Depends(require("notifications"))):
        with closing(connect()) as db:
            # 見られるお知らせだけを既読にする。ほかの人のお知らせの番号は、何も言わずに無視する
            visible = {n["notification_id"] for n in visits.notifications(db, user.employee_no, user.dept, unread_only=True)}
            targets = visible if req.all else visible & set(req.ids)
            visits.mark_read(db, sorted(targets))
        return {"read": len(targets), "unread": len(visible) - len(targets)}

    # ------------------------------------------------ 来訪予定（S-12・S-22）

    def visit_out(db, v: dict) -> dict:
        host = hr.lookup(db, v["host_employee_no"])
        return {**v, "host_name": host["name"] if host else "", "host_dept": host["dept"] if host else ""}

    @app.post("/api/visits", status_code=201)
    def visit_create(req: VisitCreate, request: Request, user: User = Depends(require("visits.own"))):
        host_no = req.host_employee_no or user.employee_no
        if host_no != user.employee_no and not can(user, "visits.all"):
            raise deny(user, "visits.all（ほかの人の来訪予定の登録）", request)
        with closing(connect()) as db:
            try:
                visit, code = visits.create(db, host_employee_no=host_no, visit_date=req.date, time=req.time, room=req.room,
                                            visitor_company=req.visitor_company, visitor_name=req.visitor_name,
                                            purpose=req.purpose, host_contact=req.host_contact)
            except visits.VisitError as e:
                raise ApiError(400, "invalid_visit", str(e)) from e
            host = hr.lookup(db, host_no)
            out = visit_out(db, {**visit, "cancelled": False})
        # コードは保存していない。ここで1回だけ返す
        return {"visit": out, "code": visits.format_code(code),
                "invitation": visits.invitation_text(visit, code, host["name"] if host else "")}

    @app.get("/api/visits")
    def visit_list(request: Request, host: str = "me", date: str | None = None, user: User = Depends(require("visits.own"))):
        everyone = can(user, "visits.all")
        day = None
        if date is not None:
            if not everyone:
                raise deny(user, "visits.all（日付で全社の予定を見る）", request)
            try:
                day = Date.fromisoformat(date)
            except ValueError as e:
                raise ApiError(400, "invalid_date", "日付は YYYY-MM-DD で指定してください") from e
        if host != "me" and not everyone:
            raise deny(user, "visits.all（ほかの人の来訪予定を見る）", request)
        if day is not None:
            host_no = None if host in ("me", "all") else host          # 総務：その日の全社の予定
        else:
            host_no = None if host == "all" else (user.employee_no if host == "me" else host)
        with closing(connect()) as db:
            items = visits.list_visits(db, host_employee_no=host_no, day=day)
            return {"visits": [visit_out(db, v) for v in items]}

    @app.delete("/api/visits/{visit_id}")
    def visit_cancel(visit_id: int, user: User = Depends(require("visits.own"))):
        with closing(connect()) as db:
            v = visits.find(db, visit_id)
            # ほかの人の予定は、あるかどうかも分からせない（総務は全社の予定を取り消せる）
            if not v or (v["host_employee_no"] != user.employee_no and not can(user, "visits.all")):
                raise not_found("来訪予定")
            visits.cancel(db, visit_id)
        return {"ok": True}

    @app.post("/api/visits/import")
    async def visit_import(request: Request, user: User = Depends(require("visits.all"))):
        raw = await request.body()
        if len(raw) > settings.visits_import_max_bytes:
            raise ApiError(413, "too_large", "ファイルが大きすぎます")
        try:
            text = raw.decode("utf-8-sig")
        except UnicodeDecodeError as e:
            raise ApiError(400, "invalid_csv", "CSV は UTF-8 で保存してください") from e
        if not text.strip():
            raise ApiError(400, "invalid_csv", "CSV が空です")

        def run() -> dict:
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "visits.csv"
                path.write_text(text, encoding="utf-8")
                with closing(connect()) as db:
                    if visits.is_calendar_export(path):
                        return {"kind": "calendar", **visits.import_calendar_csv(db, path)}
                    header = text.splitlines()[0].split(",")
                    absent = [c for c in VISIT_CSV_COLUMNS if c not in [h.strip() for h in header]]
                    if absent:
                        raise ApiError(400, "invalid_csv", f"列がありません: {'、'.join(absent)}")
                    try:
                        added = visits.import_csv(db, path)
                    except visits.VisitError as e:
                        raise ApiError(400, "invalid_csv", f"取り込めない行があります（その前の行までは登録しました）：{e}") from e
                    return {"kind": "visits", "errors": [],
                            "added": [{"visit": visit_out(db, {**v, "cancelled": False}), "code": visits.format_code(c)}
                                      for v, c in added]}

        return await run_in_threadpool(run)

    # ------------------------------------------------ 受付の記録（S-23）

    @app.get("/api/reception/records")
    def record_list(status: str | None = None, date: str | None = None, user: User = Depends(require("reception.records"))):
        if status is not None and status not in desk.RECORD_STATUSES:
            raise ApiError(400, "invalid_status", f"状態は {'・'.join(desk.RECORD_STATUSES)} のどれかです")
        with closing(connect()) as db:
            return {"records": desk.records(db, status, date)}

    @app.post("/api/reception/records/{record_id}/done")
    def record_done(record_id: str, user: User = Depends(require("reception.records"))):
        with closing(connect()) as db:
            if not desk.record_exists(db, record_id):
                raise not_found("受付の記録")
            desk.set_status(db, "record", record_id, "対応済み", None, user.employee_no)
            return next(r for r in desk.records(db) if r["record_id"] == record_id)

    # ------------------------------------------------ 問い合わせ票（S-30）

    @app.get("/api/tickets")
    def ticket_list(status: str | None = None, priority: str | None = None, user: User = Depends(require("helpdesk.handle"))):
        if status is not None and status not in desk.TICKET_STATUSES:
            raise ApiError(400, "invalid_status", f"状態は {'・'.join(desk.TICKET_STATUSES)} のどれかです")
        if priority is not None and priority not in desk.PRIORITIES:
            raise ApiError(400, "invalid_priority", "優先度は 高・中・低 のどれかです")
        with closing(connect()) as db:
            return {"tickets": desk.tickets(db, status, priority)}

    @app.get("/api/tickets/{ticket_id}")
    def ticket_get(ticket_id: str, user: User = Depends(require("helpdesk.handle"))):
        with closing(connect()) as db:
            t = desk.ticket(db, ticket_id)
        if not t:
            raise not_found("問い合わせ票")
        return t

    @app.patch("/api/tickets/{ticket_id}")
    def ticket_update(ticket_id: str, req: TicketUpdate, user: User = Depends(require("helpdesk.handle"))):
        if req.status is not None and req.status not in desk.TICKET_STATUSES:
            raise ApiError(400, "invalid_status", f"状態は {'・'.join(desk.TICKET_STATUSES)} のどれかです")
        with closing(connect()) as db:
            t = desk.ticket(db, ticket_id)
            if not t:
                raise not_found("問い合わせ票")
            desk.set_status(db, "ticket", ticket_id, req.status or t["status"], req.note, user.employee_no)
            return desk.ticket(db, ticket_id)

    # ------------------------------------------------ 受付端末の登録（S-54。管理者が社内のログインで使う）

    @app.get("/api/admin/kiosk-devices")
    def device_list(user: User = Depends(require("kiosk.devices"))):
        with closing(connect()) as db:
            return {"devices": kiosk.devices(db)}

    @app.post("/api/admin/kiosk-devices", status_code=201)
    def device_register(req: DeviceCreate, user: User = Depends(require("kiosk.devices"))):
        with closing(connect()) as db:
            # 端末トークンはここで1回だけ返す（保存するのはハッシュだけ）。監査ログには番号と名前だけを残す
            device = kiosk.register(db, req.name.strip(), user.employee_no)
        audit_by(user, "kiosk.register", target=device["device_id"], detail=f"受付端末「{device['name']}」を登録しました")
        return device

    @app.delete("/api/admin/kiosk-devices/{device_id}")
    def device_revoke(device_id: str, user: User = Depends(require("kiosk.devices"))):
        with closing(connect()) as db:
            name = next((d["name"] for d in kiosk.devices(db) if d["device_id"] == device_id), "")
            if not kiosk.revoke(db, device_id):
                raise not_found("受付端末")
        audit_by(user, "kiosk.revoke", target=device_id, detail=f"受付端末「{name}」を取り消しました")
        return {"ok": True}

    # ------------------------------------------------ 受付端末（S-40・S-41。端末トークンだけ）
    # 受付の API は async にし、重い処理（照合・会話）はスレッドで行い、待ち時間は asyncio.sleep で待つ

    def kiosk_reply(r: dict) -> dict:
        # 返事の形はいつも同じ（予定がないときは null）。入館票は画面がこの予定から作る
        out = {"session_id": r["session_id"], "finished": r["finished"], "reply": r["reply"], "visit": None}
        if r.get("visit"):
            with closing(connect()) as db:
                out["visit"] = kiosk.visit_for_kiosk(r["visit"], hr.lookup(db, r["visit"]["host_employee_no"]))
        return out

    @app.post("/api/kiosk/sessions", status_code=201)
    async def kiosk_start(request: Request, device: dict = Depends(current_device)):
        started = time.monotonic()
        try:
            r = await run_in_threadpool(request.app.state.reception.start, f"kiosk:{device['device_id']}")
            return await run_in_threadpool(kiosk_reply, r)
        finally:
            await padded(started)

    @app.post("/api/kiosk/sessions/{session_id}/messages")
    async def kiosk_send(session_id: str, req: ChatMessage, request: Request, device: dict = Depends(current_device)):
        started = time.monotonic()
        try:
            r = await run_in_threadpool(request.app.state.reception.send, f"kiosk:{device['device_id']}", session_id, req.text)
            return await run_in_threadpool(kiosk_reply, r)
        finally:
            await padded(started)

    def check_in(device: dict, code: str) -> dict:
        failed = {"ok": False, "message": kiosk.FAILED_CODE, "visit": None}
        # 失敗が続いている端末では照合しない。返事は間違えたときと同じ（止めているかどうかを分からせない）
        if limiter.blocked(device["device_id"]):
            return failed
        with closing(connect()) as db:
            visit = visits.find_by_code(db, code, today())
            if not visit:
                if limiter.fail(device["device_id"]):
                    # 入力されたコードは残さない（間違えた回数と、止めたことだけ）
                    auditor.record("kiosk.locked", actor=device["device_id"], actor_name=device["name"], kind=audit.DEVICE,
                                   target=device["device_id"], result=audit.DENIED,
                                   detail=f"コードを1分に{settings.kiosk_max_failures}回間違えたので、しばらく照合を止めました")
                return failed
            visit = visits.check_in(db, visit)
            host = hr.lookup(db, visit["host_employee_no"])
            if visit["first_check_in"]:
                desk.add_record(db, channel="来訪", category="来訪", purpose="インビテーションコードで受付",
                                visitor={"company": visit["visitor_company"], "name": visit["visitor_name"]},
                                host=host, route=host["dept"] if host else "総務部", priority="低",
                                appointment={k: v for k, v in visit.items() if k != "first_check_in"})
            shown = kiosk.visit_for_kiosk(visit, host)
        return {"ok": True, "message": f"{shown['visitor_company']} {shown['visitor_name']}様、お待ちしておりました。"
                                       "担当の者に到着をお知らせしました。入館票をお受け取りのうえ、会議室へお進みください。",
                "visit": shown}

    @app.post("/api/kiosk/check-in")
    async def kiosk_check_in(req: CheckIn, device: dict = Depends(current_device)):
        started = time.monotonic()
        try:
            return await run_in_threadpool(check_in, device, req.code)
        finally:
            await padded(started)

    # ------------------------------------------------ ファイルの受け渡し（段階3）

    uploading: dict[str, int] = {}      # 受け取り中のアップロード（番号 → 受け取った大きさ）

    def work_full() -> ApiError:
        return ApiError(507, "work_full", "サーバーの作業用の場所がいっぱいです。古いファイルは "
                        f"{settings.file_keep_hours} 時間で消えるので、しばらくしてからもう一度送ってください")

    @app.post("/api/uploads", status_code=201)
    async def upload(request: Request, kind: str, name: str = "", user: User = Depends(current_user)):
        if kind not in UPLOAD_KINDS:
            raise ApiError(400, "invalid_kind", "アップロードの種類が正しくありません")
        perm, limit_key, exts = UPLOAD_KINDS[kind]
        if not can(user, perm):
            raise await run_in_threadpool(deny, user, f"{perm}（{kind} のアップロード）", request)
        limit = getattr(settings, limit_key)
        shown = files.safe_name(name, f"upload{exts[0]}")
        if not shown.lower().endswith(exts):
            raise ApiError(400, "invalid_type", f"このファイルは受け付けられません（{'・'.join(exts)}）")
        declared = request.headers.get("content-length")
        if declared and declared.isdigit() and int(declared) > limit:
            raise ApiError(413, "too_large", f"ファイルが大きすぎます（上限 {limit // 1_000_000}MB）")

        def used_now() -> int:
            with closing(connect()) as db:
                files.cleanup(db, settings.work_dir)
                return files.total_size(db)

        # 作業用フォルダの合計：置いているファイル＋ほかに受け取り中のファイル＋これから受け取る大きさ
        used = await run_in_threadpool(used_now)
        if used + sum(uploading.values()) + (int(declared) if declared and declared.isdigit() else 0) > settings.work_max_bytes:
            raise work_full()
        file_id = files.new_id()
        folder = settings.work_dir / "uploads"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{file_id}{Path(shown).suffix.lower()}"
        size, head = 0, b""
        uploading[file_id] = 0
        try:
            with path.open("wb") as out:
                # 受け取りながら大きさを数え、上限を超えたらその場で止める（全部を受け取ってから断らない）
                async for chunk in request.stream():
                    size += len(chunk)
                    if size > limit:
                        raise ApiError(413, "too_large", f"ファイルが大きすぎます（上限 {limit // 1_000_000}MB）")
                    uploading[file_id] = size
                    if used + sum(uploading.values()) > settings.work_max_bytes:
                        raise work_full()
                    if len(head) < 8:
                        head += chunk[:8]
                    out.write(chunk)
            if size == 0:
                raise ApiError(400, "empty", "ファイルが空です")
            if kind == "pdf" and not head.startswith(b"%PDF-"):
                raise ApiError(400, "invalid_type", "PDF ではないファイルです")
            if kind == "segments":
                try:
                    data = json.loads(path.read_text(encoding="utf-8"))
                    assert isinstance(data, list) and all(isinstance(x, dict) and "text" in x and "start" in x for x in data)
                except (ValueError, AssertionError, UnicodeDecodeError) as e:
                    raise ApiError(400, "invalid_type", "文字起こし済みの発言（JSON）の形が正しくありません") from e

            others = sum(v for k, v in uploading.items() if k != file_id)

            def register():
                with closing(connect()) as db:
                    files.cleanup(db, settings.work_dir)
                    # 受け取っている間に、ほかのアップロードが終わって置かれた分も入れて、もう一度確かめる
                    if files.total_size(db) + size + others > settings.work_max_bytes:
                        raise work_full()
                    return files.add(db, file_id=file_id, owner=user.employee_no, kind=kind, perm=perm, name=shown,
                                     path=path, keep_hours=settings.file_keep_hours)

            return await run_in_threadpool(register)
        except BaseException:
            path.unlink(missing_ok=True)
            raise
        finally:
            uploading.pop(file_id, None)

    def my_file(file_id: str, user: User, kind: str | None = None) -> dict:
        with closing(connect()) as db:
            f = files.get(db, file_id, user.employee_no, kind)
        # 権限がなくなった人・ほかの人・期限切れ・ないものは、どれも同じ「見つかりません」
        if not f or not can(user, f["perm"]):
            raise not_found("ファイル")
        return f

    @app.get("/api/files/{file_id}")
    def download(file_id: str, user: User = Depends(current_user)):
        f = my_file(file_id, user)
        audit_by(user, "file.download", target=file_id, detail=f"「{f['name']}」をダウンロードしました")
        return FileResponse(f["path"], filename=f["name"], headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    # ------------------------------------------------ PDF の仕分け（S-20）・変換（S-21）

    @app.get("/api/paper/inbox")
    def paper_inbox(user: User = Depends(require("paper"))):
        return {"files": paperjobs.inbox_list(settings.paper_inbox)}

    @app.get("/api/paper/rules")
    def paper_rule_list(user: User = Depends(require("paper"))):
        rs = paper_rules()
        return {"rules": [{"name": r.name, "templates": sorted(r.templates)} for r in rs.rules]}

    @app.post("/api/paper/plans", status_code=202)
    def paper_plan(req: PaperPlanCreate, user: User = Depends(require("paper"))):
        for n in req.files:
            if not paperjobs.inbox_path(settings.paper_inbox, n):
                raise ApiError(400, "not_in_inbox", f"受信箱にないファイルです：{files.safe_name(n)}")
        if not req.files and not paperjobs.inbox_files(settings.paper_inbox):
            raise ApiError(400, "empty_inbox", "受信箱に PDF がありません")
        paper_rules()   # ルールが読めないときは、ジョブにする前に知らせる
        return jobs.submit("paper-plan", user.employee_no, {"owner": user.employee_no, "files": req.files, "use_llm": req.use_llm})

    def my_plan(db, plan_id: str, user: User) -> dict:
        plan = paperjobs.get_plan(db, plan_id, user.employee_no)
        if not plan:
            raise not_found("仕分けの予定")
        return plan

    @app.get("/api/paper/plans/{plan_id}")
    def paper_plan_get(plan_id: str, user: User = Depends(require("paper"))):
        with closing(connect()) as db:
            return my_plan(db, plan_id, user)

    @app.put("/api/paper/plans/{plan_id}/items/{index}")
    def paper_rechoose(plan_id: str, index: int, req: PaperRechoose, user: User = Depends(require("paper"))):
        rs = paper_rules()
        with closing(connect()) as db:
            plan = my_plan(db, plan_id, user)
            if plan["executed_at"]:
                raise ApiError(409, "executed", "この予定は実行済みです。新しく予定を作ってください")
            if not 0 <= index < len(plan["items"]):
                raise not_found("予定の項目")
            if plan["items"][index]["error"]:
                raise ApiError(409, "unreadable", "読めなかったファイルは、ルールを選び直せません")
            if req.rule and req.rule not in rs.names():
                raise ApiError(400, "unknown_rule", "そのルールはありません")
            return paperjobs.rechoose(db, plan_id, index, req.rule, settings.paper_inbox, rs)

    @app.post("/api/paper/plans/{plan_id}/execute")
    def paper_execute(plan_id: str, req: PaperExecute, user: User = Depends(require("paper"))):
        with closing(connect()) as db:
            plan = my_plan(db, plan_id, user)
            if plan["executed_at"]:
                raise ApiError(409, "executed", "この予定は実行済みです")
            # 予定を作ったあとにルールが消えた（版に戻した・既定に戻した）ときは、置かずに作り直しを促す
            names = set(paper_rules().names())
            gone = [x for x in plan["items"] if not x["error"] and x["rule"] and x["rule"] not in names]
            if gone:
                audit_by(user, "paper.execute", target=plan_id, result=audit.FAILED,
                         detail=f"使ったルールがなくなっていたので実行しませんでした（{len(gone)}件）")
                raise ApiError(409, "rules_gone",
                               "予定を作ったあとに、使ったルールがなくなりました。予定を作り直してから実行してください",
                               [{"where": x["file"], "message": f"ルール「{x['rule']}」は今のルールにありません"} for x in gone])
            result = paperjobs.execute(db, plan_id, settings.paper_inbox, settings.paper_out, move=req.move)
        # 何件を、どのフォルダへ（ファイル名は書類の中身から作るので残さない。フォルダだけ）
        folders = sorted({str(Path(p).parent) for p in result["placed"]})
        audit_by(user, "paper.execute", target=plan_id,
                 detail=f"{len(result['placed'])}件を{'移しました' if req.move else '写しました'}"
                        f"（置かなかったもの {len(result['skipped'])}件）。置いた先：{'、'.join(folders) or 'なし'}")
        return result

    @app.get("/api/paper/history")
    def paper_history(limit: int = 200, user: User = Depends(require("paper"))):
        return {"records": paperjobs.history(settings.paper_out, max(1, min(limit, 1000)))}

    # ------------------------------------------------ 仕分けルールの下書き・保存・前の版（K-047）

    RULE_KEYS = {"name", "keywords", "fields", "filename", "folder", "priority"}   # 画面から書けるところ（様式は書けない）

    @app.post("/api/paper/rule-drafts", status_code=202)
    def paper_rule_draft(req: PaperRuleDraft, user: User = Depends(require("paper.rules"))):
        if bool(req.plan_id) == bool(req.inbox):
            raise ApiError(400, "invalid_source", "予定の1件か、受信箱の PDF のどちらか1つを選んでください")
        params = {"owner": user.employee_no, "name": (req.name or "").strip(), "use_llm": req.use_llm}
        if req.plan_id:
            with closing(connect()) as db:
                plan = my_plan(db, req.plan_id, user)
            if req.index is None or not 0 <= req.index < len(plan["items"]):
                raise not_found("予定の項目")
            item = plan["items"][req.index]
            if item["error"]:
                raise ApiError(409, "unreadable", "読めなかったファイルからは、下書きを作れません")
            params.update(plan_id=req.plan_id, index=req.index, source=item["file"])
        else:
            if not paperjobs.inbox_path(settings.paper_inbox, req.inbox or ""):
                raise ApiError(400, "not_in_inbox", "受信箱にないファイルです")
            params["inbox"] = req.inbox
        return jobs.submit("paper-rule-draft", user.employee_no, params)

    def rules_with(rule: dict):
        """今のルールに rule を足した中身を作る。読み込みと同じ検証にかけ、誤りは位置つきで 400。"""
        from ..paper import rules as paper_rule_file

        unknown = sorted(set(rule) - RULE_KEYS)
        if unknown:
            raise ApiError(400, "invalid_rule", "ルールに書けない項目があります",
                           [{"where": k, "message": "画面からは書けない項目です（様式はルールのファイルで設定します）"} for k in unknown])

        def build(current: str) -> str:
            try:
                data = json.loads(current)
            except ValueError as e:
                raise ApiError(409, "rules_broken", "今のルールのファイルが読めません。管理者に連絡してください") from e
            if not isinstance(data, dict) or not isinstance(data.setdefault("rules", []), list):
                raise ApiError(409, "rules_broken", "今のルールのファイルが読めません。管理者に連絡してください")
            data["rules"].append(rule)
            at = f"rules[{len(data['rules'])}]"
            try:
                paper_rule_file.parse_rules(data, settings.paper_rules.parent, settings.paper_rules.name)
            except paper_rule_file.RuleError as e:
                # 足すルールの誤りは、ルールの中の位置（例：fields.invoice_no）で返す。今のルールの誤りはそのまま
                details = [{"where": (w[len(at) + 1:] if w.startswith(at + ".") else ("" if w == at else w)), "message": m}
                           for w, m in e.problems]
                raise ApiError(400, "invalid_rule", "ルールに直すところがあります", details) from e
            return json.dumps(data, ensure_ascii=False, indent=2) + "\n"

        return build

    def setting_error(e: SettingError, status: int, code: str, message: str) -> ApiError:
        return ApiError(status, code, message, [{"where": w, "message": m} for w, m in e.problems])

    @app.post("/api/paper/rules/validate")
    def paper_rule_validate(req: PaperRuleSave, user: User = Depends(require("paper.rules"))):
        from ..paper import rules as paper_rule_file

        rules_with(req.rule)(rule_store().read_text())
        # 誤りではない注意：ファイル名・保存先に、下書きのもとの PDF から読み取れない項目を使っている（保存はできる）
        warnings = []
        if req.sample_text is not None:
            warnings = [{"where": w, "message": m}
                        for w, m in paper_rule_file.unread_fields(req.rule, req.sample_text, paper_rules().self_names)]
        return {"ok": True, "warnings": warnings}

    @app.post("/api/paper/rules", status_code=201)
    def paper_rule_save(req: PaperRuleSave, user: User = Depends(require("paper.rules"))):
        name = str(req.rule.get("name", "")).strip()
        try:
            version = rule_store().save(rules_with(req.rule), user.employee_no, f"ルール「{name}」を足した")
        except SettingError as e:
            raise setting_error(e, 400, "invalid_rule", "ルールに直すところがあります") from e
        log.info("仕分けルール「%s」を足しました（版 %s、%s、%s）", name, version, user.employee_no, rule_store().saved)
        return {"version": version, "rules": paper_rule_list(user)["rules"]}

    @app.get("/api/paper/rules/status")
    def paper_rule_status(user: User = Depends(require("paper"))):
        # 仕分けの画面で「既定のルールが更新されています」を知らせるのに使う（総務の全員）
        return rule_store().status()

    @app.get("/api/paper/rules/history")
    def paper_rule_history(user: User = Depends(require("paper.rules"))):
        versions = rule_store().history()
        with closing(connect()) as db:
            names = {no: ((hr.lookup(db, no) or {}).get("name") or no) for no in {v["saved_by"] for v in versions}}
        # 保存した人は名前で出す（総務の人どうしで、だれが直したかを見るため）
        return {"versions": [{**v, "saved_by_name": names.get(v["saved_by"], v["saved_by"])} for v in versions],
                "current": rule_store().status()["current_version"], "status": rule_store().status()}

    @app.get("/api/paper/rules/history/{version}")
    def paper_rule_version(version: int, user: User = Depends(require("paper.rules"))):
        body_text = rule_store().get(version)
        if body_text is None:
            raise not_found("ルールの版")
        try:
            data = json.loads(body_text)
            names = [r.get("name", "") for r in data.get("rules", []) if isinstance(r, dict)]
        except (ValueError, AttributeError):
            names = []
        return {"version": version, "rules": names, "body": body_text}

    @app.post("/api/paper/rules/history/{version}/restore")
    def paper_rule_restore(version: int, user: User = Depends(require("paper.rules"))):
        try:
            new = rule_store().restore(version, user.employee_no)
        except KeyError as e:
            raise not_found("ルールの版") from e
        except SettingError as e:
            raise setting_error(e, 409, "invalid_version", "この版は、今の検証を通らないので戻せません") from e
        log.info("仕分けルールを版 %s に戻しました（新しい版 %s、%s）", version, new, user.employee_no)
        return {"version": new, "rules": paper_rule_list(user)["rules"]}

    @app.post("/api/paper/rules/reset")
    def paper_rule_reset(user: User = Depends(require("paper.rules"))):
        try:
            new = rule_store().reset_to_default(user.employee_no)
        except SettingError as e:
            raise setting_error(e, 409, "invalid_default", "既定のルールが今の検証を通らないので、戻せません。管理者に連絡してください") from e
        if new is None:
            # ほかの画面で先に戻していたときなど。版は増やさない
            return {"version": None, "already_default": True, "message": "すでに既定です", "rules": paper_rule_list(user)["rules"]}
        log.info("仕分けルールを既定に戻しました（版 %s、%s）", new, user.employee_no)
        return {"version": new, "already_default": False, "rules": paper_rule_list(user)["rules"]}

    @app.post("/api/paper/convert", status_code=202)
    def paper_convert(req: PaperConvert, user: User = Depends(require("paper"))):
        if bool(req.file_id) == bool(req.inbox):
            raise ApiError(400, "invalid_source", "アップロードした PDF か、受信箱の PDF のどちらか1つを選んでください")
        if req.file_id:
            name = my_file(req.file_id, user, "pdf")["name"]
        else:
            p = paperjobs.inbox_path(settings.paper_inbox, req.inbox or "")
            if not p:
                raise ApiError(400, "not_in_inbox", "受信箱にないファイルです")
            name = p.name
        if req.rule:
            if req.rule not in paper_rules().names():
                raise ApiError(400, "unknown_rule", "そのルールはありません")
        return jobs.submit("paper-convert", user.employee_no, {
            "owner": user.employee_no, "file_id": req.file_id, "inbox": req.inbox, "source_name": name, "to": req.to,
            "form": req.form, "rule": req.rule})

    # ------------------------------------------------ 議事録（S-13）

    def my_meeting(db, meeting_id: str, user: User) -> dict:
        m = meetings.get(db, meeting_id, user.employee_no)
        if not m:
            raise not_found("議事録")
        return m

    @app.post("/api/meetings", status_code=202)
    def meeting_create(req: MeetingCreate, user: User = Depends(require("meetings"))):
        media = my_file(req.upload_id, user, "recording")
        seg = my_file(req.segments_upload_id, user, "segments") if req.segments_upload_id else None
        try:
            day = date.fromisoformat(req.date).isoformat() if req.date else today().isoformat()
        except ValueError as e:
            raise ApiError(400, "invalid_date", "開催日は YYYY-MM-DD で指定してください") from e
        meeting_id = files.new_id()
        with closing(connect()) as db:
            meetings.create(db, meeting_id=meeting_id, owner=user.employee_no, title=req.title.strip(), day=day,
                            place=req.place.strip(), tool=req.tool,
                            participants=[p.strip()[:60] for p in req.participants if p.strip()], media_name=media["name"])
            job = jobs.submit("meeting", user.employee_no, {
                "owner": user.employee_no, "meeting_id": meeting_id, "upload_id": req.upload_id,
                "segments_upload_id": req.segments_upload_id if seg else None, "summary": req.summary})
            meetings.set_job(db, meeting_id, job["job_id"])
        return {**job, "meeting_id": meeting_id}

    @app.get("/api/meetings")
    def meeting_list(user: User = Depends(require("meetings"))):
        with closing(connect()) as db:
            return {"meetings": meetings.list_for(db, user.employee_no)}

    @app.get("/api/meetings/{meeting_id}")
    def meeting_get(meeting_id: str, user: User = Depends(require("meetings"))):
        with closing(connect()) as db:
            return my_meeting(db, meeting_id, user)

    @app.put("/api/meetings/{meeting_id}")
    async def meeting_update(meeting_id: str, request: Request, user: User = Depends(require("meetings"))):
        try:
            changes = await request.json()
        except ValueError as e:
            raise ApiError(400, "invalid_request", "リクエストの形が正しくありません") from e
        if not isinstance(changes, dict):
            raise ApiError(400, "invalid_request", "リクエストの形が正しくありません")

        def run():
            with closing(connect()) as db:
                my_meeting(db, meeting_id, user)
                try:
                    meetings.update(db, meeting_id, changes)
                except meetings.MeetingError as e:
                    raise ApiError(400, "invalid_meeting", str(e)) from e
                return my_meeting(db, meeting_id, user)

        return await run_in_threadpool(run)

    @app.get("/api/meetings/{meeting_id}/minutes.docx")
    def meeting_docx(meeting_id: str, user: User = Depends(require("meetings"))):
        with closing(connect()) as db:
            m = my_meeting(db, meeting_id, user)
        if m["status"] != meetings.REVIEW:
            raise ApiError(409, "not_ready", "まだ議事録を作れません（処理中か、失敗しています）")
        if not settings.meeting_template.is_file():
            raise ApiError(500, "no_template", "議事録の様式（Word）が見つかりません。管理者に問い合わせてください")
        body, name = meetings.minutes_docx(m, settings.meeting_template)
        audit_by(user, "meeting.download", target=meeting_id, detail=f"議事録の Word「{name}」をダウンロードしました")
        from urllib.parse import quote

        return RawResponse(body, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                           headers={"Content-Disposition": f"attachment; filename=\"minutes.docx\"; filename*=UTF-8''{quote(name)}",
                                    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

    # ------------------------------------------------ 人事の画面（S-51。取り込みは hr.import、一覧は hr.view）

    HR_LOCK = threading.Lock()        # 確定・戻すは1つずつ（権限の変更 access.CHANGE_LOCK とも重ねない）
    MAPPING_NAME_MAX = 40

    def hr_mapping_problems(text: str) -> list[tuple[str, str]]:
        try:
            data = json.loads(text)
        except ValueError as e:
            return [("", f"JSON として読めません（{e}）")]
        if not isinstance(data, dict) or not isinstance(data.get("mappings"), dict):
            return [("mappings", "{\"mappings\": {\"名前\": {...}}} の形にしてください")]
        out = []
        for name, m in data["mappings"].items():
            if not str(name).strip() or len(str(name)) > MAPPING_NAME_MAX:
                out.append((f"mappings.{name}", f"名前は1〜{MAPPING_NAME_MAX}文字にしてください"))
            out += [(f"{name}: {w}" if w else str(name), msg) for w, msg in hrimport.mapping_problems(m)]
        return out

    def hr_mappings_store():
        from ..settingfiles import SettingFile

        return SettingFile("hr_mappings.json", settings.hr_mappings_config, settings.settings_dir, hr_mapping_problems,
                           audit=audit_setting, audit_action="hr.mappings")

    def hr_mappings() -> tuple[dict, list[dict]]:
        """保存した対応づけ（名前 → 対応づけ）と、読めないときの理由。読めなくても起動は止めない（画面に理由を出す）。"""
        st = hr_mappings_store()
        try:
            text = st.read_text()
        except OSError as e:
            return {}, [{"where": "", "message": f"対応づけのファイルを読めません（{e}）"}]
        found = hr_mapping_problems(text)
        if found:
            return {}, [{"where": w, "message": m} for w, m in found]
        return json.loads(text)["mappings"], []

    def hr_view_scope(user: User):
        depts = engine.departments(user.employee_no, user.dept, "hr.view")
        return None if depts == access.ALL else depts

    def hr_problems() -> list[dict]:
        """人事データについて、画面の上に出す理由（起動は止めない。今の人事データのまま動く）。"""
        out = []
        _, bad = hr_mappings()
        out += [{"where": f"対応づけ {x['where']}".strip(), "message": x["message"]} for x in bad]
        if "hr" in bad_settings:
            out += [{"where": "設定「人事」", "message": m} for _w, m in bad_settings["hr"]]
        try:
            with closing(connect()) as db:
                hrimport.ensure_schema(db)
                last = hr.last_import(db)
        except sqlite3.Error as e:
            return out + [{"where": "人事データ", "message": f"人事データを読めません（{e}）"}]
        if last is None:
            out.append({"where": "人事データ", "message": "人事データをまだ取り込んでいません"})
        return out

    @app.get("/api/hr/status")
    def hr_status(user: User = Depends(current_user)):
        if not (can(user, "hr.import") or can(user, "hr.view")):
            raise deny(user, "hr.import か hr.view", None)
        with closing(connect()) as db:
            hrimport.ensure_schema(db)
            last = hr.last_import(db)
            hist = hrimport.history(db)
            backs = hrimport.backups(db) if can(user, "hr.import") else []
        mappings, _ = hr_mappings()
        return {"last": last, "history": hist if can(user, "hr.import") else [], "backups": backs,
                "mappings": mappings if can(user, "hr.import") else {}, "problems": hr_problems(),
                "fields": {f: hr.LABELS[f] for f in hr.FIELDS}, "required": list(hr.REQUIRED), "statuses": list(hr.STATUSES),
                "leave": {"login": settings.on_leave_login, "permissions": settings.on_leave_permissions},
                "can_import": can(user, "hr.import"), "can_view": can(user, "hr.view"), "keep": hrimport.HR_KEEP}

    @app.post("/api/hr/inspect")
    def hr_inspect(req: HrInspect, user: User = Depends(require("hr.import"))):
        """上げたファイルの列名・シート・対応づけの下書き。status_column を渡すと、その列に出てくる値（50種類まで）と下書き。
        読むのは列名と在籍状態の列だけ（ほかの列の値は読まない）。"""
        path = Path(my_file(req.file_id, user, "hr")["path"])
        sheets: list[str] = []
        try:
            if path.suffix.lower() in (".xlsx", ".xlsm"):
                from openpyxl import load_workbook

                wb = load_workbook(path, read_only=True)
                sheets = list(wb.sheetnames)
                wb.close()
            heads = hrimport.headers(path, req.sheet or None)
            values: list[dict] = []
            if req.status_column:
                if req.status_column not in heads:
                    raise ApiError(400, "invalid_mapping", f"列「{req.status_column}」がファイルにありません")
                seen: dict[str, None] = {}
                for _line, r in hrimport._iter(path, [req.status_column], req.sheet or None):
                    v = r.get(req.status_column, "")
                    if v and v not in seen:
                        seen[v] = None
                        if len(seen) >= 50:
                            break
                values = [{"value": v, "guess": hr.guess_status(v)} for v in seen]
        except hr.HRImportError as e:
            raise ApiError(400, "invalid_file", str(e)) from e
        return {"headers": heads, "sheets": sheets, "draft": hr.suggest_mapping(heads)["columns"], "status_values": values}

    def check_mapping(m: HrMapping) -> dict:
        data = m.model_dump(exclude_none=True)
        found = hrimport.mapping_problems(data)
        if found:
            raise ApiError(400, "invalid_mapping", "対応づけに直すところがあります", [{"where": w, "message": msg} for w, msg in found])
        return data

    @app.put("/api/hr/mappings/{name}")
    def hr_mapping_save(name: str, req: HrMapping, user: User = Depends(require("hr.import"))):
        name = name.strip()
        if not name or len(name) > MAPPING_NAME_MAX:
            raise ApiError(400, "invalid_mapping", f"名前は1〜{MAPPING_NAME_MAX}文字にしてください",
                           [{"where": "name", "message": f"1〜{MAPPING_NAME_MAX}文字にしてください"}])
        data = check_mapping(req)
        st = hr_mappings_store()
        verb = "直した" if name in hr_mappings()[0] else "足した"

        def put(text: str) -> str:
            body = json.loads(text) if not hr_mapping_problems(text) else {"mappings": {}}
            body["mappings"][name] = data
            return json.dumps(body, ensure_ascii=False, indent=2) + "\n"
        try:
            version = st.save(put, user.employee_no, f"人事データの対応づけ「{name}」を{verb}")
        except SettingError as e:
            raise ApiError(400, "invalid_mapping", "対応づけに直すところがあります",
                           [{"where": w, "message": msg} for w, msg in e.problems]) from e
        return {"ok": True, "version": version, "mappings": hr_mappings()[0]}

    @app.delete("/api/hr/mappings/{name}")
    def hr_mapping_delete(name: str, user: User = Depends(require("hr.import"))):
        mappings, _ = hr_mappings()
        if name not in mappings:
            raise not_found("対応づけ")

        def drop(text: str) -> str:
            body = json.loads(text)
            body["mappings"].pop(name, None)
            return json.dumps(body, ensure_ascii=False, indent=2) + "\n"
        hr_mappings_store().save(drop, user.employee_no, f"人事データの対応づけ「{name}」を消した")
        return {"ok": True, "mappings": hr_mappings()[0]}

    def hr_job(ctx: JobContext, params: dict) -> dict:
        """試し（preview）・確定（commit）・前に戻す（restore、restore_preview）。"""
        mode = params["mode"]
        leave = settings.on_leave_permissions
        db = sqlite3.connect(str(settings.db_path), timeout=60)
        try:
            ctx.progress(5, "読み込み")

            def progress(rows: int) -> None:
                ctx.progress(min(60, 5 + rows // 20000), f"読み込み（{rows:,}行）")
            if mode in ("restore", "restore_preview"):
                try:
                    if mode == "restore_preview":
                        out = hrimport.preview_restore(db, leave).as_dict()
                    else:
                        with HR_LOCK, access.CHANGE_LOCK:
                            result = hrimport.restore(db, params["by"], leave, today().isoformat())
                        out = result.as_dict()
                except hrimport.NoBackup as e:
                    raise JobFailed(str(e)) from e
            else:
                f = job_file(params["owner"], params["file_id"], "hr")
                path = Path(f["path"])
                try:
                    if mode == "preview":
                        out = hrimport.preview(db, path, params["mapping"], leave, progress).as_dict()
                    else:
                        with HR_LOCK, access.CHANGE_LOCK:
                            ctx.progress(10, "確定（入れ替え）")
                            result = hrimport.commit(db, path, params["mapping"], params["by"], leave, today().isoformat(), progress)
                        out = result.as_dict()
                except hr.HRImportError as e:
                    raise JobFailed(str(e)) from e
                out["source"] = f["name"]
            if mode in ("commit", "restore"):
                source = out["source"] if mode == "commit" else "1つ前に戻す"
                for r in hrimport.audit_records(result, source):
                    auditor.record(r["action"], actor=params["by"], target=r["target"],
                                   result=audit.OK if r["result"] == "成功" else audit.FAILED, detail=r["detail"], ip="")
        finally:
            db.close()
        ctx.progress(100, "完了")
        return out

    jobs.register("hr", hr_job)

    @app.post("/api/hr/preview", status_code=202)
    def hr_preview(req: HrRun, user: User = Depends(require("hr.import"))):
        my_file(req.file_id, user, "hr")
        return jobs.submit("hr", user.employee_no, {"mode": "preview", "file_id": req.file_id, "owner": user.employee_no,
                                                    "mapping": check_mapping(req.mapping), "by": user.employee_no})

    @app.post("/api/hr/imports", status_code=202)
    def hr_commit(req: HrRun, user: User = Depends(require("hr.import"))):
        my_file(req.file_id, user, "hr")
        return jobs.submit("hr", user.employee_no, {"mode": "commit", "file_id": req.file_id, "owner": user.employee_no,
                                                    "mapping": check_mapping(req.mapping), "by": user.employee_no})

    @app.post("/api/hr/restore/preview", status_code=202)
    def hr_restore_preview(user: User = Depends(require("hr.import"))):
        return jobs.submit("hr", user.employee_no, {"mode": "restore_preview", "owner": user.employee_no, "by": user.employee_no})

    @app.post("/api/hr/restore", status_code=202)
    def hr_restore(user: User = Depends(require("hr.import"))):
        return jobs.submit("hr", user.employee_no, {"mode": "restore", "owner": user.employee_no, "by": user.employee_no})

    @app.get("/api/hr/employees")
    def hr_employees(q: str = "", dept: str = "", offset: int = 0, user: User = Depends(require("hr.view"))):
        """一覧（hr.view の対象の部署の人だけ）：社員番号・氏名・部署・在籍状態・最後に変わった日。"""
        scope = hr_view_scope(user)
        with closing(connect()) as db:
            out = hrimport.employees(db, scope, q[:100], dept[:100], max(0, offset), 100)
            out["departments"] = hrimport.department_names(db, scope)
        return out

    # ------------------------------------------------ 設定の画面（S-52。管理者だけ。K-042）

    def setting_store(key: str):
        if key not in adminsettings.SECTIONS:
            raise not_found("設定")
        return adminsettings.store(settings, key, audit=audit_setting, catalog=model_catalog)

    def setting_section(key: str) -> dict:
        st = setting_store(key)
        out = {"key": key, "label": adminsettings.LABELS[key], "status": st.status(),
               "pinned": adminsettings.pinned_fields(settings, key), "values": None, "problems": []}
        running = adminsettings.running_values(settings, key, policy_state["policy"])
        try:
            text = st.read_text()
            found = adminsettings.problems(key, text)
            if found:
                out["problems"] = [{"where": w, "message": m} for w, m in found]
            else:
                out["values"] = adminsettings.values_of(key, text)
        except OSError as e:
            out["problems"] = [{"where": "", "message": f"設定のファイルを読めません（{e}）"}]
        # 動いている値と、保存したもの（写しか既定）が同じか。違えば再起動すると使われる（写しを手で直したときなど）
        values = out["values"] or {}
        out["running"] = running
        out["applied"] = bool(values) and all(running.get(k) == v for k, v in values.items() if k not in out["pinned"])
        return out

    def setting_meta(key: str) -> dict:
        if key == "identity":
            return {"presets": {name: {"description": p["_説明"], "question": p["question"], "clues": p["clues"],
                                       "confirm": p["confirm"]} for name, p in identity.PRESETS.items()},
                    "clues": identity.MATCH_LABELS}
        if key == "models":
            # 目録にあるモデルだけ（Ollama に入っていても、目録にないものは出さない）。使えないものは理由つき
            try:
                return {"catalog": model_catalog(), "error": ""}
            except Exception as e:  # noqa: BLE001
                return {"catalog": [], "error": f"Ollama に、入っているモデルを問い合わせられません（{e}）"}
        if key in ("audit", "files"):
            return {"limits": {k.split(".", 1)[1]: {"min": lo, "max": hi, "unit": u}
                               for k, (lo, hi, u) in adminsettings.LIMITS.items() if k.startswith(f"{key}.")}}
        return {}

    def apply_setting(key: str) -> None:
        """保存・版に戻す・既定に戻すのあとで、動いている値を入れ替える（再起動しなくても効く）。"""
        text = setting_store(key).read_text()
        if key == "identity":
            policy_state["policy"] = identity.build_policy(json.loads(text))
            if hasattr(app.state, "helpdesk"):
                app.state.helpdesk.set_policy(policy_state["policy"])
            return
        adminsettings.apply_to_settings(settings, key, adminsettings.values_of(key, text))
        if key == "audit":
            auditor.keep_days, auditor.keep_body = settings.audit_keep_days, settings.audit_keep_body
        elif key == "models":
            llm.name = settings.chat_model
            if summary_llm is not llm:
                summary_llm.name = settings.summary_model

    def setting_text(key: str, values) -> str:
        """画面から来た値を確かめて、保存する中身にする。起動の指定が勝つ項目は、変えさせない。"""
        if not isinstance(values, dict):
            raise ApiError(400, "invalid_setting", "設定に直すところがあります", [{"where": "", "message": "値の形が正しくありません"}])
        pins = adminsettings.pinned_fields(settings, key)
        if pins:
            now = setting_section(key)["values"] or {}
            locked = [{"where": f, "message": f"起動の指定（{flag}）が勝っているので、画面では変えられません"}
                      for f, flag in pins.items() if f in values and values[f] != now.get(f)]
            if locked:
                raise ApiError(400, "invalid_setting", "設定に直すところがあります", locked)
        text = adminsettings.text_of(key, values)
        found = adminsettings.problems(key, text, model_catalog)
        if found:
            raise ApiError(400, "invalid_setting", "設定に直すところがあります", [{"where": w, "message": m} for w, m in found])
        return text

    @app.get("/api/admin/settings")
    def settings_list(user: User = Depends(require("settings.view"))):
        sections = [setting_section(k) for k in adminsettings.SECTIONS]
        return {"sections": sections, "pending_restart": [s["key"] for s in sections if not s["applied"]],
                "links": [{"label": "PDF の仕分けルール", "path": "/paper/sort", "note": "仕分けルールは「PDF の仕分け」の画面で直します"}]}

    @app.get("/api/admin/settings/{key}")
    def settings_get(key: str, user: User = Depends(require("settings.view"))):
        out = setting_section(key)
        versions = setting_store(key).history()
        with closing(connect()) as db:
            names = {no: ((hr.lookup(db, no) or {}).get("name") or no) for no in {v["saved_by"] for v in versions}}
        return {**out, "meta": setting_meta(key), "current": out["status"]["current_version"],
                "versions": [{**v, "saved_by_name": names.get(v["saved_by"], v["saved_by"])} for v in versions]}

    @app.post("/api/admin/settings/{key}/validate")
    def settings_validate(key: str, req: SettingValues, user: User = Depends(require("settings.edit"))):
        setting_store(key)
        setting_text(key, req.values)
        return {"ok": True}

    @app.put("/api/admin/settings/{key}")
    def settings_save(key: str, req: SettingValues, user: User = Depends(require("settings.edit"))):
        st = setting_store(key)
        text = setting_text(key, req.values)
        changes = adminsettings.describe_changes(key, setting_section(key)["values"], adminsettings.values_of(key, text))
        try:
            version = st.save(text, user.employee_no, f"{adminsettings.LABELS[key]}を保存した（{changes}）")
        except SettingError as e:
            raise setting_error(e, 400, "invalid_setting", "設定に直すところがあります") from e
        apply_setting(key)
        return {"version": version, **setting_section(key)}

    @app.post("/api/admin/settings/{key}/history/{version}/restore")
    def settings_restore(key: str, version: int, user: User = Depends(require("settings.edit"))):
        st = setting_store(key)
        try:
            note = adminsettings.describe_changes(key, setting_section(key)["values"], adminsettings.values_of(key, st.get(version) or ""))
        except (ValueError, KeyError, identity.PolicyError):
            note = ""                    # その版が読めないとき（戻すときの検証で断る）
        try:
            new = st.restore(version, user.employee_no, note)
        except KeyError as e:
            raise not_found("設定の版") from e
        except SettingError as e:
            raise setting_error(e, 409, "invalid_version", "この版は、今の検証を通らないので戻せません") from e
        apply_setting(key)
        return {"version": new, **setting_section(key)}

    @app.post("/api/admin/settings/{key}/reset")
    def settings_reset(key: str, user: User = Depends(require("settings.edit"))):
        st = setting_store(key)
        try:
            note = adminsettings.describe_changes(key, setting_section(key)["values"],
                                                  adminsettings.values_of(key, st.default.read_text(encoding="utf-8")))
        except (OSError, ValueError, KeyError, identity.PolicyError):
            note = ""
        try:
            new = st.reset_to_default(user.employee_no, note if st.saved.is_file() else "")
        except SettingError as e:
            raise setting_error(e, 409, "invalid_default", "既定の設定が今の検証を通らないので、戻せません。管理者に連絡してください") from e
        if new is None:
            return {"version": None, "already_default": True, "message": "すでに既定です", **setting_section(key)}
        apply_setting(key)
        return {"version": new, "already_default": False, **setting_section(key)}

    @app.post("/api/admin/settings/identity/try")
    def settings_identity_try(req: IdentityTry, user: User = Depends(require("settings.view"))):
        # 保存前の方針で、言い方を1つ試す。保存しない。人事データは本物を使うが、特定できたかと聞き返す項目だけを返す
        problems = identity.policy_problems(req.values)
        if problems:
            raise ApiError(400, "invalid_setting", "設定に直すところがあります", [{"where": w, "message": m} for w, m in problems])
        policy = identity.build_policy(req.values)
        with closing(connect()) as db:
            r = identity.check(db, [req.said], policy)
        ok = r.person is not None
        audit_by(user, "settings.identity.try", target="identity",
                 detail=f"本人確認の方針を試しました（{'特定できた' if ok else '特定できなかった'}）")   # 試した言い方は残さない
        return {"identified": ok, "matched": r.matched if ok else [],
                "ask": [identity.LABELS[a] for a in r.ask] if not ok else [],
                "question": r.question() if not ok and r.ask else "", "note": r.note}

    # ------------------------------------------------ 通信とモデル（S-53。管理者だけ。K-052）

    now = clock or (lambda: datetime.now().astimezone())
    check_state = {"last": None, "started": now()}      # 予定の確認の、前に見に行った時刻（起動した時刻から数える）
    CHECK_FILE = "network_check.json"

    def init_network() -> None:
        """初めて入れたとき（覚えた既定がなく、写しもないとき）：今の既定の宛先をすべて ON で覚える。版は増やさない
        （画面は「既定」を使っている）。このあと増えた既定は OFF で加わる。"""
        defaults, error = net_defaults()
        if defaults is None:
            log.error("通信の設定を読めません。社外には通信しません：%s", error)
            return
        st = netsettings.store(settings, defaults["allowed_hosts"])
        if st.saved.is_file() or read_known() is not None:
            return
        write_known(defaults["allowed_hosts"])
        auditor.record("settings.network.init", actor=audit.SYSTEM, actor_name=audit.SYSTEM, kind=audit.SYSTEM, ip="",
                       detail=f"初めての起動：既定の宛先をすべて ON にした（{'、'.join(defaults['allowed_hosts'])}）")

    def last_check() -> dict | None:
        p = settings.settings_dir / CHECK_FILE
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
        except (OSError, ValueError):
            return None

    def save_check(data: dict) -> None:
        settings.settings_dir.mkdir(parents=True, exist_ok=True)
        tmp = settings.settings_dir / f".{CHECK_FILE}.tmp"
        tmp.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(settings.settings_dir / CHECK_FILE)

    def run_check(actor: str, actor_name: str, kind: str) -> dict:
        """更新の確認：ON の宛先だけを見に行く。新しい版があっても入れ替えない（知らせるだけ）。結果は監査ログに残す。"""
        policy = net_policy()
        results = []
        for name, spec in (policy.models.items() if policy else []):
            kind_m = models.kind_of(spec)
            row = {"name": name, "kind": "ollama" if kind_m == models.OLLAMA else "whisper", "result": "skipped",
                   "current": "", "latest": "", "message": ""}
            if kind_m == models.OLLAMA:
                host, url = "registry.ollama.ai", "https://registry.ollama.ai/"
                lock = models.read_lock(settings.models_dir).get(name, {})
                row["current"] = lock.get("digest", "")
                if not policy.allows(url):
                    row["message"] = "registry.ollama.ai を追加して ON にしたときだけ、入手元と照らします"
                else:
                    try:
                        src = models.ollama_source_digest(name, policy)
                        row["latest"] = src
                        if not row["current"]:
                            row.update(result="not_registered", message="登録されていません（models pull で登録してください）")
                        elif src != row["current"]:
                            row.update(result="differs", message="入手元と違います（手で ollama pull → models pull --update）")
                        else:
                            row.update(result="same", message="入手元と同じです")
                    except Exception as e:  # noqa: BLE001
                        row.update(result="error", message=f"見に行けませんでした（{e}）")
            else:
                host = "huggingface.co"
                row["current"] = spec.get("revision", "")
                if not policy.allows(f"https://{host}/"):
                    row["message"] = f"{host} が OFF なので、見に行きませんでした"
                else:
                    try:
                        latest = models.hf_latest(spec["repo"], policy)
                        row["latest"] = latest
                        if latest != row["current"]:
                            row.update(result="new", message=f"新しい版があります（版 {latest[:12]}）。入れ替えるときは「入れ替える」を押してください")
                        else:
                            row.update(result="same", message="今の版が最新です")
                    except Exception as e:  # noqa: BLE001
                        row.update(result="error", message=f"見に行けませんでした（{e}）")
            if row["result"] != "skipped":
                auditor.record("network.check", actor=actor, actor_name=actor_name, kind=kind, target=name,
                               result=audit.FAILED if row["result"] == "error" else audit.OK,
                               detail=f"宛先 {host}：{row['message']}" + (f"（版 {row['latest'][:12]}）" if row["latest"] else ""))
            results.append(row)
        data = {"checked_at": now().isoformat(timespec="seconds"), "by": actor_name, "results": results}
        save_check(data)
        check_state["last"] = now()
        return data

    def run_scheduled_check() -> bool:
        """予定の時刻を過ぎていたら見に行く（「しない」「手動だけ」なら、決して見に行かない）。見に行ったら True。"""
        uc = net_state()["update_check"]
        last = check_state["last"] or check_state["started"]
        if not netsettings.due(uc, last, now()):
            return False
        run_check(audit.SYSTEM, audit.SYSTEM, audit.SYSTEM)
        return True

    app.state.run_scheduled_check = run_scheduled_check

    def net_section() -> dict:
        st = net_store()
        state = net_state()
        versions = st.history()
        with closing(connect()) as db:
            names = {no: ((hr.lookup(db, no) or {}).get("name") or no) for no in {v["saved_by"] for v in versions}}
        return {**net_view(), "update_check": state["update_check"], "schedule": netsettings.schedule_text(state["update_check"]),
                "modes": netsettings.MODES, "status": st.status(), "current": st.status()["current_version"],
                "versions": [{**v, "saved_by_name": names.get(v["saved_by"], v["saved_by"])} for v in versions],
                "last_check": last_check()}

    def net_text(body: dict) -> tuple[str, dict, dict]:
        defaults, error = net_defaults()
        if defaults is None:
            raise ApiError(409, "network_broken", f"通信の設定（config/network.json）が読めないので、変えられません：{error}")
        old = net_state()
        if not isinstance(body, dict):
            body = {}
        wrong = []
        for host, v in (body.get("defaults") or {}).items():
            if host not in defaults["allowed_hosts"]:
                wrong.append({"where": f"defaults.{host}", "message": f"{host} は既定の宛先ではありません（足すときは、追加の宛先にしてください）"})
            elif not isinstance(v, bool):
                wrong.append({"where": f"defaults.{host}", "message": f"{host} は既定の宛先なので消せません。使わないときは OFF にしてください"})
        if wrong:
            raise ApiError(400, "invalid_setting", "設定に直すところがあります", wrong)
        new = netsettings.from_screen(defaults, old, body, read_known())
        text = netsettings.text_of(new)
        found = netsettings.state_problems(text, defaults["allowed_hosts"])
        if found:
            raise ApiError(400, "invalid_setting", "設定に直すところがあります", [{"where": w, "message": m} for w, m in found])
        return text, old, new

    @app.get("/api/admin/network")
    def network_get(user: User = Depends(require("network.view"))):
        return net_section()

    @app.post("/api/admin/network/validate")
    def network_validate(req: SettingValues, user: User = Depends(require("network.edit"))):
        net_text(req.values)
        return {"ok": True}

    @app.put("/api/admin/network")
    def network_save(req: SettingValues, user: User = Depends(require("network.edit"))):
        text, old, new = net_text(req.values)
        defaults = net_defaults()[0]
        changes = netsettings.describe_changes(defaults, old, netsettings.read_state_text(text), read_known())
        try:
            version = net_store().save(text, user.employee_no, f"通信の設定を保存した（{changes}）")
        except SettingError as e:
            raise setting_error(e, 400, "invalid_setting", "設定に直すところがあります") from e
        write_known(defaults["allowed_hosts"])     # 画面で見た既定の宛先は、知ったことにする
        return {"version": version, **net_section()}

    @app.post("/api/admin/network/history/{version}/restore")
    def network_restore(version: int, user: User = Depends(require("network.edit"))):
        st = net_store()
        try:
            note = netsettings.describe_changes(net_defaults()[0], net_state(), netsettings.read_state_text(st.get(version) or ""), read_known())
        except (ValueError, KeyError, AttributeError):
            note = ""
        try:
            new = st.restore(version, user.employee_no, note)
        except KeyError as e:
            raise not_found("設定の版") from e
        except SettingError as e:
            raise setting_error(e, 409, "invalid_version", "この版は、今の検証を通らないので戻せません") from e
        return {"version": new, **net_section()}

    @app.post("/api/admin/network/reset")
    def network_reset(user: User = Depends(require("network.edit"))):
        # 既定に戻す：既定の宛先をすべて ON、追加の宛先を消す、更新の確認は手動だけ（入れ替えた版も既定の版に戻る）
        st = net_store()
        defaults = net_defaults()[0]
        note = (netsettings.describe_changes(defaults, net_state(), netsettings.first_state(defaults), read_known(),
                                             defaults["allowed_hosts"]) if defaults and st.saved.is_file() else "")
        try:
            new = st.reset_to_default(user.employee_no, note)
        except SettingError as e:
            raise setting_error(e, 409, "invalid_default", "既定の設定が今の検証を通らないので、戻せません。管理者に連絡してください") from e
        if new is None:
            return {"version": None, "already_default": True, "message": "すでに既定です", **net_section()}
        if defaults:
            # 同じ操作の中で、今の既定をすべて ON で覚え直す（版は増やさない。このあと増えた既定は OFF で加わる）
            write_known(defaults["allowed_hosts"])
        return {"version": new, "already_default": False, **net_section()}

    @app.post("/api/admin/network/check")
    def network_check(user: User = Depends(require("network.edit"))):
        if net_state()["update_check"]["mode"] == "none":
            raise ApiError(409, "check_disabled", "更新の確認は「しない」になっています。見に行くときは、予定を「手動だけ」などに変えてください")
        return run_check(user.employee_no, user.name, audit.EMPLOYEE)

    def model_rows() -> dict:
        policy = net_policy()
        defaults, error = net_defaults()
        catalog = (policy.models if policy else (defaults or {}).get("models", {}))
        lock = models.read_lock(settings.models_dir)
        verified = (last_check_file("models_verify.json") or {}).get("results", {})
        latest = {r["name"]: r for r in (last_check() or {}).get("results", [])}
        try:
            tags, tags_error = local_tags(), ""
        except Exception as e:  # noqa: BLE001
            tags, tags_error = None, f"Ollama につながりません（{e}）"
        rows = []
        for name, spec in catalog.items():
            entry = lock.get(name, {})
            row = {"name": name, "use": spec.get("use", []), "latest": latest.get(name, {}).get("latest", ""),
                   "check": latest.get(name, {}).get("result", ""), "check_message": latest.get(name, {}).get("message", "")}
            if models.kind_of(spec) == models.OLLAMA:
                row.update(kind="ollama", revision="", recorded=entry.get("digest", ""))
                if tags is None:
                    row.update(state="unknown", message=tags_error)
                else:
                    state, message = models.ollama_state(name, settings.models_dir, tags)
                    row.update(state={"ok": "match", "changed": "differs"}.get(state, state), message=message)
            else:
                row.update(kind="whisper", revision=spec.get("revision", ""),
                           recorded=(entry.get("files", {}).get("model.bin") or ""), files=len(entry.get("files", {})))
                if not entry:
                    row.update(state="not_registered", message=f"入手していません（python -m yanua models pull {name}）")
                elif not (settings.models_dir / entry.get("path", name)).is_dir():
                    row.update(state="not_installed", message="記録はありますが、ファイルがありません")
                elif row["revision"] and entry.get("revision") != row["revision"]:
                    row.update(state="differs", message=f"入手した版（{entry.get('revision', '')[:12]}）が、固定した版と違います")
                else:
                    v = verified.get(name)
                    row.update(state=v["state"] if v else "unverified", message=v["message"] if v else "まだ照合していません（「照合する」）")
                    if v:
                        row["verified_at"] = v["at"]
            rows.append(row)
        return {"models": rows, "error": error}

    def last_check_file(name: str) -> dict | None:
        p = settings.settings_dir / name
        try:
            return json.loads(p.read_text(encoding="utf-8")) if p.is_file() else None
        except (OSError, ValueError):
            return None

    @app.get("/api/admin/models")
    def models_get(user: User = Depends(require("network.view"))):
        return model_rows()

    @app.post("/api/admin/models/verify")
    def models_verify(user: User = Depends(require("network.edit"))):
        """手元のファイル（whisper）と記録を照らす。通信しない。Ollama のモデルは、手元の Ollama の digest と照らす。"""
        policy = net_policy()
        defaults = net_defaults()[0] or {}
        catalog = policy.models if policy else defaults.get("models", {})
        results = {}
        for name, spec in catalog.items():
            if models.kind_of(spec) == models.OLLAMA:
                continue                      # 一覧で、手元の Ollama の digest と毎回照らしている
            try:
                models.verify(name, settings.models_dir, revision=spec.get("revision") or None)
                results[name] = {"state": "match", "message": "記録と一致しました", "at": now().isoformat(timespec="seconds")}
            except models.ModelError as e:
                if "入手していません" in str(e):
                    continue
                results[name] = {"state": "differs", "message": str(e), "at": now().isoformat(timespec="seconds")}
            audit_by(user, "models.verify", target=name, result=audit.OK if results[name]["state"] == "match" else audit.FAILED,
                     detail=results[name]["message"])
        data = {"results": results}
        settings.settings_dir.mkdir(parents=True, exist_ok=True)
        (settings.settings_dir / "models_verify.json").write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        return model_rows()

    def model_replace_job(ctx: JobContext, params: dict) -> dict:
        name, revision = params["name"], params["revision"]
        policy = net_policy()
        if policy is None:
            raise JobFailed("通信の設定（config/network.json）が読めないので、入れ替えられません")
        ctx.progress(10, "新しい版を入手しています")
        try:
            entry = models.replace(name, revision, policy, settings.models_dir, downloader=model_downloader)
        except (models.ModelError, NetworkBlocked, ConnectionError) as e:
            auditor.record("models.replace", actor=params["owner"], target=name, result=audit.FAILED,
                           detail=f"版 {revision[:12]} に入れ替えられませんでした（元のモデルのまま）：{e}")
            raise JobFailed(f"入れ替えられませんでした。元のモデルのままです（{e}）") from e
        ctx.progress(80, "記録を更新しています")
        st = net_store()
        state = net_state()
        old_rev = policy.models[name].get("revision", "")
        state["revisions"][name] = revision
        st.save(netsettings.text_of(state), params["owner"], f"{name} を版 {revision[:12]} に入れ替えた（前の版 {old_rev[:12]}）")
        auditor.record("models.replace", actor=params["owner"], target=name,
                       detail=f"版 {old_rev[:12]} → {revision[:12]} に入れ替えました（入手元のハッシュと照合済み、ファイル {len(entry['files'])} 件）")
        ctx.progress(100, "完了")
        return {"name": name, "revision": revision, "files": len(entry["files"])}

    jobs.register("model-replace", model_replace_job)

    @app.post("/api/admin/models/{name}/replace", status_code=202)
    def models_replace(name: str, user: User = Depends(require("network.edit"))):
        # 入れ替えは、管理者が押したときだけ。「更新の確認」で見つけた新しい版にする
        row = next((r for r in (last_check() or {}).get("results", []) if r["name"] == name), None)
        if not row or row.get("result") != "new" or row.get("kind") != "whisper":
            raise ApiError(409, "no_new_version", "入れ替える新しい版がありません。先に「更新の確認」をしてください")
        return jobs.submit("model-replace", user.employee_no, {"owner": user.employee_no, "name": name, "revision": row["latest"]})

    # ------------------------------------------------ 役割と権限（S-54。画面は /admin/access）

    def need(args: dict, key: str) -> str:
        v = args.get(key)
        if isinstance(v, int) and not isinstance(v, bool):
            return v
        if not isinstance(v, str) or not v.strip() or len(v) > 128:
            raise ApiError(400, "invalid_request", f"{key} を指定してください")
        return v.strip()

    def body(model, args: dict):
        try:
            return model.model_validate(args)
        except ValidationError as e:
            raise ApiError(400, "invalid_request", "リクエストの形が正しくありません") from e

    def hosts_now() -> list[str]:
        return [h["host"] for h in net_view()["hosts"]]

    def b_member_add(db, data, by, a):
        m = body(MemberAdd, a)
        return access.member_add(db, data, by, need(a, "group_id"), m.account_id.strip(), (m.expires_at or "").strip() or None)

    def b_grant_add(db, data, by, a):
        g = body(GrantAdd, a)
        return access.grant_add(db, data, by, need(a, "group_id"), g.role_id, g.target_id, g.set_id)

    def b_role_update(db, data, by, a):
        r = body(RoleUpdate, a)
        return access.role_update(db, data, by, need(a, "role_id"), r.permissions, r.description)

    def b_role_create(db, data, by, a):
        r = body(RoleCreate, a)
        return access.role_create(db, data, by, r.name, r.description, r.permissions)

    def b_group_create(db, data, by, a):
        g = body(GroupCreate, a)
        return access.group_create(db, data, by, g.name, g.description, g.rule)

    def b_group_update(db, data, by, a):
        return access.group_update(db, data, by, need(a, "group_id"), body(GroupUpdate, a).description)

    def b_target_create(db, data, by, a):
        t = body(TargetCreate, a)
        return access.target_create(db, data, by, t.kind, t.name, t.description, t.host, t.secret_ref, hosts_now())

    def b_target_update(db, data, by, a):
        t = body(TargetUpdate, a)
        return access.target_update(db, data, by, need(a, "target_id"), t.description, t.host, t.secret_ref, hosts_now())

    def b_set_create(db, data, by, a):
        s = body(SetBody, a)
        return access.set_create(db, data, by, s.name, s.members)

    def b_set_update(db, data, by, a):
        return access.set_update(db, data, by, need(a, "set_id"), body(SetBody, a).members)

    # 操作の名前（監査ログは roles.<名前>）→ 変更の作り方。画面の「試しに当てる」も、保存も、同じものを使う
    BUILDERS = {
        "member.add": b_member_add,
        "member.remove": lambda db, data, by, a: access.member_remove(db, data, by, need(a, "group_id"), need(a, "account_id")),
        "grant.add": b_grant_add,
        "grant.remove": lambda db, data, by, a: access.grant_remove(db, data, by, int(need(a, "grant_id"))),
        "role.create": b_role_create,
        "role.update": b_role_update,
        "role.delete": lambda db, data, by, a: access.role_delete(db, data, by, need(a, "role_id")),
        "group.create": b_group_create,
        "group.update": b_group_update,
        "group.delete": lambda db, data, by, a: access.group_delete(db, data, by, need(a, "group_id")),
        "target.create": b_target_create,
        "target.update": b_target_update,
        "target.delete": lambda db, data, by, a: access.target_delete(db, data, by, need(a, "target_id")),
        "set.create": b_set_create,
        "set.update": b_set_update,
        "set.delete": lambda db, data, by, a: access.set_delete(db, data, by, need(a, "set_id")),
    }

    def denied_groups(user: User, change: access.Change, data: dict) -> list[str]:
        """roles.assign がそのグループに効いていないもの（グループ名）。"""
        return [data["groups"].get(g, {}).get("name", g) for g in sorted(change.groups)
                if not engine.can(user.employee_no, user.dept, "roles.assign", ("group", g)).allowed]

    def audit_target(args: dict) -> str:
        for key in ("group_id", "role_id", "target_id", "set_id", "grant_id", "name"):
            if args.get(key) not in (None, ""):
                return str(args[key])[:100]
        return ""

    def access_change(user: User, op: str, args: dict):
        """権限の変更：変更を作り、roles.assign がそのグループに効くか確かめ、決まりに当てて保存し、中身つきで監査ログに残す。
        画面専用の抜け道は作らない（画面もこの API を使う）。"""
        action = f"roles.{op}"
        try:
            with access.CHANGE_LOCK, closing(connect()) as db:
                data = access.load(db)
                change = BUILDERS[op](db, data, user.employee_no, args)
                missing = denied_groups(user, change, data)
                if missing:
                    raise deny(user, f"roles.assign（グループ {'・'.join(missing)}）")
                detail = access.apply(db, engine, user.employee_no, user.dept, data, change)
        except KeyError as e:
            raise not_found("権限の設定") from e
        except access.AccessError as e:
            auditor.record(action, actor=user.employee_no, actor_name=user.name, target=audit_target(args), result=audit.DENIED,
                           detail=f"変えませんでした：{e}")
            raise ApiError(e.status, "invalid_access", str(e),
                           [{"where": w, "message": m} for w, m in e.problems]) from e
        audit_by(user, change.action, target=change.target, detail=detail)
        return {"ok": True, "detail": detail, **access_view()}

    def access_view() -> dict:
        from ..permissions import PERMISSIONS, TARGET_KINDS

        with closing(connect()) as db:
            access.sync_targets(db)          # 部署（人事データ）・グループ・機能の対象を、自動でそろえる
            data = access.load(db)
            names = {m["account_id"]: (hr.lookup(db, m["account_id"]) or {}).get("name") for m in data["members"]}
            counts = {gid: access._rule_count(db, g["rule"]) for gid, g in data["groups"].items()}
            stale = access.stale_targets(db, data)
            depts = access._hr_departments(db)
        today = datetime.now().astimezone().date().isoformat()
        used = access._used_targets(data)
        groups = []
        for gid, g in data["groups"].items():
            members = [{**m, "name": names.get(m["account_id"]) or data["accounts"].get(m["account_id"], {}).get("name") or m["account_id"],
                        "in_hr": bool(names.get(m["account_id"])) or m["account_id"].startswith("sys:"),
                        "expired": bool(m["expires_at"]) and m["expires_at"][:10] < today}
                       for m in data["members"] if m["group_id"] == gid]
            grants = [{**gr, "role": data["roles"][gr["role_id"]]["name"],
                       "target": (data["targets"][gr["target_id"]]["name"] if gr["target_id"] else data["sets"][gr["set_id"]]["name"]),
                       "target_kind": data["targets"][gr["target_id"]]["kind"] if gr["target_id"] else "set"}
                      for gr in data["grants"] if gr["group_id"] == gid]
            groups.append({**g, "how": "rule" if g["rule"] else "manual", "rule_text": access._rule_text(g["rule"]),
                           "rule_count": counts[gid], "member_count": counts[gid] + len(members),
                           "members": members, "grants": grants})
        roles = [{**r, "used_by": sorted({data["groups"][x["group_id"]]["name"] for x in data["grants"] if x["role_id"] == rid})}
                 for rid, r in data["roles"].items()]
        targets = [{**t, "kind_name": access.SPECIAL_NAMES.get(t["target_id"]) and "特別" or TARGET_KINDS.get(t["kind"], t["kind"]),
                    "auto": t["kind"] in access.AUTO_KINDS or t["kind"] == "special", "used": tid in used, "stale": tid in stale}
                   for tid, t in data["targets"].items()]
        sets = [{**s, "used_by": sorted({data["groups"][x["group_id"]]["name"] for x in data["grants"] if x["set_id"] == sid})}
                for sid, s in data["sets"].items()]
        return {"permissions": [{"name": p.name, "feature": p.feature, "does": p.does,
                                 "targets": [TARGET_KINDS[t] for t in p.targets]} for p in PERMISSIONS.values()],
                "roles": roles, "groups": groups, "targets": targets, "target_sets": sets,
                "accounts": list(data["accounts"].values()), "departments": depts, "hosts": hosts_now()}

    @app.get("/api/admin/access")
    def access_get(user: User = Depends(require("roles.view"))):
        return access_view()

    @app.get("/api/admin/access/people")
    def access_people(q: str = "", user: User = Depends(require("roles.view"))):
        # 社員番号か名前の一部で探す（メンバーを足すとき・「この人は何ができるか」）。システムアカウントも出す
        with closing(connect()) as db:
            return {"people": access.find_people(db, access.load(db), q)}

    @app.get("/api/admin/access/explain")
    def access_explain(q: str = "", user: User = Depends(require("roles.view"))):
        """「この人は何ができるか」。q は画面の欄と同じ：社員番号・名前の一部・システムアカウント（sys:…）。
        1人に決まれば result に権限と出どころ（どのグループのどの役割から・対象・期限）。決まらなければ候補だけ。"""
        with closing(connect()) as db:
            data = access.load(db)
            q = q.strip()[:50]
            exact = access.account_of(db, data, q) if q else None
            candidates = [exact] if exact else access.find_people(db, data, q)
        result = None
        if len(candidates) == 1:
            who = candidates[0]
            result = {**who, "note": access.SYSTEM_NOTES.get(who["account_id"], ""),
                      "groups": engine.groups_of(who["account_id"], who["dept"]),
                      "permissions": access.explain(engine, who["account_id"], who["dept"])}
        return {"q": q, "candidates": candidates, "result": result}

    @app.post("/api/admin/access/preview")
    def access_preview(req: AccessPreview, user: User = Depends(require("roles.assign"))):
        """変更を試しに当てて、だれのどの権限が増える・減るかを返す（保存しない。監査ログにも残さない）。
        args は保存のときの API と同じ中身（パスの番号も入れる。例 {"group_id": "G-audit", "account_id": "10004"}）。"""
        if req.op not in BUILDERS:
            raise ApiError(400, "invalid_request", f"知らない操作です: {req.op}")
        empty = {"self": {"gained": [], "lost": []}, "self_loss": False, "people": [], "checked": 0, "too_many": False, "groups": []}
        with access.CHANGE_LOCK, closing(connect()) as db:
            data = access.load(db)
            try:
                change = BUILDERS[req.op](db, data, user.employee_no, req.args)
            except KeyError as e:
                raise not_found("権限の設定") from e
            except access.AccessError as e:
                return {**empty, "detail": None, "refused": str(e), "problems": [{"where": w, "message": m} for w, m in e.problems]}
            missing = denied_groups(user, change, data)
            if missing:
                return {**empty, "detail": change.detail, "problems": [],
                        "refused": f"グループ「{'・'.join(missing)}」を変える権限（roles.assign）がありません"}
            return {**access.preview(db, engine, user.employee_no, user.dept, data, change), "problems": []}

    @app.post("/api/admin/access/groups", status_code=201)
    def access_group_create(req: GroupCreate, user: User = Depends(require("roles.assign"))):
        return access_change(user, "group.create", req.model_dump())

    @app.patch("/api/admin/access/groups/{group_id}")
    def access_group_update(group_id: str, req: GroupUpdate, user: User = Depends(require("roles.assign"))):
        return access_change(user, "group.update", {**req.model_dump(), "group_id": group_id})

    @app.delete("/api/admin/access/groups/{group_id}")
    def access_group_delete(group_id: str, user: User = Depends(require("roles.assign"))):
        return access_change(user, "group.delete", {"group_id": group_id})

    @app.post("/api/admin/access/groups/{group_id}/members", status_code=201)
    def access_member_add(group_id: str, req: MemberAdd, user: User = Depends(require("roles.assign"))):
        return access_change(user, "member.add", {**req.model_dump(), "group_id": group_id})

    @app.delete("/api/admin/access/groups/{group_id}/members/{account_id}")
    def access_member_remove(group_id: str, account_id: str, user: User = Depends(require("roles.assign"))):
        return access_change(user, "member.remove", {"group_id": group_id, "account_id": account_id})

    @app.post("/api/admin/access/groups/{group_id}/grants", status_code=201)
    def access_grant_add(group_id: str, req: GrantAdd, user: User = Depends(require("roles.assign"))):
        return access_change(user, "grant.add", {**req.model_dump(), "group_id": group_id})

    @app.delete("/api/admin/access/grants/{grant_id}")
    def access_grant_remove(grant_id: int, user: User = Depends(require("roles.assign"))):
        return access_change(user, "grant.remove", {"grant_id": grant_id})

    @app.post("/api/admin/access/roles", status_code=201)
    def access_role_create(req: RoleCreate, user: User = Depends(require("roles.assign"))):
        return access_change(user, "role.create", req.model_dump())

    @app.put("/api/admin/access/roles/{role_id}")
    def access_role_update(role_id: str, req: RoleUpdate, user: User = Depends(require("roles.assign"))):
        return access_change(user, "role.update", {**req.model_dump(), "role_id": role_id})

    @app.delete("/api/admin/access/roles/{role_id}")
    def access_role_delete(role_id: str, user: User = Depends(require("roles.assign"))):
        return access_change(user, "role.delete", {"role_id": role_id})

    @app.post("/api/admin/access/targets", status_code=201)
    def access_target_create(req: TargetCreate, user: User = Depends(require("roles.assign"))):
        return access_change(user, "target.create", req.model_dump())

    @app.patch("/api/admin/access/targets/{target_id}")
    def access_target_update(target_id: str, req: TargetUpdate, user: User = Depends(require("roles.assign"))):
        return access_change(user, "target.update", {**req.model_dump(), "target_id": target_id})

    @app.delete("/api/admin/access/targets/{target_id}")
    def access_target_delete(target_id: str, user: User = Depends(require("roles.assign"))):
        return access_change(user, "target.delete", {"target_id": target_id})

    @app.post("/api/admin/access/target-sets", status_code=201)
    def access_set_create(req: SetBody, user: User = Depends(require("roles.assign"))):
        return access_change(user, "set.create", req.model_dump())

    @app.put("/api/admin/access/target-sets/{set_id}")
    def access_set_update(set_id: str, req: SetBody, user: User = Depends(require("roles.assign"))):
        return access_change(user, "set.update", {**req.model_dump(), "set_id": set_id})

    @app.delete("/api/admin/access/target-sets/{set_id}")
    def access_set_delete(set_id: str, user: User = Depends(require("roles.assign"))):
        return access_change(user, "set.delete", {"set_id": set_id})

    # ------------------------------------------------ 監査ログ（S-55。管理者だけ）

    @app.get("/api/audit")
    def audit_list(since: str | None = None, until: str | None = None, actor: str = "", action: str = "", result: str = "",
                   page: int = 1, per_page: int = 50, user: User = Depends(require("audit.view"))):
        if result and result not in audit.RESULTS:
            raise ApiError(400, "invalid_result", f"結果は {'・'.join(audit.RESULTS)} のどれかで指定してください")
        page, per_page = max(1, page), max(1, min(per_page, 200))
        lo, hi = audit_bound(since, False), audit_bound(until, True)
        entries, total = auditor.query(since=lo, until=hi, actor=actor.strip()[:100], action=action.strip()[:100],
                                       result=result, page=page, per_page=per_page)
        # 見たことも記録する（どんな条件で見たか）
        shown = "、".join(f"{k}={v}" for k, v in (("期間の始め", since), ("期間の終わり", until), ("した人", actor.strip()),
                                                  ("操作", action.strip()), ("結果", result)) if v) or "条件なし"
        audit_by(user, "audit.view", detail=f"監査ログを見ました（{shown}、{page}ページ目）")
        return {"entries": entries, "total": total, "page": page, "per_page": per_page, "actions": auditor.actions(),
                "keep_days": settings.audit_keep_days, "keep_body": settings.audit_keep_body}

    # ------------------------------------------------ Web画面（最後に登録する）

    install_web(app, settings.web_dist)
    return app


# 社内の画面の HTML に付けるヘッダー。ほかのサイトに埋め込ませない・ほかの宛先から読み込ませない
WEB_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; img-src 'self' data:; style-src 'self'; script-src 'self'; "
                               "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'self'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}


def install_web(app: FastAPI, dist: Path | None) -> None:
    """組み立てた画面（web/dist）を同じサーバーから配る。

    - /assets/... などのファイルはそのまま返す。dist の外のファイルは返さない
    - それ以外の画面の URL（/、/chat など）は index.html を返し、画面の側で切り替える
    - /kiosk と /kiosk/... は受付端末の画面（kiosk.html）だけを返す。社内の画面（index.html）は返さない。
      受付端末の画面は別に組み立てるので、社内の画面のコードを含まない
    - /api/... は画面を返さない
    """
    if not dist or not (Path(dist) / "index.html").is_file():
        return
    root = Path(dist).resolve()
    index = root / "index.html"
    kiosk_page = root / "kiosk.html"

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def web(path: str):
        first = path.split("/", 1)[0]
        if first in ("api", "health"):
            raise not_found()
        if first == "kiosk":
            if "." in path.rsplit("/", 1)[-1] or not kiosk_page.is_file():
                raise not_found()
            return FileResponse(kiosk_page, headers={**WEB_HEADERS, "Cache-Control": "no-cache"})
        if path:
            f = (root / path).resolve()
            if f != root and f.is_relative_to(root) and f.is_file():
                # 組み立てたファイルは名前にハッシュが付くので、長く覚えさせてよい
                cache = "public, max-age=31536000, immutable" if first == "assets" else "no-cache"
                return FileResponse(f, headers={**WEB_HEADERS, "Cache-Control": cache})
            if "." in path.rsplit("/", 1)[-1]:
                raise not_found()   # ないファイル（.js など）に index.html を返さない
        return FileResponse(index, headers={**WEB_HEADERS, "Cache-Control": "no-cache"})
