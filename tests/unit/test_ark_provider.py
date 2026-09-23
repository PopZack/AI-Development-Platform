"""Ark（火山方舟）Provider 的网络层测试。

**全部用 ``httpx.MockTransport`` 打桩，不打真实网络。** 这样能在没有有效密钥的情况下
验证三件真正重要的事：

1. **我们到底发出了什么请求** —— URL、认证头、请求体字段。Provider 抽象层最容易出的
   问题就是「请求构造错了」，而这类问题用真实调用很难定位（只会看到一个 400）。
2. **上游各种失败被映射成哪一类错误** —— 尤其要保证「不可重试」的类型没被误判成可重试。
3. **SSE 流的拼接** —— 本条是 2026-09-23 改流式时加的。原因见 ``ark.py`` 里
   「为什么必须 stream」：非流式下一次 Agent 调用有 300s+ 的服务端推理期，
   客户端整段收不到字节，读超时必然触发。改了流式之后，**流的边界就成了正确性的一部分**：
   跨块拼接、`[DONE]`、收尾 usage 块、流中途报错、坏块，都得有确定行为。

API Key 的**有效性与模型名归属**只能在真实调用里验证，那是单独的连通性检查，
不属于单元测试（它需要一个有效密钥，而且要花钱）。端点的行为（流式是否可用、
``max_tokens`` 是否生效）用 ``scripts/probe_llm_endpoint.py`` 量，不在这里断言。
"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.config.settings import Settings
from app.infrastructure.llm import (
    LLMBadRequestError,
    LLMCredentialError,
    LLMMessage,
    LLMRateLimitError,
    LLMRequest,
    LLMTimeoutError,
    LLMTransportError,
)
from app.infrastructure.llm.ark import ArkLLMProvider

API_KEY = "ark-super-secret-key-do-not-leak"
BASE_URL = "https://ark.cn-beijing.volces.com/api/v3"
MODEL = "deepseek-v4-flash-260425"

DEFAULT_CONTENT = '{"title": "ok"}'
DEFAULT_USAGE = {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33}


def _sse(
    *pieces: str,
    usage: dict[str, Any] | None = None,
    done: bool = True,
    reasoning: str | None = None,
    raw_lines: list[str] | None = None,
) -> bytes:
    """拼一段 SSE 响应体。

    真实的流是「每个 chunk 一个 ``data:`` 行、事件之间空行」，最后一块带 usage
    （此时 ``choices`` 是空数组），再以 ``data: [DONE]`` 收尾。
    ``raw_lines`` 用来塞入刻意畸形的行（坏 JSON、心跳注释）。
    """
    events: list[str] = []
    if reasoning is not None:
        events.append(
            json.dumps(
                {"model": MODEL, "choices": [{"index": 0, "delta": {"reasoning_content": reasoning}}]},
                ensure_ascii=False,
            )
        )
    for piece in pieces:
        events.append(
            json.dumps(
                {"model": MODEL, "choices": [{"index": 0, "delta": {"content": piece}}]},
                ensure_ascii=False,
            )
        )
    for line in raw_lines or []:
        events.append(line)
    if usage is not None:
        events.append(json.dumps({"model": MODEL, "choices": [], "usage": usage}))
    if done:
        events.append("[DONE]")

    body = ": ping\n\n" + "".join(f"data: {event}\n\n" for event in events)
    return body.encode("utf-8")


class _Recorder:
    """记录 provider 实际发出的请求，并回放指定的响应。"""

    def __init__(
        self,
        *,
        status: int = 200,
        raw: bytes | None = None,
        json_body: Any = None,
        raise_exc: Exception | None = None,
    ):
        self.request: httpx.Request | None = None
        self._status = status
        # 三者取一：raw（SSE 字节）> json_body（非流式 JSON）> 默认的正常 SSE
        self._raw = raw if raw is not None else (None if json_body is not None else _sse(DEFAULT_CONTENT))
        self._json_body = json_body
        self._raise = raise_exc

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        if self._raise is not None:
            raise self._raise
        headers = {"X-Request-Id": "req-ark-1", "Content-Type": "text/event-stream"}
        if self._raw is not None:
            return httpx.Response(self._status, content=self._raw, headers=headers)
        return httpx.Response(self._status, json=self._json_body, headers=headers)

    @property
    def payload(self) -> dict[str, Any]:
        assert self.request is not None, "provider 没有发出请求"
        return json.loads(self.request.content)

    @property
    def auth_header(self) -> str:
        assert self.request is not None
        return self.request.headers.get("Authorization", "")


def _provider(recorder: _Recorder, **overrides: Any) -> ArkLLMProvider:
    kwargs: dict[str, Any] = {
        "api_key": API_KEY,
        "base_url": BASE_URL,
        "model": MODEL,
        "timeout_seconds": 5.0,
        "transport": httpx.MockTransport(recorder.handler),
    }
    kwargs.update(overrides)
    return ArkLLMProvider(**kwargs)


def _request(**kwargs: Any) -> LLMRequest:
    kwargs.setdefault("messages", [LLMMessage.system("你是需求分析师"), LLMMessage.user("做一个待办接口")])
    return LLMRequest(**kwargs)


# ------------------------------------------------------------ 请求构造


async def test_request_url_and_auth_header() -> None:
    recorder = _Recorder()
    provider = _provider(recorder)

    await provider.complete(_request())

    assert str(recorder.request.url) == f"{BASE_URL}/chat/completions"
    # Bearer 后恰好一个空格 —— 多一个少一个都会被 Ark 判 401
    assert recorder.auth_header == f"Bearer {API_KEY}"
    assert recorder.request.headers["Content-Type"] == "application/json"
    await provider.aclose()


async def test_base_url_trailing_slash_does_not_double_up() -> None:
    """配置里多写一个斜杠不该拼出 //chat/completions。"""
    recorder = _Recorder()
    provider = _provider(recorder, base_url=f"{BASE_URL}/")

    await provider.complete(_request())

    assert str(recorder.request.url) == f"{BASE_URL}/chat/completions"
    await provider.aclose()


async def test_request_is_streaming_and_asks_for_usage() -> None:
    """**流式不是可选项。**

    非流式下客户端要等服务端把整包算完才拿到第一个字节，而实测一次 Agent 调用
    有 300s+ 的服务端推理期 —— 180s 的读超时必然触发，重试也只是把长请求再压一遍。
    所以这里断言请求体里必须带 ``stream``；usage 则靠 ``stream_options`` 要，
    否则 agent_runs 的用量统计全是 0。
    """
    recorder = _Recorder()
    provider = _provider(recorder)

    await provider.complete(_request())

    assert recorder.payload["stream"] is True
    assert recorder.payload["stream_options"] == {"include_usage": True}
    await provider.aclose()


async def test_request_body_carries_model_messages_and_roles() -> None:
    recorder = _Recorder()
    provider = _provider(recorder)

    await provider.complete(_request(temperature=0.7, max_tokens=512))

    payload = recorder.payload
    assert payload["model"] == MODEL
    assert payload["temperature"] == 0.7
    assert payload["max_tokens"] == 512
    assert payload["messages"] == [
        {"role": "system", "content": "你是需求分析师"},
        {"role": "user", "content": "做一个待办接口"},
    ]
    assert "response_format" not in payload
    await provider.aclose()


async def test_json_mode_adds_response_format() -> None:
    recorder = _Recorder(raw=_sse('{"ok": true}'))
    provider = _provider(recorder)

    await provider.complete(_request(json_mode=True))

    assert recorder.payload["response_format"] == {"type": "json_object"}
    # 探针实测 json_mode 与流式可以共存（HTTP 200 且返回 JSON），所以两者同时发
    assert recorder.payload["stream"] is True
    await provider.aclose()


async def test_max_tokens_omitted_when_not_set() -> None:
    recorder = _Recorder()
    provider = _provider(recorder)

    await provider.complete(_request())

    assert "max_tokens" not in recorder.payload
    await provider.aclose()


# ------------------------------------------------------------ SSE 拼接


async def test_chunks_are_concatenated_across_events() -> None:
    """内容块是按 token 流式下发的，拼接必须无损 —— 包括中文与跨块的转义。"""
    recorder = _Recorder(raw=_sse('{"title": "实现', "待办 API", '"}'))
    provider = _provider(recorder)

    response = await provider.complete(_request())

    assert response.content == '{"title": "实现待办 API"}'
    await provider.aclose()


async def test_usage_is_taken_from_the_final_chunk() -> None:
    """usage 只在收尾块给，且那一块的 choices 是空数组 —— 不能因为「没有 delta」就丢掉它。"""
    recorder = _Recorder(raw=_sse('{"ok": true}', usage=DEFAULT_USAGE))
    provider = _provider(recorder)

    response = await provider.complete(_request())

    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 22
    assert response.usage.total_tokens == 33
    await provider.aclose()


async def test_usage_defaults_to_zero_when_absent() -> None:
    """端点不给 usage 时不能炸 —— 退化成 0，只是统计不准。"""
    recorder = _Recorder(raw=_sse('{"ok": true}', usage=None))
    provider = _provider(recorder)

    response = await provider.complete(_request())

    assert response.content == '{"ok": true}'
    assert response.usage.total_tokens == 0
    await provider.aclose()


async def test_done_marker_and_keepalive_comments_are_ignored() -> None:
    """``[DONE]`` 与 SSE 注释（网关的 keep-alive 心跳）都不能进内容。"""
    recorder = _Recorder(raw=_sse('{"a"', ": 1}", raw_lines=["", "[DONE]"]))
    provider = _provider(recorder)

    response = await provider.complete(_request())

    assert response.content == '{"a": 1}'
    await provider.aclose()


async def test_reasoning_content_never_leaks_into_content() -> None:
    """模型内部推理**不能**拼进 content。

    deepseek 这代模型会下发 ``reasoning_content``，实测一次调用可以有 300s+ 的推理期
    （completion_tokens 里大部分是它）。把它拼进 content 会直接污染交给 Pydantic 的 JSON，
    表现成「输出校验失败」，而真正的原因是我们自己拼错了。
    """
    recorder = _Recorder(raw=_sse('{"ok": true}', reasoning="先想一下……再想一下……"))
    provider = _provider(recorder)

    response = await provider.complete(_request())

    assert response.content == '{"ok": true}'
    assert "先想一下" not in response.content
    await provider.aclose()


async def test_malformed_chunk_is_skipped_but_rest_is_kept() -> None:
    """单个坏块跳过，不整次失败。

    拼出来的内容最终要过 Pydantic 校验；真坏了会在内容层带着「上次输出」重试。
    在这里判成传输错误反而会把已经收到的内容全丢掉。
    """
    recorder = _Recorder(raw=_sse('{"a"', ": 1}", raw_lines=["{不是合法 JSON"]))
    provider = _provider(recorder)

    response = await provider.complete(_request())

    assert response.content == '{"a": 1}'
    await provider.aclose()


async def test_empty_content_with_a_chunk_is_not_an_error() -> None:
    """收到数据块但内容为空 → 不是传输错误。

    合法的空回答是可能的（模型选择什么都不说），该由内容层去判定
    「这不是我要的 JSON」，而不是在这里武断地当网络问题。
    """
    recorder = _Recorder(raw=_sse(""))
    provider = _provider(recorder)

    response = await provider.complete(_request())

    assert response.content == ""
    await provider.aclose()


async def test_response_without_any_chunk_is_a_transport_error() -> None:
    """一个数据块都没有 = 结构不符预期。

    **不能静默返回空内容** —— 那会伪装成「Agent 输出校验失败」，
    把排查方向指向我们自己，而真正的问题是上游回了个完全不同的东西。
    """
    recorder = _Recorder(json_body={"unexpected": "shape"})
    provider = _provider(recorder)

    with pytest.raises(LLMTransportError):
        await provider.complete(_request())
    await provider.aclose()


async def test_raw_keeps_the_non_streaming_shape() -> None:
    """``raw`` 统一成非流式响应的形状 —— 上游是不是流式不该渗透到上层。

    「模型到底回了什么」排障时要看得到，而且调用方与测试都按同一个形状读。
    """
    recorder = _Recorder(raw=_sse('{"title": "实现 Todo API"}', usage=DEFAULT_USAGE))
    provider = _provider(recorder)

    response = await provider.complete(_request())

    assert response.raw["choices"][0]["message"]["content"] == '{"title": "实现 Todo API"}'
    assert response.raw["model"] == MODEL
    await provider.aclose()


# ------------------------------------------------------------ 失败映射


@pytest.mark.parametrize("status", [401, 403])
async def test_credential_errors_are_not_retryable(status: int) -> None:
    recorder = _Recorder(status=status, json_body={"error": {"message": "invalid key"}})
    provider = _provider(recorder)

    with pytest.raises(LLMCredentialError) as excinfo:
        await provider.complete(_request())

    assert excinfo.value.status_code == 502
    assert getattr(excinfo.value, "retryable", False) is False
    await provider.aclose()


async def test_rate_limit_is_retryable() -> None:
    recorder = _Recorder(status=429, json_body={"error": {"message": "too many requests"}})
    provider = _provider(recorder)

    with pytest.raises(LLMRateLimitError) as excinfo:
        await provider.complete(_request())

    assert getattr(excinfo.value, "retryable", False) is True
    await provider.aclose()


async def test_bad_request_is_not_retryable() -> None:
    """400 通常意味着模型名写错或参数不被支持 —— 重试没有意义。"""
    recorder = _Recorder(status=400, json_body={"error": {"message": "model not found"}})
    provider = _provider(recorder)

    with pytest.raises(LLMBadRequestError) as excinfo:
        await provider.complete(_request())

    assert getattr(excinfo.value, "retryable", False) is False
    assert "model not found" in excinfo.value.details["upstream"]
    await provider.aclose()


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_are_retryable(status: int) -> None:
    recorder = _Recorder(status=status, json_body={"error": {"message": "server exploded"}})
    provider = _provider(recorder)

    with pytest.raises(LLMTransportError) as excinfo:
        await provider.complete(_request())

    assert getattr(excinfo.value, "retryable", False) is True
    await provider.aclose()


async def test_timeout_maps_to_timeout_error_and_is_not_retryable() -> None:
    """超时**不可重试** —— 它和网络抖动不是一回事。

    抖动是随机的；超时说明 provider 在推理或排队，再压一份同样的负载
    大概率再超一次。实测代价：``max_retries=2`` = 共试 3 次，
    一个注定失败的请求白等 540s 才报错。改判之后最坏等待降到 180s。
    """
    recorder = _Recorder(raise_exc=httpx.ReadTimeout("too slow"))
    provider = _provider(recorder)

    with pytest.raises(LLMTimeoutError) as excinfo:
        await provider.complete(_request())

    assert excinfo.value.status_code == 504
    assert getattr(excinfo.value, "retryable", False) is False
    await provider.aclose()


async def test_connection_error_maps_to_transport_error() -> None:
    """连不上 / 连接被重置仍然是可重试的 —— 那是真的抖动。"""
    recorder = _Recorder(raise_exc=httpx.ConnectError("dns failed"))
    provider = _provider(recorder)

    with pytest.raises(LLMTransportError) as excinfo:
        await provider.complete(_request())

    assert getattr(excinfo.value, "retryable", False) is True
    await provider.aclose()


async def test_in_stream_error_event_is_reported() -> None:
    """流中途的 error 事件要如实报错，并把上游原文带上（不能让人猜）。"""
    body = 'data: {"error": {"code": "InvalidParameter", "message": "bad thing"}}\n\n'
    recorder = _Recorder(raw=body.encode("utf-8"))
    provider = _provider(recorder)

    with pytest.raises(LLMTransportError) as excinfo:
        await provider.complete(_request())

    assert "InvalidParameter" in excinfo.value.details["upstream"]
    await provider.aclose()


# ------------------------------------------------------------ 密钥不外泄


async def test_api_key_never_leaks_into_errors() -> None:
    """密钥绝不能出现在异常消息或 details 里 —— 它们会被写进日志、
    也会经统一错误处理器回到客户端。"""
    for status in (400, 401, 403, 429, 500):
        recorder = _Recorder(status=status, json_body={"error": {"message": f"boom {status}"}})
        provider = _provider(recorder)

        with pytest.raises(Exception) as excinfo:
            await provider.complete(_request())

        exc = excinfo.value
        blob = f"{exc} {getattr(exc, 'message', '')} {getattr(exc, 'details', {})}"
        assert API_KEY not in blob, f"密钥泄漏进了 HTTP {status} 的错误信息"

        await provider.aclose()


# ------------------------------------------------------------ 配置来源


async def test_from_settings_wires_all_three_fields_to_the_right_places() -> None:
    """配置的三个字段必须各就各位。

    官方文档明确警告「Coding Plan 专属 Key 与平台 Key 不是同一个」，
    把 base_url 和 model 接错，表现就是一个 401/400 —— 极难从现象反推原因。
    所以这里走一遍真实请求，断言 URL、认证头、请求体各自拿到了正确的配置值。
    """
    recorder = _Recorder()
    settings = Settings(
        app_env="test",
        llm_provider="ark",
        llm_api_key=API_KEY,
        llm_base_url=BASE_URL,
        llm_model=MODEL,
        llm_timeout_seconds=12.5,
    )
    provider = ArkLLMProvider.from_settings(settings, transport=httpx.MockTransport(recorder.handler))

    await provider.complete(_request())

    assert str(recorder.request.url) == f"{BASE_URL}/chat/completions"
    assert recorder.auth_header == f"Bearer {API_KEY}"
    assert recorder.payload["model"] == MODEL
    await provider.aclose()
