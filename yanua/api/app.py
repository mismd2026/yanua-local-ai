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
from datetime import date

Date = date   # 引数名 date（?date=...）に隠されないための別名
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, Response as RawResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .. import hr, models, visits
from ..answer import answer_question
from ..llm import ChatModel, LLMError, OllamaChat
from ..search import search
from ..store import Store
from . import auth, desk, errors, files, kiosk, meetings, paperjobs
from .auth import User
from .chat import ChatDB, HelpdeskSessions, ReceptionSessions
from .errors import ApiError, forbidden, not_found, not_logged_in
from ..paper.extract import OCRUnavailable
from .jobs import FINISHED, JobContext, JobFailed, JobQueue
from ..identity import load_policy
from .roles import allowed, load_roles, permissions_of
from .settings import Settings

CSRF_HEADER = "X-Yanua-Client"
MAX_QUESTION = 2000
STREAM_PING_SEC = 10     # 回答を作っている間も、途中の機器に接続を切られないよう空の行を送る

log = logging.getLogger("yanua.api")

# 逐次表示で画面に出す進み具合（answer_question の段階 → 画面の文言）
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
}


class CheckIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)


VISIT_CSV_COLUMNS = ("date", "time", "company", "visitor", "host_employee_no", "room")


# ------------------------------------------------------------ 本体


def create_app(settings: Settings, llm: ChatModel, transcriber_factory=None, summary_llm: ChatModel | None = None) -> FastAPI:
    """summary_llm は議事録の要約に使うモデル。渡さないとき、llm が Ollama なら settings.summary_model を同じ Ollama で使う。"""
    if summary_llm is None:
        summary_llm = OllamaChat(settings.summary_model, llm.base_url) if isinstance(llm, OllamaChat) else llm
    """transcriber_factory：文字起こしを作る関数（テストでは偽のものを渡す）。既定は社内の faster-whisper。"""
    settings.check()
    roles = load_roles(settings.roles_path)
    policy = load_policy(settings.identity_path)
    jobs = JobQueue(settings.db_path, settings.job_workers)
    local = threading.local()

    def store() -> Store:
        # SQLite の接続はスレッドをまたいで使えないので、スレッドごとに開く
        if not hasattr(local, "store"):
            local.store = Store(settings.db_path)
        return local.store

    def connect() -> sqlite3.Connection:
        return sqlite3.connect(str(settings.db_path), timeout=10)

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

    def today() -> date:
        return settings.today or date.today()

    # ------------------------------------------------ ジョブの種類

    def ingest_job(ctx: JobContext, params: dict) -> dict:
        ctx.progress(5, "取り込みの準備")
        s = Store(settings.db_path)
        try:
            report = s.ingest(Path(params["source"]))
        finally:
            s.close()
        ctx.progress(100, "完了")
        return report.as_dict()

    jobs.register("ingest", ingest_job)

    def whisper():
        from ..meeting.transcribe import WhisperTranscriber

        try:
            return WhisperTranscriber(models.verify(settings.whisper_model, settings.models_dir))
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

    def paper_rules():
        from ..paper import rules as paper_rule_file

        try:
            return paper_rule_file.load_rules(settings.paper_rules)
        except (OSError, ValueError) as e:
            log.error("仕分けルール %s を読めません：%s", settings.paper_rules, e)
            raise ApiError(500, "paper_rules", "仕分けルールを読めません。管理者に連絡してください") from e

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        jobs.start()
        chats = ChatDB(settings.chat_db_path, settings.chat_idle_minutes)
        app.state.helpdesk = HelpdeskSessions(chats, settings.db_path, settings.helpdesk_data, llm, settings.top_k, policy)
        app.state.reception = ReceptionSessions(chats, settings.db_path, settings.reception_data, llm, settings.top_k,
                                                settings.today)

        async def sweep():
            # 期限の来たファイルを10分ごとに消す
            while True:
                await asyncio.sleep(600)
                try:
                    def run():
                        with closing(connect()) as db:
                            files.cleanup(db, settings.work_dir)

                    await run_in_threadpool(run)
                except Exception:  # noqa: BLE001  消せなかったものは次の回に
                    log.exception("期限切れのファイルを消せませんでした")

        sweeper = asyncio.create_task(sweep())
        yield
        sweeper.cancel()
        jobs.stop()
        chats.close()

    app = FastAPI(title="Yanua API", version="0.1", lifespan=lifespan,
                  # API の一覧（OpenAPI）は開発用ログインのときだけ公開する
                  docs_url="/api/docs" if settings.dev_login else None,
                  redoc_url=None, openapi_url="/api/openapi.json" if settings.dev_login else None)
    app.state.settings = settings
    app.state.jobs = jobs
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
        # 端末トークン（Bearer）で使えるのは受付の API（/api/kiosk/...）だけ
        if path.startswith("/api/") and request.headers.get("authorization", "").lower().startswith("bearer ") \
                and not path.startswith("/api/kiosk/"):
            return JSONResponse(errors.body("forbidden", "この操作をする権限がありません"), status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS") and request.url.path.startswith("/api/") \
                and not request.headers.get(CSRF_HEADER):
            return JSONResponse(errors.body("csrf", "画面からの操作として受け付けられません。画面を読み込み直してください"),
                                status_code=403)
        return await call_next(request)

    # ------------------------------------------------ ログイン中の人

    def current_user(request: Request) -> User:
        with closing(connect()) as db:
            user = auth.user_for_token(db, request.cookies.get(auth.COOKIE, ""), roles, settings.session_idle_minutes)
        if not user:
            raise not_logged_in()
        return user

    def require(action: str):
        def check(user: User = Depends(current_user)) -> User:
            if not allowed(user.roles, action):
                raise forbidden()
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
            raise not_found()
        with closing(connect()) as db:
            person = hr.lookup(db, req.employee_no)
            if not person:
                raise ApiError(401, "login_failed", "その社員番号ではログインできません")
            token = auth.create_session(db, person["employee_no"], settings.session_max_hours)
        set_cookie(response, token)
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
                auth.delete_session(db, token)
        response.delete_cookie(auth.COOKIE, path="/")
        return {"ok": True}

    @app.get("/api/me")
    def me(user: User = Depends(current_user)):
        return {**user.as_dict(), "permissions": permissions_of(user.roles)}

    # ------------------------------------------------ 質問・検索・文書

    @app.post("/api/ask")
    def ask(req: Ask, user: User = Depends(require("ask"))):
        started = time.monotonic()
        try:
            result = answer_question(store(), llm, req.question, user.dept, settings.top_k)
        except ValueError as e:
            raise ApiError(400, "invalid_question", str(e)) from e
        return {**result.as_dict(), "elapsed_sec": round(time.monotonic() - started, 2)}

    @app.post("/api/ask/stream")
    def ask_stream(req: Ask, user: User = Depends(require("ask"))):
        # ログイン・権限・質問の形は、送り始める前に確かめる（ふつうのエラーの形で返す）
        events: queue.Queue[tuple[str, dict] | None] = queue.Queue()

        def work():
            started = time.monotonic()
            s = Store(settings.db_path)
            try:
                result = answer_question(s, llm, req.question, user.dept, settings.top_k,
                                         progress=lambda st: events.put(("progress", {"stage": st, "message": ASK_STAGES[st]})))
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
        return {"hits": [h.as_dict() for h in search(store(), q, user.dept, max(1, min(top_k, 20)))]}

    @app.get("/api/documents")
    def documents(user: User = Depends(require("documents.list"))):
        s = store()
        admin = allowed(user.roles, "documents.ingest")
        docs = []
        for d in s.documents():
            if admin:
                docs.append(d)
            elif s.can_view(d["doc_id"], user.dept):
                # 置き場所（サーバーのパス）は管理者にだけ見せる
                docs.append({k: v for k, v in d.items() if k != "path"})
        return {"documents": docs}

    @app.get("/api/documents/{doc_id}")
    def document(doc_id: str, user: User = Depends(require("documents.list"))):
        s = store()
        # 見られない文書は、あるかどうかも分からせない（管理者でも部署の権限で決める）
        doc = s.document_sections(doc_id) if s.can_view(doc_id, user.dept) else None
        if not doc:
            raise not_found("文書")
        return doc

    @app.get("/api/documents/ingest/sources")
    def ingest_sources(user: User = Depends(require("documents.ingest"))):
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
    def ingest(req: Ingest, user: User = Depends(require("documents.ingest"))):
        path = Path(req.source).resolve()
        roots = [r.resolve() for r in settings.ingest_roots]
        if not any(path == r or path.is_relative_to(r) for r in roots) or not path.is_dir():
            raise ApiError(400, "invalid_source", "取り込めるフォルダではありません（設定の ingest_roots の下のフォルダだけ）")
        return jobs.submit("ingest", user.employee_no, {"source": str(path)})

    # ------------------------------------------------ ヘルプデスク

    @app.post("/api/chat/sessions", status_code=201)
    def chat_start(req: ChatStart, request: Request, user: User = Depends(require("helpdesk"))):
        if req.flow != "helpdesk":
            raise ApiError(400, "unknown_flow", "その会話は使えません")
        if req.account is not None and not settings.dev_login:
            raise forbidden()
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
        if not job or (job["owner"] != user.employee_no and not allowed(user.roles, "jobs.all")):
            raise not_found("ジョブ")
        return job

    @app.get("/api/jobs")
    def job_list(all: bool = False, user: User = Depends(require("jobs.own"))):
        if all and not allowed(user.roles, "jobs.all"):
            raise forbidden()
        return {"jobs": jobs.list(None if all else user.employee_no)}

    @app.get("/api/jobs/{job_id}")
    def job_get(job_id: str, user: User = Depends(require("jobs.own"))):
        return visible_job(job_id, user)

    @app.delete("/api/jobs/{job_id}")
    def job_cancel(job_id: str, user: User = Depends(require("jobs.own"))):
        job = visible_job(job_id, user)
        if job["status"] in FINISHED:
            raise ApiError(409, "finished", "このジョブは終わっています")
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
    def visit_create(req: VisitCreate, user: User = Depends(require("visits.own"))):
        host_no = req.host_employee_no or user.employee_no
        if host_no != user.employee_no and not allowed(user.roles, "visits.all"):
            raise forbidden()
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
    def visit_list(host: str = "me", date: str | None = None, user: User = Depends(require("visits.own"))):
        everyone = allowed(user.roles, "visits.all")
        day = None
        if date is not None:
            if not everyone:
                raise forbidden()
            try:
                day = Date.fromisoformat(date)
            except ValueError as e:
                raise ApiError(400, "invalid_date", "日付は YYYY-MM-DD で指定してください") from e
        if host != "me" and not everyone:
            raise forbidden()
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
            if not v or (v["host_employee_no"] != user.employee_no and not allowed(user.roles, "visits.all")):
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
    def ticket_list(status: str | None = None, priority: str | None = None, user: User = Depends(require("tickets"))):
        if status is not None and status not in desk.TICKET_STATUSES:
            raise ApiError(400, "invalid_status", f"状態は {'・'.join(desk.TICKET_STATUSES)} のどれかです")
        if priority is not None and priority not in desk.PRIORITIES:
            raise ApiError(400, "invalid_priority", "優先度は 高・中・低 のどれかです")
        with closing(connect()) as db:
            return {"tickets": desk.tickets(db, status, priority)}

    @app.get("/api/tickets/{ticket_id}")
    def ticket_get(ticket_id: str, user: User = Depends(require("tickets"))):
        with closing(connect()) as db:
            t = desk.ticket(db, ticket_id)
        if not t:
            raise not_found("問い合わせ票")
        return t

    @app.patch("/api/tickets/{ticket_id}")
    def ticket_update(ticket_id: str, req: TicketUpdate, user: User = Depends(require("tickets"))):
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
            # 端末トークンはここで1回だけ返す（保存するのはハッシュだけ）
            return kiosk.register(db, req.name.strip(), user.employee_no)

    @app.delete("/api/admin/kiosk-devices/{device_id}")
    def device_revoke(device_id: str, user: User = Depends(require("kiosk.devices"))):
        with closing(connect()) as db:
            if not kiosk.revoke(db, device_id):
                raise not_found("受付端末")
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
                limiter.fail(device["device_id"])
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
        if not allowed(user.roles, perm):
            raise forbidden()
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
        if not f or not allowed(user.roles, f["perm"]):
            raise not_found("ファイル")
        return f

    @app.get("/api/files/{file_id}")
    def download(file_id: str, user: User = Depends(current_user)):
        f = my_file(file_id, user)
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
            return paperjobs.execute(db, plan_id, settings.paper_inbox, settings.paper_out, move=req.move)

    @app.get("/api/paper/history")
    def paper_history(limit: int = 200, user: User = Depends(require("paper"))):
        return {"records": paperjobs.history(settings.paper_out, max(1, min(limit, 1000)))}

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
        from urllib.parse import quote

        return RawResponse(body, media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
                           headers={"Content-Disposition": f"attachment; filename=\"minutes.docx\"; filename*=UTF-8''{quote(name)}",
                                    "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})

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
