"""统一错误响应。

设计文档 §7 规定所有错误必须长这样：

    {"error": {"code": "...", "message": "...", "request_id": "...", "details": {}}}

这个模块把三类异常收敛到同一种结构：
1. 业务异常 AppError
2. FastAPI/Pydantic 的请求校验异常 RequestValidationError
3. Starlette HTTPException 以及未捕获异常

Layer: Common。
"""

from __future__ import annotations

import logging

from fastapi import FastAPI
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.requests import Request
from starlette.responses import JSONResponse

from app.common.exceptions import AppError, InternalError, ValidationError
from app.common.request_context import get_request_id

logger = logging.getLogger(__name__)

__all__ = ["register_exception_handlers", "error_response"]

_STATUS_TO_CODE: dict[int, str] = {
    400: "BAD_REQUEST",
    401: "AUTHENTICATION_REQUIRED",
    403: "FORBIDDEN",
    404: "NOT_FOUND",
    405: "METHOD_NOT_ALLOWED",
    409: "CONFLICT",
    422: "VALIDATION_ERROR",
}


def error_response(
    *,
    status_code: int,
    code: str,
    message: str,
    details: dict | list | None = None,
    request_id: str | None = None,
) -> JSONResponse:
    """构造统一错误响应。"""
    return JSONResponse(
        status_code=status_code,
        content={
            "error": {
                "code": code,
                "message": message,
                "request_id": request_id or get_request_id(),
                "details": details or {},
            }
        },
    )


def register_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(AppError)
    async def _handle_app_error(_: Request, exc: AppError) -> JSONResponse:
        # 业务异常是可预期分支：warning 级别，不打堆栈
        logger.warning(
            "business error | code=%s status=%s message=%s details=%s",
            exc.code,
            exc.status_code,
            exc.message,
            exc.details or "-",
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_error_body(get_request_id()))

    @app.exception_handler(RequestValidationError)
    async def _handle_request_validation(_: Request, exc: RequestValidationError) -> JSONResponse:
        details = {"errors": jsonable_encoder(exc.errors(), custom_encoder={Exception: str})}
        logger.info("request validation failed | %s", details)
        return error_response(
            status_code=ValidationError.status_code,
            code=ValidationError.code,
            message=ValidationError.default_message,
            details=details,
        )

    @app.exception_handler(StarletteHTTPException)
    async def _handle_http_exception(_: Request, exc: StarletteHTTPException) -> JSONResponse:
        code = _STATUS_TO_CODE.get(exc.status_code, f"HTTP_{exc.status_code}")
        return error_response(
            status_code=exc.status_code,
            code=code,
            message=str(exc.detail) if exc.detail else code,
            details={},
        )

    @app.exception_handler(Exception)
    async def _handle_unexpected(_: Request, exc: Exception) -> JSONResponse:
        # 未知错误：打完整堆栈，但只对外返回通用消息，不暴露内部实现
        logger.exception("unhandled error")
        failure = InternalError()
        return JSONResponse(status_code=failure.status_code, content=failure.to_error_body(get_request_id()))
