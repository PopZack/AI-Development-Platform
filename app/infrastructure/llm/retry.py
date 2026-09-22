"""传输层重试包装。

Layer: Infrastructure。

**必须和「内容层重试」分清楚**，这是两个完全不同的问题：

| | 传输层重试（这里） | 内容层重试（Agent Runtime） |
|---|---|---|
| 触发条件 | 超时、网络抖动、5xx、429 | 模型回的内容不是合法 JSON / 不合 Schema |
| 重试有意义吗 | 有 —— 抖动是随机的 | 有，但**要改 Prompt 或告诉模型哪里错了**才有意义 |
| 谁负责 | 这个装饰器 | ``app/agent/runtime.py`` |

把两者混成一个「最多重试 N 次」会很糟：模型稳定地回错误 JSON 时，
传输层会白等三轮退避；而网络抖动时，内容层又会去重写 Prompt。

只重试 ``retryable = True`` 的异常。401/403/400 一次都不重试 ——
它们重试一万次也一样失败，只是把真正的配置问题埋进重试日志里。
"""

from __future__ import annotations

import asyncio
import logging

from app.common.exceptions import AppError
from app.infrastructure.llm.base import LLMProvider, LLMRequest, LLMResponse

__all__ = ["RetryingLLMProvider"]

logger = logging.getLogger(__name__)


class RetryingLLMProvider(LLMProvider):
    def __init__(
        self,
        inner: LLMProvider,
        *,
        max_retries: int = 2,
        backoff_seconds: float = 0.5,
        sleep=asyncio.sleep,
    ) -> None:
        self._inner = inner
        self.name = inner.name
        self._max_retries = max(0, max_retries)
        self._backoff = backoff_seconds
        # 可注入：测试里换成不真睡的假实现，否则退避会让测试白白变慢
        self._sleep = sleep

    async def complete(self, request: LLMRequest) -> LLMResponse:
        attempt = 0
        while True:
            try:
                return await self._inner.complete(request)
            except AppError as exc:
                if not getattr(exc, "retryable", False) or attempt >= self._max_retries:
                    raise
                attempt += 1
                delay = self._backoff * (2 ** (attempt - 1))
                logger.warning(
                    "llm call failed, retrying | provider=%s code=%s attempt=%s/%s delay=%.1fs",
                    self.name,
                    exc.code,
                    attempt,
                    self._max_retries,
                    delay,
                )
                await self._sleep(delay)

    async def aclose(self) -> None:
        await self._inner.aclose()

    @property
    def closed(self) -> bool:
        """转发给被包装的 provider。

        必须转发，不能继承基类那个恒为 False 的默认实现 —— 否则包装之后
        「连接关了吗」永远答 False，资源泄漏检查会给出假警报
        （实测踩过：lifespan 里明明已经 aclose，检查结果却是 False）。
        包装器对调用方应当是透明的。
        """
        return self._inner.closed
