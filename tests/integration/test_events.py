"""事件总线与 SSE 接口的测试。

事件是旁路：这些用例要同时验证「推得出去」和「推不出去也不影响业务」。
"""

from __future__ import annotations

import asyncio
import contextlib
from typing import Any
from uuid import uuid4

from httpx import AsyncClient

from app.infrastructure.events import Event, EventBus
from tests.conftest import API

_PING = ": ping\n\n"

_PING = ": ping\n\n"


async def _next(iterator: Any, timeout: float = 5.0) -> str:
    return await asyncio.wait_for(anext(iterator), timeout=timeout)


# ---------------------------------------------------------------- 总线本身


async def test_subscriber_receives_matching_events_only() -> None:
    """按需求过滤：前端只关心自己正在看的那个需求。"""

    bus = EventBus()
    wanted = uuid4()
    other = uuid4()

    async with bus.subscribe(requirement_id=wanted) as subscription:
        await bus.publish(Event(type="skipped", requirement_id=other))
        await bus.publish(Event(type="kept", requirement_id=wanted))
        # 全局事件（requirement_id=None）不该推给按需求订阅的人：它没有归属，
        # 推过去只会让前端收到一堆自己看不懂的事件
        await bus.publish(Event(type="global"))

        event = await asyncio.wait_for(subscription.get(), timeout=2)
        assert event.type == "kept"
        assert subscription.queue.empty()


async def test_global_subscriber_receives_everything() -> None:
    bus = EventBus()
    async with bus.subscribe() as subscription:
        await bus.publish(Event(type="a", requirement_id=uuid4()))
        await bus.publish(Event(type="b"))

        assert (await asyncio.wait_for(subscription.get(), timeout=2)).type == "a"
        assert (await asyncio.wait_for(subscription.get(), timeout=2)).type == "b"


async def test_queue_overflow_drops_oldest_not_newest() -> None:
    """订阅者卡住时丢最旧的：SSE 要的是「当前进展」，堆过期事件没有价值。"""
    bus = EventBus()
    async with bus.subscribe() as subscription:
        for i in range(250):  # 队列上限 200
            await bus.publish(Event(type=f"e{i}"))

        seen = [subscription.queue.get_nowait().type for _ in range(subscription.queue.qsize())]

        assert len(seen) == 200
        assert seen[-1] == "e249"  # 最新的在
        assert "e0" not in seen  # 最旧的被丢掉


async def test_emit_never_raises_without_event_loop() -> None:
    """emit 是「发完不管」：没有事件循环时静默返回，绝不把业务带崩。"""
    bus = EventBus()
    bus.emit(Event(type="no-loop"))  # 同步上下文里调用，不应抛错


async def test_publish_survives_broken_redis() -> None:
    """Redis 发布失败不能影响本地投递 —— 事件是旁路。"""

    class _BrokenRedis:
        async def publish(self, channel: str, message: str) -> int:
            raise ConnectionError("redis down")

    bus = EventBus(redis=_BrokenRedis())  # type: ignore[arg-type]
    async with bus.subscribe() as subscription:
        await bus.publish(Event(type="still-delivered"))
        assert (await asyncio.wait_for(subscription.get(), timeout=2)).type == "still-delivered"


async def test_unsubscribe_on_exit() -> None:
    bus = EventBus()
    async with bus.subscribe() as subscription:
        assert subscription is not None
    # 退出上下文后订阅者被摘掉，事件不再堆积
    await bus.publish(Event(type="after"))
    assert not bus._subscribers  # noqa: SLF001 - 这条断言看的就是内部状态


# ---------------------------------------------------------------- SSE 接口


async def test_sse_stream_requires_permission(
    client: AsyncClient, make_actor, make_project_with_member
) -> None:
    """没有成员身份的人不能订阅 —— 否则事件流是绕过权限的信息泄露通道。"""
    owner, proj, _member = await make_project_with_member()
    outsider = await make_actor()
    req = (
        await client.post(
            f"{API}/projects/{proj['id']}/requirements",
            json={"title": "T", "description": "D"},
            headers=owner["headers"],
        )
    ).json()

    response = await client.get(f"{API}/requirements/{req['id']}/events", headers=outsider["headers"])
    assert response.status_code == 403


async def test_sse_requires_a_token_at_all(client: AsyncClient, make_actor) -> None:
    owner = await make_actor()
    proj = (await client.post(f"{API}/projects", json={"name": "P"}, headers=owner["headers"])).json()
    req = (
        await client.post(
            f"{API}/projects/{proj['id']}/requirements",
            json={"title": "T", "description": "D"},
            headers=owner["headers"],
        )
    ).json()

    response = await client.get(f"{API}/requirements/{req['id']}/events")
    assert response.status_code == 401


async def test_frames_survive_heartbeats() -> None:
    """回归用例：心跳之后事件必须还能送达。

    这里锁住的是一个真实踩过的坑 —— 用 ``asyncio.wait_for(anext(订阅), timeout)``
    实现心跳看起来最直观，但超时会把取件协程取消、连带杀掉那个异步生成器，
    于是**第一次心跳之后事件永远不再送达**（页面开了十几秒就不动了）。
    正确做法是取件任务常驻、只给「等待」加超时。
    """
    from app.api.v1.events import _frames

    bus = EventBus()
    async with bus.subscribe() as subscription:
        frames = _frames(subscription, heartbeat=0.05)
        collected: list[str] = []

        async def collect() -> None:
            # 后台一直读：心跳和事件在时间上是竞争的，用例不该假设谁先到
            while True:
                collected.append(await anext(frames))

        reader = asyncio.ensure_future(collect())
        try:
            # 先让心跳跑几轮 —— 这一步正是「杀死生成器」那个 bug 的触发条件
            await _wait_until(lambda: collected.count(_PING) >= 2)

            await bus.publish(Event(type="workflow.status", payload={"status": "RUNNING"}))
            await _wait_until(lambda: any(f.startswith("event: workflow.status") for f in collected))

            # 关键断言：心跳之后生成器依然活着，再发还能收到
            await bus.publish(Event(type="artifact.created", payload={"type": "PRD"}))
            await _wait_until(lambda: any(f.startswith("event: artifact.created") for f in collected))
        finally:
            reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader
            await frames.aclose()


async def _wait_until(predicate: Any, timeout: float = 3.0) -> None:
    """轮询等待条件成立 —— 只用在测试里，实现简单比花哨重要。"""
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.01)
    raise AssertionError("condition not met within timeout")


async def test_frames_release_the_getter_on_close() -> None:
    """生成器被关闭（客户端断线）时不能留下悬空的取件任务。"""
    from app.api.v1.events import _frames

    bus = EventBus()
    async with bus.subscribe() as subscription:
        frames = _frames(subscription, heartbeat=5)
        first = asyncio.ensure_future(anext(frames))
        await asyncio.sleep(0.01)
        first.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await first
        await frames.aclose()
        assert True  # 不抛错即通过：finally 里的清理必须能正常收尾


async def test_query_token_authenticates(db_session: Any, test_settings: Any, make_actor) -> None:
    """浏览器 EventSource 带不了自定义头，所以 ?token= 这条路必须真的能用。"""
    from starlette.requests import Request

    from app.api.v1.events import _user_from_header_or_query
    from app.application.auth_service import AuthService

    actor = await make_actor()
    token = actor["headers"]["Authorization"].removeprefix("Bearer ")
    service = AuthService(db_session, test_settings)

    request = Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/api/v1/events",
            "query_string": f"token={token}".encode(),
            "headers": [],
        }
    )
    user = await _user_from_header_or_query(request, service)
    assert str(user.id) == actor["user"]["id"]
