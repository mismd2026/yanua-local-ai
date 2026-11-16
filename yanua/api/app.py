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
    POST   /api/chat/sessions                  ヘルプデスクの会話を始める
    POST   /api/chat/sessions/{id}/messages    返事を送る
    DELETE /api/chat/sessions/{id}             会話をやめる
    GET    /api/jobs, GET/DELETE /api/jobs/{id}
    GET    /, /chat など           Web画面（web/ を組み立てた web/dist。組み立てていなければ配らない）

画面から呼ぶ前提の決まり：
- 状態を変えるリクエスト（GET 以外）には、ヘッダー X-Yanua-Client を付ける。
  別のサイトのページからは、このヘッダーを付けて送れない（CSRF 対策）
- 権限はすべてこの API の側で確かめる（画面で隠すだけにしない）
"""

from __future__ import annotations

import json
import logging
import queue
import sqlite3
import threading
import time
from contextlib import asynccontextmanager, closing
from pathlib import Path

from fastapi import Depends, FastAPI, Request, Response
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field

from .. import hr
from ..answer import answer_question
from ..llm import ChatModel, LLMError
from ..search import search
from ..store import Store
from . import auth, errors
from .auth import User
from .chat import HelpdeskSessions
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
        app.state.helpdesk = HelpdeskSessions(settings.db_path, settings.chat_db_path, settings.helpdesk_data, llm,
                                              settings.top_k, settings.chat_idle_minutes, policy)
        yield
        jobs.stop()
        app.state.helpdesk.close()

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
