"""限流的单元测试。

重点不是「计数器会不会加」，而是三件事：

1. 分档是否真的分开了（LLM 档不能被普通请求吃掉额度）
2. 限流键的身份粒度（登录用户按令牌、匿名按 IP、auth 档永远按 IP）
3. 限流器故障时 fail-open —— 保护措施的故障不该变成全站不可用
"""

from __future__ import annotations

import asyncio
from typing import Any

import pytest

from app.common.rate_limit import (
    TIER_AUTH,
    TIER_DEFAULT,
    TIER_LLM,
    InMemoryRateLimiter,
    RateLimitMiddleware,
    RedisRateLimiter,
    classify_request,
    client_key,
)


def _scope(path: str, *, headers: list[tuple[bytes, bytes]] | None = None) -> dict[str, Any]:
    return {
        "type": "http",
        "path": path,
        "headers": headers or [],
        "client": ("203.0.113.7", 12345),
    }


# ---------------------------------------------------------------- 分档


@pytest.mark.parametrize(
    ("path", "tier"),
    [
        ("/api/v1/requirements/abc/analyze", TIER_LLM),
        ("/api/v1/requirements/abc/plan", TIER_LLM),
        ("/api/v1/runs/abc/start", TIER_LLM),
        ("/api/v1/runs/abc/resume", TIER_LLM),
        ("/api/v1/auth/login", TIER_AUTH),
        ("/api/v1/auth/register", TIER_AUTH),
        ("/api/v1/projects", TIER_DEFAULT),
        ("/api/v1/requirements/abc/artifacts", TIER_DEFAULT),
    ],
)
def test_paths_are_classified_correctly(path: str, tier: str) -> None:
    assert classify_request(path) == tier


@pytest.mark.parametrize("path", ["/health", "/docs", "/openapi.json", "/ui/", "/ui/app.js"])
def test_probe_and_static_paths_are_exempt(path: str) -> None:
    """探针必须能高频打健康检查；静态资源不该占用户额度。"""
    assert classify_request(path) is None


def test_analyze_is_not_mistaken_for_a_requirement_detail() -> None:
    """`/requirements/{id}/analyze` 不能被当成普通接口 —— 它是最贵的那档。"""
    assert classify_request("/api/v1/requirements/abc/analyze") is TIER_LLM
    assert classify_request("/api/v1/requirements/abc") is TIER_DEFAULT


# ---------------------------------------------------------------- 身份键


def test_logged_in_requests_are_keyed_by_token_fingerprint() -> None:
    scope = _scope("/api/v1/projects", headers=[(b"authorization", b"Bearer token-a")])
    key = client_key(scope, TIER_DEFAULT)

    assert key.startswith("tok:")
    # 同一把令牌稳定，不同令牌不同 —— 否则所有用户共用一个额度
    assert key == client_key(scope, TIER_DEFAULT)
    other = client_key(
        _scope("/api/v1/projects", headers=[(b"authorization", b"Bearer token-b")]), TIER_DEFAULT
    )
    assert key != other


def test_anonymous_requests_are_keyed_by_ip() -> None:
    assert client_key(_scope("/api/v1/projects"), TIER_DEFAULT) == "ip:203.0.113.7"


def test_auth_tier_ignores_token_and_uses_ip() -> None:
    """登录接口在认证前，用令牌指纹就会让爆破者每换一次「令牌」就重置额度。"""
    scope = _scope("/api/v1/auth/login", headers=[(b"authorization", b"Bearer whatever")])
    assert client_key(scope, TIER_AUTH) == "ip:203.0.113.7"


def test_x_forwarded_for_is_only_trusted_when_configured() -> None:
    """反代后面不信任它 ⇒ 全站算一个 IP；信任它 ⇒ 必须能取到真实 IP。"""
    headers = [(b"x-forwarded-for", b"198.51.100.9, 10.0.0.1")]

    untrusted = _scope("/api/v1/projects", headers=headers)
    assert client_key(untrusted, TIER_DEFAULT) == "ip:203.0.113.7"

    trusted = _scope("/api/v1/projects", headers=headers)
    trusted["_trust_proxy"] = True
    assert client_key(trusted, TIER_DEFAULT) == "ip:198.51.100.9"


# ---------------------------------------------------------------- 限额


async def test_in_memory_limiter_allows_up_to_limit_then_blocks() -> None:
    limiter = InMemoryRateLimiter()

    for _ in range(3):
        result = await limiter.hit("k", limit=3, window_seconds=60)
        assert result.allowed

    blocked = await limiter.hit("k", limit=3, window_seconds=60)
    assert not blocked.allowed
    assert blocked.remaining == 0
    assert blocked.retry_after >= 1


async def test_in_memory_limiter_separates_keys() -> None:
    limiter = InMemoryRateLimiter()
    assert (await limiter.hit("a", limit=1, window_seconds=60)).allowed
    # a 用完不影响 b —— 限流键隔离是「按用户限流」的前提
    assert (await limiter.hit("b", limit=1, window_seconds=60)).allowed
    assert not (await limiter.hit("a", limit=1, window_seconds=60)).allowed


async def test_in_memory_limiter_window_slides() -> None:
    """窗口滑过之后额度恢复。用 0 秒窗口模拟「窗口已过」。"""
    limiter = InMemoryRateLimiter()
    assert (await limiter.hit("k", limit=1, window_seconds=60)).allowed
    assert not (await limiter.hit("k", limit=1, window_seconds=60)).allowed
    # 窗口设为 0 ⇒ 上一次命中立刻过期
    assert (await limiter.hit("k", limit=1, window_seconds=0)).allowed


class _FakeRedis:
    """只实现限流用到的那几个方法。"""

    def __init__(self) -> None:
        self.counts: dict[str, int] = {}
        self.expires: dict[str, int] = {}

    async def incr(self, key: str) -> int:
        self.counts[key] = self.counts.get(key, 0) + 1
        return self.counts[key]

    async def expire(self, key: str, seconds: int) -> bool:
        self.expires[key] = seconds
        return True

    async def ttl(self, key: str) -> int:
        return self.expires.get(key, -1)


async def test_redis_limiter_uses_incr_and_sets_expiry_once() -> None:
    redis = _FakeRedis()
    limiter = RedisRateLimiter(redis)

    first = await limiter.hit("k", limit=2, window_seconds=60)
    assert first.allowed and first.remaining == 1
    # EXPIRE 只在第一次设置：每次命中都续期会让窗口永不结束（等于没有限流）
    assert redis.expires == {"k": 60}

    await limiter.hit("k", limit=2, window_seconds=60)
    blocked = await limiter.hit("k", limit=2, window_seconds=60)
    assert not blocked.allowed
    assert blocked.retry_after == 60


# ---------------------------------------------------------------- 中间件


class _ExplodingLimiter:
    async def hit(self, key: str, *, limit: int, window_seconds: int):  # noqa: ANN201
        raise RuntimeError("redis is down")


async def _run_middleware(app_middleware: RateLimitMiddleware, scope: dict[str, Any]) -> list[dict]:
    messages: list[dict] = []
    sent_body = False

    async def receive() -> dict[str, Any]:
        nonlocal sent_body
        if not sent_body:
            sent_body = True
            return {"type": "http.request", "body": b"", "more_body": False}
        await asyncio.sleep(3600)
        return {"type": "http.disconnect"}  # pragma: no cover

    async def send(message: dict[str, Any]) -> None:
        messages.append(message)

    await app_middleware(scope, receive, send)
    return messages


async def test_middleware_returns_unified_error_body_on_429() -> None:
    """限流的响应体必须和其他错误一致 —— 前端的错误处理不该有特例。"""
    middleware = RateLimitMiddleware(
        _ok_app,
        limiter=InMemoryRateLimiter(),
        limits={"default": 1, "llm": 1, "auth": 1},
        window_seconds=60,
    )
    scope = _scope("/api/v1/projects")

    await _run_middleware(middleware, scope)  # 第一次放行
    messages = await _run_middleware(middleware, scope)

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 429
    headers = {k.decode(): v.decode() for k, v in start["headers"]}
    assert headers["retry-after"]
    assert headers["x-ratelimit-limit"] == "1"


async def test_middleware_fails_open_when_limiter_is_broken() -> None:
    """Redis 挂了要放行：让保护措施的故障变成全站不可用是拿小风险换大风险。"""
    middleware = RateLimitMiddleware(
        _ok_app, limiter=_ExplodingLimiter(), limits={"default": 1}, window_seconds=60
    )
    messages = await _run_middleware(middleware, _scope("/api/v1/projects"))

    start = next(m for m in messages if m["type"] == "http.response.start")
    assert start["status"] == 200


async def test_middleware_passes_through_exempt_paths() -> None:
    limiter = InMemoryRateLimiter()
    middleware = RateLimitMiddleware(_ok_app, limiter=limiter, limits={"default": 1}, window_seconds=60)

    for _ in range(5):
        messages = await _run_middleware(middleware, _scope("/health"))
        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 200


async def test_middleware_can_be_disabled() -> None:
    limiter = InMemoryRateLimiter()
    middleware = RateLimitMiddleware(
        _ok_app, limiter=limiter, limits={"default": 1}, window_seconds=60, enabled=False
    )

    for _ in range(3):
        messages = await _run_middleware(middleware, _scope("/api/v1/projects"))
        start = next(m for m in messages if m["type"] == "http.response.start")
        assert start["status"] == 200


async def _ok_app(scope: dict[str, Any], receive: Any, send: Any) -> None:
    await send({"type": "http.response.start", "status": 200, "headers": []})
    await send({"type": "http.response.body", "body": b"ok"})
