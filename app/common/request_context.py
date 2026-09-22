"""请求上下文：为每个请求生成 request_id 并贯穿日志与错误响应。

用 contextvars 而不是把 request_id 层层传参，这样 Repository / Service
在任意深度都能拿到同一个 request_id，日志和错误响应天然对齐。

Layer: Common。
"""

from __future__ import annotations

import logging
from contextvars import ContextVar, Token
from uuid import uuid4

from starlette.datastructures import MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

__all__ = [
    "RequestContextMiddleware",
    "get_request_id",
    "new_request_id",
]

logger = logging.getLogger(__name__)

REQUEST_ID_HEADER = "X-Request-ID"

_request_id: ContextVar[str] = ContextVar("request_id", default="-")


def new_request_id() -> str:
    return f"req-{uuid4().hex[:12]}"


def get_request_id() -> str:
    """取当前请求的 request_id；不在请求上下文里时返回 '-'。"""
    return _request_id.get()


def _set_request_id(value: str) -> Token[str]:
    return _request_id.set(value)


def _reset_request_id(token: Token[str]) -> None:
    _request_id.reset(token)


class RequestContextMiddleware:
    """纯 ASGI 中间件：绑定 request_id，并把它回写到响应头。

    刻意不用 BaseHTTPMiddleware —— 它在 contextvar 传播上有已知的
    边界问题，纯 ASGI 实现更可靠。
    """

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        # 允许客户端透传自己的 request_id，便于跨端排查
        incoming = dict(scope.get("headers") or [])
        provided = incoming.get(REQUEST_ID_HEADER.lower().encode())
        request_id = provided.decode("latin-1") if provided else new_request_id()

        token = _set_request_id(request_id)

        async def send_with_request_id(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[REQUEST_ID_HEADER] = request_id
            await send(message)

        try:
            await self.app(scope, receive, send_with_request_id)
        finally:
            _reset_request_id(token)
