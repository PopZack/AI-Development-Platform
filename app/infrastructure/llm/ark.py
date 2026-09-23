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

import json
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
            # ⚠️ 流式是**必需的**，不是性能优化。
            #
            # httpx 的 timeout 是**读超时**（两次数据到达之间的最大间隔），不是总时长。
            # 非流式下「服务端把整包算完才发第一个字节」，于是读超时退化成总时长上限；
            # 而一次 Agent 调用的服务端推理期实测可以到 300s+（deepseek-v4-flash
            # 的推理 token 也算在 completion_tokens 里）→ 客户端在整段推理期里
            # 收不到任何一个字节，180s 读超时必然触发，而且重试只是把同样的
            # 长请求再压一遍（实测 3 次尝试白等 540s）。
            #
            # 流式把这个语义换成「等下一个数据块」——探针实测块间最大间隔 0.5s，
            # 首字节 1.3s。同一个 timeout 值，含义完全不同。
            "stream": True,
            # 流式下 usage 只在最后一块给；不要它就得自己估 token，
            # 而 agent_runs 的用量统计是要给人看的
            "stream_options": {"include_usage": True},
        }
        if request.max_tokens is not None:
            # ⚠️ 这个参数**本端点实测不生效**（请求 60，实得 568 个 completion token），
            # 见 scripts/probe_llm_endpoint.py。留着是为了「端点哪天支持了」，
            # 但**不要指望它兜住输出规模**。
            payload["max_tokens"] = request.max_tokens
        if request.json_mode:
            # 只是「请求」模型输出 JSON。有些模型不支持，上游会回 400，
            # 我们映射成 LLMBadRequestError 如实报错 —— 不静默忽略，
            # 否则上层会以为拿到了 JSON 保证
            payload["response_format"] = {"type": "json_object"}

        try:
            async with self._client.stream(
                "POST",
                self._url,
                json=payload,
                # 绝不把 key 写进日志：下面所有 logger 调用都只打状态码和错误摘要
                headers={
                    "Authorization": f"Bearer {self._api_key}",
                    "Content-Type": "application/json",
                },
            ) as response:
                if response.status_code >= 400:
                    # 流式响应体默认没读；_map_error 要构造错误摘要就必须先把 body 拉出来
                    await response.aread()
                    raise self._map_error(response)
                return await self._consume_stream(response)
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

    async def _consume_stream(self, response: httpx.Response) -> LLMResponse:
        """把 SSE 流拼成一次完整的响应。

        对外仍然给一个「非流式形状」的 ``LLMResponse`` —— **上游是不是流式，
        不该渗透到上层**。上层只关心：内容是什么、用了多少 token。
        """
        parts: list[str] = []
        usage_body: dict[str, Any] | None = None
        model = self._model
        saw_chunk = False
        saw_reasoning = False

        async for line in response.aiter_lines():
            if not line.startswith("data:"):
                # SSE 注释与心跳（有些网关会插 keep-alive）直接跳过
                continue
            data = line[5:].strip()
            if not data or data == "[DONE]":
                continue
            try:
                body = json.loads(data)
            except json.JSONDecodeError:
                # 单个坏块不让整次调用失败：拼出来的内容最终要过 Pydantic 校验，
                # 真坏了会在内容层暴露（并带着「上次输出」重试），
                # 而在这里判成传输错误反而会丢掉已经收到的内容
                logger.warning("ark streamed a non-JSON chunk | model=%s", self._model)
                continue

            if body.get("error"):
                # 流中途报错（少数网关这么干）。它没法带 HTTP 状态码，
                # 所以只能当传输错误 —— 但把上游原文带上，别让人猜
                raise LLMTransportError(
                    "Ark streamed an error",
                    details={
                        "model": self._model,
                        "upstream": json.dumps(body["error"], ensure_ascii=False)[:300],
                    },
                )

            if body.get("model"):
                model = str(body["model"])
            if body.get("usage"):
                usage_body = body["usage"]

            choices = body.get("choices") or []
            if not choices:
                # 带 usage 的收尾块就是这种形状（choices 为空），不算「没有数据块」
                continue

            saw_chunk = True
            delta = choices[0].get("delta") or {}
            piece = delta.get("content")
            if piece:
                parts.append(piece)
            elif delta.get("reasoning_content"):
                # 模型内部推理。**刻意不拼进 content** —— 它是思考过程不是答案，
                # 拼进去会污染交给 Pydantic 的 JSON。但要知道它在发生：
                # 这是「这次为什么这么慢」的直接解释（实测出现过 300s 的推理期）
                saw_reasoning = True

        if not saw_chunk:
            # 一个数据块都没有 = 响应结构不符预期。**绝不能静默返回空内容** ——
            # 那会变成一个「Agent 输出校验失败」的假象，把排查方向指错
            raise LLMTransportError(
                "Ark returned an unexpected response shape (no streamed chunks)",
                details={"url": self._url, "model": self._model},
            )
        if saw_reasoning:
            logger.info("ark streamed reasoning_content before/among content | model=%s", model)

        content = "".join(parts)
        return LLMResponse(
            content=content,
            model=model,
            usage=LLMUsage.from_openai_like(usage_body),
            # raw 统一成非流式响应的形状：调用方与测试都按这个形状读全文，
            # 「模型到底回了什么」要看得到
            raw={
                "model": model,
                "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
                "usage": usage_body,
            },
        )

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
