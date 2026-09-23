"""接口限流。

Layer: Common（横切关注点）。

### 为什么现在才做，以及为什么必须做

Stage 1~4 是本地单人使用，限流只会碍事。**部署给多人用**是另一回事：
每个用户都能触发会真实调模型的操作（一次 20~40 秒、花真金白银），
一个手滑的循环就能把额度和机器一起打满。所以限流是「多人可用」的前置条件，
不是优化项。

### 三档限额，不是一档

不同接口的代价差着数量级，用同一个限额必然要么放开（等于没限）、要么误伤：

| 档位 | 匹配 | 为什么单独设 |
|---|---|---|
| ``auth`` | 登录 / 注册 | 反口令爆破。按 IP 计，因为这时还没有身份 |
| ``llm`` | analyze / plan / start / resume | 每次调用 20~40 秒 + 花 token，最贵的资源 |
| ``default`` | 其余接口 | 防脚本乱扫 |

### 谁被限：登录用户按「令牌指纹」，匿名按 IP

用 ``sha256(token)[:16]`` 而不是用户 id：解析 JWT 需要密钥与一次校验，
而限流发生在中间件里、早于任何业务逻辑 —— 用令牌指纹既有稳定的身份粒度，
又不必在中间件里引入认证依赖。

### Redis 与降级

单进程用内存实现即可；**多 worker 必须用 Redis**，否则限额被乘以 worker 数。
Redis 不可用时**放行并告警**（fail-open）：限流是保护措施，
让保护措施的故障变成全站不可用，是拿小风险换大风险。这个取舍是显式的，
不是忘了处理 —— 见 ``rate_limit_fail_open``。
"""

from __future__ import annotations

import hashlib
import logging
import re
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import unquote

from starlette.types import ASGIApp, Receive, Scope, Send

from app.common.request_context import get_request_id
from app.infrastructure.redis import RedisLike

__all__ = [
    "InMemoryRateLimiter",
    "RateLimitMiddleware",
    "RateLimitResult",
    "RateLimiter",
    "RedisRateLimiter",
    "classify_request",
]

logger = logging.getLogger(__name__)

# 会真实调模型的接口：每次 20~40 秒、花 token
_LLM_PATHS = re.compile(
    r"^/[^/]+/api/v1/(requirements/[^/]+/(analyze|plan)|runs/[^/]+/(start|resume))$|"
    r"^/api/v1/(requirements/[^/]+/(analyze|plan)|runs/[^/]+/(start|resume))$"
)
# 反爆破：登录 / 注册
_AUTH_PATHS = re.compile(r"^/(?:[^/]+/)?api/v1/auth/(login|register)$")

# 完全不计流量的路径：健康检查要能被探针高频打，静态资源不该占限额
_EXEMPT_PREFIXES = ("/health", "/docs", "/redoc", "/openapi.json", "/ui", "/favicon")

TIER_DEFAULT = "default"
TIER_LLM = "llm"
TIER_AUTH = "auth"


@dataclass(frozen=True, slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    retry_after: int


class RateLimiter(Protocol):
    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitResult: ...


class InMemoryRateLimiter:
    """滑动窗口，单进程有效。

    时间戳队列而不是固定窗口计数器：固定窗口在窗口边界上能放过 2 倍流量
    （窗口末尾打满 + 下个窗口开头再打满），对 LLM 这种昂贵操作不够。
    """

    def __init__(self) -> None:
        self._hits: dict[str, deque[float]] = defaultdict(deque)

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        now = time.monotonic()
        cutoff = now - window_seconds
        bucket = self._hits[key]
        while bucket and bucket[0] <= cutoff:
            bucket.popleft()

        if len(bucket) >= limit:
            retry_after = max(1, int(window_seconds - (now - bucket[0])) + 1)
            return RateLimitResult(False, limit, 0, retry_after)

        bucket.append(now)
        return RateLimitResult(True, limit, limit - len(bucket), 0)


class RedisRateLimiter:
    """固定窗口计数器（INCR + EXPIRE）。

    Redis 侧刻意用固定窗口：多 worker 共享一个计数器需要原子性，
    ``INCR`` + 首次 ``EXPIRE`` 是最简单且无竞态的做法（代价是边界处可能
    放过接近 2 倍流量，对「防打爆」这个目的足够）。
    """

    def __init__(self, redis: RedisLike) -> None:
        self._redis = redis

    async def hit(self, key: str, *, limit: int, window_seconds: int) -> RateLimitResult:
        count = await self._redis.incr(key)
        if count == 1:
            await self._redis.expire(key, window_seconds)
        if count > limit:
            ttl = await self._redis.ttl(key)
            return RateLimitResult(False, limit, 0, max(1, int(ttl)))
        return RateLimitResult(True, limit, max(0, limit - count), 0)


def classify_request(path: str) -> str | None:
    """返回档位；``None`` 表示该路径不计流量。"""
    if path.startswith(_EXEMPT_PREFIXES):
        return None
    if _LLM_PATHS.match(path):
        return TIER_LLM
    if _AUTH_PATHS.match(path):
        return TIER_AUTH
    return TIER_DEFAULT


def client_key(scope: Scope, tier: str) -> str:
    """限流键：登录用户用令牌指纹，匿名用 IP。"""
    if tier != TIER_AUTH:
        for name, value in scope.get("headers") or []:
            if name == b"authorization":
                raw = value.decode("latin-1")
                if raw.lower().startswith("bearer "):
                    digest = hashlib.sha256(raw[7:].encode("utf-8")).hexdigest()[:16]
                    return f"tok:{digest}"

    client = scope.get("client") or ("unknown", 0)
    forwarded = None
    if scope.get("_trust_proxy"):
        for name, value in scope.get("headers") or []:
            if name == b"x-forwarded-for":
                forwarded = unquote(value.decode("latin-1")).split(",")[0].strip()
    return f"ip:{forwarded or client[0]}"


class RateLimitMiddleware:
    """纯 ASGI 中间件。

    和 ``RequestContextMiddleware`` 一样避开 ``BaseHTTPMiddleware``。
    **装配顺序**：本中间件要更靠内层（先 add），这样它拿到的 request_id
    是已经生成好的 —— 否则被限流的响应里 request_id 是 ``-``，
    而「谁触发了限流」正是最需要对着日志查的事情。
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        limiter: RateLimiter,
        limits: dict[str, int],
        window_seconds: int = 60,
        enabled: bool = True,
        trust_proxy: bool = False,
        fail_open: bool = True,
    ) -> None:
        self.app = app
        self._limiter = limiter
        self._limits = limits
        self._window = window_seconds
        self._enabled = enabled
        self._trust_proxy = trust_proxy
        self._fail_open = fail_open

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not self._enabled:
            await self.app(scope, receive, send)
            return

        tier = classify_request(scope.get("path", ""))
        if tier is None:
            await self.app(scope, receive, send)
            return

        limit = self._limits.get(tier, 0)
        if limit <= 0:
            await self.app(scope, receive, send)
            return

        scope["_trust_proxy"] = self._trust_proxy
        key = f"rl:{tier}:{client_key(scope, tier)}"

        try:
            result = await self._limiter.hit(key, limit=limit, window_seconds=self._window)
        except Exception:
            if not self._fail_open:  # pragma: no cover - 需要显式关闭 fail-open
                raise
            logger.warning(
                "rate limiter unavailable, failing open | tier=%s key=%s", tier, key, exc_info=True
            )
            await self.app(scope, receive, send)
            return

        if not result.allowed:
            await self._reject(scope, send, result, tier)
            return

        # 通过时也回限额头，让客户端能自己退避而不是等撞墙
        await self.app(scope, receive, _with_headers(send, tier, result))

    async def _reject(self, scope: Scope, send: Send, result: RateLimitResult, tier: str) -> None:
        from starlette.responses import JSONResponse

        logger.warning(
            "request rate limited | tier=%s path=%s retry_after=%ss",
            tier,
            scope.get("path"),
            result.retry_after,
        )
        response = JSONResponse(
            status_code=429,
            content={
                "error": {
                    "code": "RATE_LIMITED",
                    "message": f"Too many requests ({tier} limit: {result.limit}/{self._window}s)",
                    "request_id": get_request_id(),
                    "details": {"tier": tier, "retry_after": result.retry_after},
                }
            },
            headers={
                "Retry-After": str(result.retry_after),
                "X-RateLimit-Limit": str(result.limit),
                "X-RateLimit-Remaining": "0",
            },
        )
        await response(scope, _noop_receive, send)


async def _noop_receive() -> dict[str, Any]:  # pragma: no cover - 直接回响应不需要 body
    return {"type": "http.request", "body": b""}


def _with_headers(send: Send, tier: str, result: RateLimitResult) -> Send:
    async def wrapper(message: dict[str, Any]) -> None:
        if message["type"] == "http.response.start":
            headers = list(message.get("headers") or [])
            headers.append((b"x-ratelimit-limit", str(result.limit).encode()))
            headers.append((b"x-ratelimit-remaining", str(result.remaining).encode()))
            message = {**message, "headers": headers}
        await send(message)

    return wrapper
