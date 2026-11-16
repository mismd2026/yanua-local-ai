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

段階2（K-045・K-018・K-043）：
    GET    /api/notifications, POST /api/notifications/read      自分あて・自分の部署あてのお知らせ
    POST   /api/visits, GET /api/visits, DELETE /api/visits/{id}  来訪予定（社員は自分の。総務は全社）
    POST   /api/visits/import                                     来訪予定の取り込み（総務。CSV を本文で送る）
    GET    /api/reception/records, POST .../{id}/done             受付の記録（総務）
    GET    /api/tickets, GET/PATCH /api/tickets/{id}              問い合わせ票（情シス）
    GET/POST /api/kiosk/devices, DELETE /api/kiosk/devices/{id}   受付端末の登録（管理者。社内のログインで使う）
    POST   /api/kiosk/sessions, .../{id}/messages                 受付の会話（受付端末のトークンだけ）
    POST   /api/kiosk/check-in                                    コードの照合と受付（受付端末のトークンだけ）
    GET    /api/kiosk/passes/{pass_id}.html                       入館票（発行した受付端末だけ）

画面から呼ぶ前提の決まり：
- 状態を変えるリクエスト（GET 以外）には、ヘッダー X-Yanua-Client を付ける。
  別のサイトのページからは、このヘッダーを付けて送れない（CSRF 対策）
- 権限はすべてこの API の側で確かめる（画面で隠すだけにしない）
- 受付端末は Authorization: Bearer <端末トークン> で呼ぶ。端末トークンは /api/kiosk/... の受付の API だけに使え、
  ほかの API（端末の登録 /api/kiosk/devices を含む）は拒否する。受付の API は社内のログインのクッキーを見ない
"""

from __future__ import annotations

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
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, StreamingResponse
from starlette.concurrency import run_in_threadpool
from pydantic import BaseModel, Field

from .. import hr, visits
from ..answer import answer_question
from ..llm import ChatModel, LLMError
from ..search import search
from ..store import Store
from . import auth, desk, errors, kiosk
from .auth import User
from .chat import ChatDB, HelpdeskSessions, ReceptionSessions
from .errors import ApiError, forbidden, not_found, not_logged_in
from .jobs import FINISHED, JobContext, JobQueue
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


class CheckIn(BaseModel):
    code: str = Field(min_length=1, max_length=64)


VISIT_CSV_COLUMNS = ("date", "time", "company", "visitor", "host_employee_no", "room")


# ------------------------------------------------------------ 本体


def create_app(settings: Settings, llm: ChatModel) -> FastAPI:
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

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        jobs.start()
        chats = ChatDB(settings.chat_db_path, settings.chat_idle_minutes)
        app.state.helpdesk = HelpdeskSessions(chats, settings.db_path, settings.helpdesk_data, llm, settings.top_k, policy)
        app.state.reception = ReceptionSessions(chats, settings.db_path, settings.reception_data, llm, settings.top_k,
                                                settings.today)
        yield
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
        # 端末トークン（Bearer）で使えるのは受付の API だけ。端末の登録（/api/kiosk/devices）にも使えない
        if path.startswith("/api/") and request.headers.get("authorization", "").lower().startswith("bearer ") \
                and (not path.startswith("/api/kiosk/") or path.startswith("/api/kiosk/devices")):
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

    def padded(started: float) -> None:
        # 受付の API は、決まった時間より早くは返さない（コードが正しい・違う日・取り消し・間違いで、かかる時間をそろえる）
        rest = settings.kiosk_min_seconds - (time.monotonic() - started)
        if rest > 0:
            time.sleep(rest)

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

    @app.get("/api/kiosk/devices")
    def device_list(user: User = Depends(require("kiosk.devices"))):
        with closing(connect()) as db:
            return {"devices": kiosk.devices(db)}

    @app.post("/api/kiosk/devices", status_code=201)
    def device_register(req: DeviceCreate, user: User = Depends(require("kiosk.devices"))):
        with closing(connect()) as db:
            # 端末トークンはここで1回だけ返す（保存するのはハッシュだけ）
            return kiosk.register(db, req.name.strip(), user.employee_no)

    @app.delete("/api/kiosk/devices/{device_id}")
    def device_revoke(device_id: str, user: User = Depends(require("kiosk.devices"))):
        with closing(connect()) as db:
            if not kiosk.revoke(db, device_id):
                raise not_found("受付端末")
        return {"ok": True}

    # ------------------------------------------------ 受付端末（S-40・S-41。端末トークンだけ）

    def kiosk_reply(device: dict, r: dict) -> dict:
        # 返事の形はいつも同じ（予定・入館票の番号がないときは null）
        out = {"session_id": r["session_id"], "finished": r["finished"], "reply": r["reply"], "visit": None, "pass_id": None}
        if r.get("visit"):
            with closing(connect()) as db:
                host = hr.lookup(db, r["visit"]["host_employee_no"])
                out["visit"] = kiosk.visit_for_kiosk(r["visit"], host)
                out["pass_id"] = kiosk.issue_pass(db, r["visit"]["visit_id"], device["device_id"], settings.kiosk_pass_minutes)
        return out

    @app.post("/api/kiosk/sessions", status_code=201)
    def kiosk_start(request: Request, device: dict = Depends(current_device)):
        started = time.monotonic()
        try:
            return kiosk_reply(device, request.app.state.reception.start(f"kiosk:{device['device_id']}"))
        finally:
            padded(started)

    @app.post("/api/kiosk/sessions/{session_id}/messages")
    def kiosk_send(session_id: str, req: ChatMessage, request: Request, device: dict = Depends(current_device)):
        started = time.monotonic()
        try:
            return kiosk_reply(device, request.app.state.reception.send(f"kiosk:{device['device_id']}", session_id, req.text))
        finally:
            padded(started)

    @app.post("/api/kiosk/check-in")
    def kiosk_check_in(req: CheckIn, device: dict = Depends(current_device)):
        started = time.monotonic()
        failed = {"ok": False, "message": kiosk.FAILED_CODE, "visit": None, "pass_id": None}
        try:
            if limiter.blocked(device["device_id"]):
                return failed
            with closing(connect()) as db:
                visit = visits.find_by_code(db, req.code, today())
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
                pass_id = kiosk.issue_pass(db, visit["visit_id"], device["device_id"], settings.kiosk_pass_minutes)
            return {"ok": True, "message": f"{shown['visitor_company']} {shown['visitor_name']}様、お待ちしておりました。"
                                           "担当の者に到着をお知らせしました。入館票をお受け取りのうえ、会議室へお進みください。",
                    "visit": shown, "pass_id": pass_id}
        finally:
            padded(started)

    @app.get("/api/kiosk/passes/{pass_id}.html")
    def kiosk_pass(pass_id: str, device: dict = Depends(current_device)):
        started = time.monotonic()
        try:
            with closing(connect()) as db:
                visit_id = kiosk.pass_visit_id(db, pass_id, device["device_id"])
                visit = visits.find(db, visit_id) if visit_id else None
                if not visit:
                    raise not_found("入館票")
                host = hr.lookup(db, visit["host_employee_no"])
                body = visits.pass_html(visit, host["name"] if host else "担当の者")
            return HTMLResponse(body, headers={
                # 入館票の中のスクリプト（印刷ボタン）は動かさない。印刷は受付端末の画面が行う
                "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; frame-ancestors 'self'",
                "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
        finally:
            padded(started)

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
    - /api/... と /kiosk は社内の画面を返さない（受付端末は別の画面。段階2）
    """
    if not dist or not (Path(dist) / "index.html").is_file():
        return
    root = Path(dist).resolve()
    index = root / "index.html"

    @app.api_route("/{path:path}", methods=["GET", "HEAD"], include_in_schema=False)
    def web(path: str):
        first = path.split("/", 1)[0]
        if first in ("api", "kiosk", "health"):
            raise not_found()
        if path:
            f = (root / path).resolve()
            if f != root and f.is_relative_to(root) and f.is_file():
                # 組み立てたファイルは名前にハッシュが付くので、長く覚えさせてよい
                cache = "public, max-age=31536000, immutable" if first == "assets" else "no-cache"
                return FileResponse(f, headers={**WEB_HEADERS, "Cache-Control": cache})
            if "." in path.rsplit("/", 1)[-1]:
                raise not_found()   # ないファイル（.js など）に index.html を返さない
        return FileResponse(index, headers={**WEB_HEADERS, "Cache-Control": "no-cache"})
