"""Redis 客户端（可选依赖）。

Layer: Infrastructure。

**为什么是「可选」**：本地单进程跑不需要 Redis —— 限流走内存实现、
SSE 事件走进程内广播，功能完整。但**多 worker 部署必须有它**：

- 每个 worker 各算各的限流额度 ⇒ 实际限额被乘以 worker 数
- SSE 事件只在「产生事件的那个 worker」上可见 ⇒ 客户端连到另一个 worker 就收不到

本模块只负责建客户端，不负责判断「该不该用 Redis」—— 那是调用方的决定，
而且这个决定要能从配置上看出来（见 settings.redis_url）。
"""

from __future__ import annotations

import logging
from typing import Any, Protocol, runtime_checkable

from app.common.security import mask_url

__all__ = ["RedisLike", "create_redis"]

logger = logging.getLogger(__name__)


@runtime_checkable
class RedisLike(Protocol):
    """限流与事件广播用到的最小接口。

    刻意不直接依赖 ``redis.asyncio.Redis``：一是不必为测试拉起 Redis 服务，
    二是不让 redis-py 的完整 API 渗透进来 —— 用到的就这几个方法。
    """

    async def incr(self, key: str) -> int: ...

    async def expire(self, key: str, seconds: int) -> bool: ...

    async def ttl(self, key: str) -> int: ...

    async def publish(self, channel: str, message: str) -> int: ...

    def pubsub(self) -> Any: ...

    async def aclose(self) -> None: ...


def create_redis(url: str) -> Any | None:
    """按 URL 建客户端；未配置或未安装 redis 包时返回 ``None``。

    未安装时**不报错**而是降级 + 告警：本地开发不该因为「没装 redis 包」
    起不来。但配置了 URL 却拿不到客户端，是配置错误，必须看得见。
    """
    if not url.strip():
        return None

    try:
        from redis.asyncio import from_url
    except ImportError:
        logger.warning(
            "REDIS_URL is set but the 'redis' package is not installed; "
            "falling back to in-process rate limiting and event broadcast"
        )
        return None

    # decode_responses=True：事件信封是 JSON 字符串，拿到 bytes 还得手动解一次
    client = from_url(url, decode_responses=True)
    logger.info("redis client created | url=%s", mask_url(url))
    return client
