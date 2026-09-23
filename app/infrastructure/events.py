"""应用内事件总线：单进程广播 + 可选 Redis 跨进程广播。

Layer: Infrastructure。

**为什么需要它**：一次工作流执行实测几分钟（5 次模型调用），中间还有两个
人工停点。让前端每秒轮询一次「现在到哪一步了」，既浪费（绝大多数请求返回
「没变化」），又看不出过程（轮询粒度就是信息粒度）。事件流把「状态变了」
这件事本身变成数据推给前端。

### 两种模式

| 部署形态 | 模式 | 缺限 |
|---|---|---|
| 单进程（本地/单 worker） | 进程内广播 | 无 |
| 多 worker | 进程内广播 + Redis 发布订阅 | 必须有 Redis，否则事件只在产生它的 worker 上可见 |

Redis 模式下每个进程既是发布者也是订阅者。事件信封里带 ``origin``
（进程标识），收到自己发的事件直接丢弃 —— 否则本进程的订阅者会收到两份。

### 为什么 publish 是「发完不管」

事件是**旁路**：推不出去不该让业务失败。所以对外暴露的 ``emit()`` 起一个
后台 task 就返回，不 await、不抛错。调用方（Service / Orchestrator）
因此可以放心地在业务路径上 emit。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from app.infrastructure.redis import RedisLike

__all__ = ["Event", "EventBus", "Subscription", "configure_event_bus", "get_event_bus"]

logger = logging.getLogger(__name__)

# 每个订阅者的队列上限。满了丢最旧的：SSE 是「看当前进展」，
# 堆积几百条过期事件对用户没有任何价值，而阻塞发布者会拖慢业务。
_QUEUE_SIZE = 200


@dataclass(frozen=True, slots=True)
class Event:
    """一条事件。

    ``requirement_id`` 是订阅过滤的维度：前端只关心自己正在看的那个需求。
    ``None`` 表示全局事件（如「有新审批待处理」的提醒）。
    """

    type: str
    payload: dict[str, Any] = field(default_factory=dict)
    requirement_id: UUID | None = None
    at: datetime = field(default_factory=lambda: datetime.now(UTC))

    def to_json(self) -> str:
        return json.dumps(
            {
                "type": self.type,
                "requirement_id": str(self.requirement_id) if self.requirement_id else None,
                "at": self.at.isoformat(),
                "payload": self.payload,
            },
            ensure_ascii=False,
            default=str,
        )


class Subscription:
    """一个订阅者。

    ``queue`` 直接暴露给调用方（SSE 端点要同时等「事件到来」和「心跳超时」，
    见 ``app/api/v1/events.py``）。**不要用 ``asyncio.wait_for(anext(...))``
    给取件加超时** —— 超时会取消取件协程，把等待中的 ``queue.get()`` 一起取消，
    极端情况下丢事件；正确做法是让取件任务常驻、只给「等待」加超时。
    """

    def __init__(self, requirement_id: UUID | None) -> None:
        self.requirement_id = requirement_id
        self.queue: asyncio.Queue[Event] = asyncio.Queue(maxsize=_QUEUE_SIZE)

    async def get(self) -> Event:
        return await self.queue.get()

    def matches(self, event: Event) -> bool:
        return self.requirement_id is None or event.requirement_id == self.requirement_id

    def offer(self, event: Event) -> None:
        try:
            self.queue.put_nowait(event)
        except asyncio.QueueFull:
            # 丢最旧的一条，再放新的进来
            with contextlib.suppress(asyncio.QueueEmpty):
                self.queue.get_nowait()
            with contextlib.suppress(asyncio.QueueFull):
                self.queue.put_nowait(event)


class EventBus:
    def __init__(
        self,
        *,
        redis: RedisLike | None = None,
        channel: str = "aidev:events",
        origin: str | None = None,
    ) -> None:
        self._subscribers: set[Subscription] = set()
        self._redis = redis
        self._channel = channel
        self._origin = origin or uuid4().hex
        self._reader: asyncio.Task[None] | None = None
        self._pending: set[asyncio.Task[None]] = set()

    # ------------------------------------------------------------------ 生命周期

    async def start(self) -> None:
        if self._redis is None:
            logger.info("event bus started in in-process mode (single worker only)")
            return
        self._reader = asyncio.create_task(self._read_redis())
        logger.info("event bus started with redis fan-out | channel=%s", self._channel)

    async def stop(self) -> None:
        if self._reader is not None:
            self._reader.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._reader
            self._reader = None
        for task in list(self._pending):
            task.cancel()
        self._pending.clear()

    # ------------------------------------------------------------------ 发布

    async def publish(self, event: Event) -> None:
        self._fanout(event)
        if self._redis is not None:
            envelope = json.dumps(
                {"origin": self._origin, "event": json.loads(event.to_json())},
                ensure_ascii=False,
            )
            try:
                await self._redis.publish(self._channel, envelope)
            except Exception:  # pragma: no cover - 网络故障
                # 推不出去不该影响业务；本地订阅者已经收到了
                logger.warning("redis publish failed; event delivered locally only", exc_info=True)

    def emit(self, event: Event) -> None:
        """发完不管。业务代码在关键路径上调用它，绝不因为事件系统而失败。"""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:  # pragma: no cover - 同步上下文（脚本收尾等）
            return
        task = loop.create_task(self.publish(event))
        self._pending.add(task)
        task.add_done_callback(self._pending.discard)

    # ------------------------------------------------------------------ 订阅

    @contextlib.asynccontextmanager
    async def subscribe(self, *, requirement_id: UUID | None = None) -> AsyncIterator[Subscription]:
        sub = Subscription(requirement_id)
        self._subscribers.add(sub)
        try:
            yield sub
        finally:
            self._subscribers.discard(sub)

    def _fanout(self, event: Event) -> None:
        for sub in list(self._subscribers):
            if sub.matches(event):
                sub.offer(event)

    # ------------------------------------------------------------------ Redis 侧

    async def _read_redis(self) -> None:  # pragma: no cover - 需要真实 Redis
        assert self._redis is not None
        pubsub = self._redis.pubsub()
        await pubsub.subscribe(self._channel)
        try:
            async for message in pubsub.listen():
                if message.get("type") != "message":
                    continue
                try:
                    envelope = json.loads(message["data"])
                except (TypeError, ValueError):
                    logger.warning("dropping malformed event envelope")
                    continue
                if envelope.get("origin") == self._origin:
                    continue  # 自己发的，本地已经 fanout 过
                raw = envelope.get("event") or {}
                self._fanout(
                    Event(
                        type=str(raw.get("type", "unknown")),
                        payload=dict(raw.get("payload") or {}),
                        requirement_id=_maybe_uuid(raw.get("requirement_id")),
                        at=_maybe_dt(raw.get("at")),
                    )
                )
        except asyncio.CancelledError:
            raise
        except Exception:  # pragma: no cover - 网络故障
            logger.warning("redis event reader stopped unexpectedly", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await pubsub.aclose()


def _maybe_uuid(value: Any) -> UUID | None:
    try:
        return UUID(str(value)) if value else None
    except (TypeError, ValueError):
        return None


def _maybe_dt(value: Any) -> datetime:
    try:
        parsed = datetime.fromisoformat(str(value))
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)
    except (TypeError, ValueError):
        return datetime.now(UTC)


# ---------------------------------------------------------------- 全局装配
#
# 用模块级单例而不是把 bus 一路透传：事件是旁路，Service / Orchestrator
# 在业务路径上 emit 时不该为了拿一个 bus 而修改十几个构造函数签名。
# 测试里可以 configure_event_bus(EventBus()) 换掉。

_bus: EventBus | None = None


def configure_event_bus(bus: EventBus | None) -> None:
    global _bus
    _bus = bus


def get_event_bus() -> EventBus:
    global _bus
    if _bus is None:
        # 兜底：没有显式装配时也能工作（例如单元测试直接调 Service）
        _bus = EventBus()
    return _bus
