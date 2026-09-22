"""火山方舟（Ark）LLM Provider。

Layer: Infrastructure —— 这是**唯一**允许出现「火山方舟」四个字的地方
（除了配置）。Agent / Prompt / 校验层都不知道供应商的存在。

协议：Ark 的 ``/api/v3/chat/completions`` 是 OpenAI 兼容协议，所以直接发 HTTP。
刻意的选择：不引 openai / volcengine 官方 SDK —— 少一层依赖、少一处版本漂移，
而且各家 SDK 的异常类型不统一，抽象层反而更难做得干净。

⚠️ 两套端点不能混用（2026-09 官方文档）：

- 平台端点 ``/api/v3``        模型名带日期后缀，如 ``deepseek-v4-flash-260425``
- Coding Plan ``/api/coding/v3``  模型名是短名，如 ``deepseek-v4-flash``，
  **且专属 API Key 与平台 Key 不是同一个**

这是配置问题，代码不猜 —— ``llm_base_url`` / ``llm_model`` / ``llm_api_key`` 三者
必须由使用者配成同一套。配错的表现是 401（见 LLMCredentialError 的说明）。
"""

from __future__ import annotations

import logging
from typing import Any

import httpx

from app.config.settings import Settings
from app.infrastructure.llm.base import (
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)
from app.infrastructure.llm.errors import (
    LLMBadRequestError,
    LLMCredentialError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransportError,
)

__all__ = ["ArkLLMProvider"]

logger = logging.getLogger(__name__)

_CHAT_PATH = "/chat/completions"


class ArkLLMProvider(LLMProvider):
    name = "ark"

    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        timeout_seconds: float = 60.0,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        self._api_key = api_key
        self._model = model
        self._url = f"{base_url.rstrip('/')}{_CHAT_PATH}"
        # transport 可注入：测试用 httpx.MockTransport 断言「我们到底发了什么请求」，
        # 不用打真实网络，也不用为了可测性把 URL 拼装逻辑抽出去
        self._client = httpx.AsyncClient(timeout=timeout_seconds, transport=transport)

    @classmethod
    def from_settings(
        cls, settings: Settings, *, transport: httpx.AsyncBaseTransport | None = None
    ) -> ArkLLMProvider:
        """从配置装配。

        ``transport`` 是给测试留的接缝：有了它就能用 ``httpx.MockTransport`` 验证
        **配置到底被接到了哪个字段上**（比如 base_url 有没有被误传成 model）。
        这类接线错误光看代码很容易漏，而且真调起来只表现为一个 400/401。
        """
        return cls(
            api_key=settings.llm_api_key,
            base_url=settings.llm_base_url,
            model=settings.llm_model,
            timeout_seconds=settings.llm_timeout_seconds,
            transport=transport,
        )

    async def complete(self, request: LLMRequest) -> LLMResponse:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": [{"role": m.role, "content": m.content} for m in request.messages],
            "temperature": request.temperature,
        }
        if request.max_tokens is not None:
            payload["max_tokens"] = request.max_tokens
        if request.json_mode:
            # 只是「请求」模型输出 JSON。有些模型不支持，上游会回 400，
            # 我们映射成 LLMBadRequestError 如实报错 —— 不静默忽略，
            # 否则上层会以为拿到了 JSON 保证
            payload["response_format"] = {"type": "json_object"}

        try:
            response = await self._client.post(
                self._url,
                json=payload,
                # 绝不把 key 写进日志：下面所有 logger 调用都只打状态码和错误摘要
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            )
        except httpx.TimeoutException as exc:
            raise LLMTimeoutError(
                "Ark request timed out", details={"url": self._url, "model": self._model}
            ) from exc
        except httpx.HTTPError as exc:
            # 连不上 / DNS 失败 / 连接被重置 —— 可重试
            raise LLMTransportError(
                f"Ark request failed: {type(exc).__name__}",
                details={"url": self._url, "model": self._model},
            ) from exc

        if response.status_code >= 400:
            raise self._map_error(response)

        return self._parse(response)

    @staticmethod
    def _map_error(response: httpx.Response) -> Exception:
        """把上游状态码映射成「可重试 / 不可重试」两类。

        区分这一点很要紧：401 重试一百次也还是 401，只会把配置问题埋掉。
        """
        status = response.status_code
        detail = _short_body(response)
        request_id = response.headers.get("X-Request-Id") or response.headers.get("x-request-id")

        if status in (401, 403):
            return LLMCredentialError(
                "Ark rejected the API key or the key has no access to this model/endpoint",
                details={"status": status, "upstream": detail, "request_id": request_id},
            )
        if status == 429:
            return LLMRateLimitError(
                "Ark rate limit exceeded",
                details={"status": status, "request_id": request_id},
            )
        if 400 <= status < 500:
            return LLMBadRequestError(
                "Ark rejected the request (check model name and parameters)",
                details={"status": status, "upstream": detail, "request_id": request_id},
            )
        return LLMTransportError(
            "Ark returned a server error",
            details={"status": status, "upstream": detail, "request_id": request_id},
        )

    def _parse(self, response: httpx.Response) -> LLMResponse:
        try:
            body = response.json()
            content = body["choices"][0]["message"]["content"]
        except (ValueError, KeyError, IndexError, TypeError) as exc:
            # 结构不符预期。不回 200 让上层拿到空内容 —— 那会变成一个
            # 「Agent 输出校验失败」的假象，把真正的问题指向错误的方向
            raise LLMTransportError(
                "Ark returned an unexpected response shape",
                details={"body": response.text[:500]},
            ) from exc

        return LLMResponse(
            content=content or "",
            model=str(body.get("model") or self._model),
            usage=LLMUsage.from_openai_like(body.get("usage")),
            raw=body,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    @property
    def closed(self) -> bool:
        # 由 httpx 客户端回答，不自己记一个布尔量 —— 两份状态迟早会不一致
        return self._client.is_closed


def _short_body(response: httpx.Response, limit: int = 400) -> str:
    """截断上游响应体。完整的原始响应可能很大，也可能含不该进日志的内容。"""
    try:
        text = response.text
    except Exception:  # pragma: no cover - 极端情况下的兜底
        return "<unreadable>"
    return text[:limit]
