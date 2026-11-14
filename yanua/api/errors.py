"""API のエラー。すべて同じ形で返す。

    {"error": {"code": "forbidden", "message": "この操作をする権限がありません"}}

message はそのまま画面に出せる日本語にする。詳しい理由（どの権限が足りないかなど）は返さない。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from ..llm import LLMError

log = logging.getLogger("yanua.api")


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(message)
        self.status = status
        self.code = code
        self.message = message


def not_logged_in() -> ApiError:
    return ApiError(401, "not_logged_in", "ログインしてください")


def forbidden() -> ApiError:
    return ApiError(403, "forbidden", "この操作をする権限がありません")


def not_found(what: str = "") -> ApiError:
    return ApiError(404, "not_found", f"{what}が見つかりません" if what else "見つかりません")


def body(code: str, message: str) -> dict:
    return {"error": {"code": code, "message": message}}


_HTTP_CODES = {
    404: ("not_found", "見つかりません"),
    405: ("method_not_allowed", "この操作はできません"),
}


def install(app: FastAPI) -> None:
    @app.exception_handler(ApiError)
    async def _api_error(request: Request, e: ApiError):
        return JSONResponse(body(e.code, e.message), status_code=e.status)

    @app.exception_handler(StarletteHTTPException)
    async def _http_error(request: Request, e: StarletteHTTPException):
        code, message = _HTTP_CODES.get(e.status_code, ("error", "処理できませんでした"))
        return JSONResponse(body(code, message), status_code=e.status_code)

    @app.exception_handler(RequestValidationError)
    async def _invalid(request: Request, e: RequestValidationError):
        return JSONResponse(body("invalid_request", "リクエストの形が正しくありません"), status_code=400)

    @app.exception_handler(LLMError)
    async def _llm(request: Request, e: LLMError):
        log.error("LLM: %s", e)
        return JSONResponse(body("llm_unavailable", "回答を作るAIに接続できません。しばらくしてからやり直してください"),
                            status_code=502)

    @app.exception_handler(Exception)
    async def _unexpected(request: Request, e: Exception):
        log.exception("想定していないエラー")
        return JSONResponse(body("internal", "サーバーでエラーが起きました"), status_code=500)
