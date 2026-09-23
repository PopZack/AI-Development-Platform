"""服务端事件流（SSE）。

Layer: Presentation（Router）。

### 为什么是 SSE 而不是 WebSocket

事件是**单向**的（服务端 → 浏览器）。WebSocket 是双向的，用它要多付
握手升级、心跳协议、连接状态机这套成本，换来的能力我们用不上；
用户操作走的是普通 POST，本来就有响应体。SSE 是纯 HTTP，代理、
鉴权、日志全都沿用现成的一套。

### 关于认证方式

浏览器的 ``EventSource`` **不能带自定义请求头**，所以这里同时支持：

1. ``Authorization: Bearer <token>``（推荐，我们的前端用 fetch 流式读）
2. ``?token=<token>``（给 ``EventSource`` / 第三方集成的兼容入口）

⚠️ 用 query 传令牌会进访问日志和浏览器历史。这是**显式取舍**：
不提供它，EventSource 用户就只能改前端；提供它，就要接受令牌出现在 URL 里。
内网部署可以接受，公网请用第 1 种。

### 断线与重连

``Last-Event-ID`` / 断点续传**没有实现**：事件是「看当前进展」的旁路数据，
真正的状态在 ``GET /runs/{id}``。断线后重连拉一次状态即可，
为此维护事件回放缓冲不值得（而且会把内存变成一个有界队列的复杂度黑洞）。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from app.common.dependencies import AuthServiceDep, CurrentUserDep, RequirementServiceDep, SettingsDep
from app.common.exceptions import AuthenticationError
from app.common.openapi import AUTH_ERROR_RESPONSES
from app.infrastructure.events import Event, Subscription, get_event_bus
from app.models.user import User

__all__ = ["router"]

logger = logging.getLogger(__name__)

router = APIRouter(tags=["events"])


async def _user_from_header_or_query(request: Request, auth: AuthServiceDep) -> User:
    """先看请求头，再回落到 ``?token=`` —— 见模块文档里的取舍说明。"""
    header = request.headers.get("authorization", "")
    token = header[7:] if header.lower().startswith("bearer ") else ""
    if not token:
        token = request.query_params.get("token", "")
    if not token:
        raise AuthenticationError("A Bearer token (header or ?token=) is required")
    return await auth.authenticate_token(token)


SSEUserDep = Annotated[User, Depends(_user_from_header_or_query)]


async def _frames(subscription: Subscription, heartbeat: float) -> AsyncIterator[str]:
    """把订阅变成一帧帧 SSE 文本。

    **取件任务常驻、只给「等待」加超时**，这是本函数唯一需要小心的点：
    写成 ``asyncio.wait_for(anext(events), timeout=heartbeat)`` 看起来更短，
    但超时会取消取件协程并杀掉那个异步生成器 —— 于是**第一次心跳之后事件就不再
    送达**（下一次 anext 直接 StopAsyncIteration）。这个 bug 在单测里表现为
    「超时后流死掉」，第一次真实使用时表现为「页面开了十几秒就不动了」。
    """
    getter: asyncio.Future[Event] = asyncio.ensure_future(subscription.get())
    try:
        while True:
            sleeper = asyncio.ensure_future(asyncio.sleep(heartbeat))
            done, _pending = await asyncio.wait({getter, sleeper}, return_when=asyncio.FIRST_COMPLETED)
            if getter in done:
                if not sleeper.done():
                    sleeper.cancel()
                event = getter.result()
                getter = asyncio.ensure_future(subscription.get())
                yield f"event: {event.type}\ndata: {event.to_json()}\n\n"
                continue
            # 心跳：注释帧。EventSource 忽略它，但代理不会掐掉长时间无数据的连接
            #
            # 这里**刻意不调用 request.is_disconnected()**，两个原因：
            # 1. Starlette 的 StreamingResponse 自己会用 listen_for_disconnect
            #    监听断线并取消这个生成器（真机上足够）；
            # 2. 它不是「非阻塞检查」—— 在 httpx 的 ASGITransport 下，请求体读完
            #    后 receive() 会等到响应结束才返回，于是「等断线」变成死锁：
            #    生成器等 is_disconnected，响应等生成器。测试里表现为整个用例挂住。
            yield ": ping\n\n"
    finally:
        getter.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await getter


@router.get(
    "/requirements/{requirement_id}/events",
    summary="订阅该需求的事件流（SSE）",
    description=(
        "`text/event-stream`。事件类型：\n\n"
        "- `workflow.status`：工作流状态/步骤变化（`run_id` / `status` / `current_step`）\n"
        "- `artifact.created`：新的交付物（`type` / `version`）\n"
        "- `approval.created` / `approval.decided`：审批产生与决定\n"
        "- `agent.run`：Agent 开始/成功/失败\n\n"
        "认证支持 `Authorization: Bearer`（推荐）或 `?token=`（给浏览器 EventSource）。\n\n"
        "**单 worker 下事件完整；多 worker 需要配 `REDIS_URL`**，否则只在"
        "产生事件的那个 worker 上可见。"
    ),
    responses=AUTH_ERROR_RESPONSES,
)
async def stream_requirement_events(
    requirement_id: UUID,
    request: Request,
    requirements: RequirementServiceDep,
    settings: SettingsDep,
    current_user: SSEUserDep,
) -> StreamingResponse:
    # 权限：读权限即可（VIEWER 也应该能看进展）。不这么做的话，
    # 事件流会变成绕过资源级权限的信息泄露通道
    await requirements.get_requirement(requirement_id, actor=current_user)

    bus = get_event_bus()
    heartbeat = settings.sse_heartbeat_seconds

    async def event_stream() -> AsyncIterator[str]:
        # 立刻发一帧：否则某些代理要等到第一个字节才开始转发，
        # 客户端会以为连不上
        yield ": connected\n\n"
        async with bus.subscribe(requirement_id=requirement_id) as subscription:
            async for frame in _frames(subscription, heartbeat):
                yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            # nginx：关掉对这条响应的缓冲，否则事件会被攒着一起发
            "X-Accel-Buffering": "no",
        },
    )


@router.get(
    "/events",
    summary="订阅全局事件流（SSE，审批提醒等）",
    responses=AUTH_ERROR_RESPONSES,
)
async def stream_global_events(
    request: Request, settings: SettingsDep, current_user: CurrentUserDep
) -> StreamingResponse:
    """不带 requirement 维度的事件：目前用于「有新审批待处理」。

    刻意只推送**不含业务细节**的提醒（谁的需求、什么工具），
    具体内容仍要调接口取 —— 全局流不该成为绕过权限的旁路。
    """
    bus = get_event_bus()
    heartbeat = settings.sse_heartbeat_seconds

    async def event_stream() -> AsyncIterator[str]:
        yield ": connected\n\n"
        async with bus.subscribe() as subscription:
            async for frame in _frames(subscription, heartbeat):
                yield frame

    return StreamingResponse(
        event_stream(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache, no-transform",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )
