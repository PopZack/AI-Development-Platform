"""Ark（火山方舟）Provider 的网络层测试。

**全部用 ``httpx.MockTransport`` 打桩，不打真实网络。** 这样能在没有有效密钥的情况下
验证两件真正重要的事：

1. **我们到底发出了什么请求** —— URL、认证头、请求体字段。Provider 抽象层最容易出的
   问题就是「请求构造错了」，而这类问题用真实调用很难定位（只会看到一个 400）。
2. **上游各种失败被映射成哪一类错误** —— 尤其要保证「不可重试」的类型没被误判成可重试。

API Key 的**有效性与模型名归属**只能在真实调用里验证，那是单独的连通性检查，
不属于单元测试（它需要一个有效密钥，而且要花钱）。
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


class _Recorder:
    """记录 provider 实际发出的请求，供断言用。"""

    def __init__(self, *, status: int = 200, body: Any = None, raise_exc: Exception | None = None):
        self.request: httpx.Request | None = None
        self._status = status
        self._body = body if body is not None else _ok_body()
        self._raise = raise_exc

    def handler(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        if self._raise is not None:
            raise self._raise
        return httpx.Response(self._status, json=self._body, headers={"X-Request-Id": "req-ark-1"})

    @property
    def payload(self) -> dict[str, Any]:
        assert self.request is not None, "provider 没有发出请求"
        return json.loads(self.request.content)

    @property
    def auth_header(self) -> str:
        assert self.request is not None
        return self.request.headers.get("Authorization", "")


def _ok_body(content: str = '{"title": "ok"}') -> dict[str, Any]:
    return {
        "model": MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": content}}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 22, "total_tokens": 33},
    }


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
    recorder = _Recorder(body=_ok_body('{"ok": true}'))
    provider = _provider(recorder)

    await provider.complete(_request(json_mode=True))

    assert recorder.payload["response_format"] == {"type": "json_object"}
    await provider.aclose()


async def test_max_tokens_omitted_when_not_set() -> None:
    recorder = _Recorder()
    provider = _provider(recorder)

    await provider.complete(_request())

    assert "max_tokens" not in recorder.payload
    await provider.aclose()


# ------------------------------------------------------------ 响应解析


async def test_successful_response_is_parsed() -> None:
    recorder = _Recorder(body=_ok_body('{"title": "实现 Todo API"}'))
    provider = _provider(recorder)

    response = await provider.complete(_request())

    assert response.content == '{"title": "实现 Todo API"}'
    assert response.model == MODEL
    assert response.usage.prompt_tokens == 11
    assert response.usage.completion_tokens == 22
    assert response.usage.total_tokens == 33
    # raw 必须保留全文，「模型到底回了什么」排障时要看得到
    assert response.raw["choices"][0]["message"]["content"] == '{"title": "实现 Todo API"}'
    await provider.aclose()


async def test_malformed_response_shape_raises_transport_error() -> None:
    """少字段不能静默返回空内容 —— 那会伪装成「Agent 输出校验失败」，
    把排查方向指错。"""
    recorder = _Recorder(body={"unexpected": "shape"})
    provider = _provider(recorder)

    with pytest.raises(LLMTransportError):
        await provider.complete(_request())
    await provider.aclose()


# ------------------------------------------------------------ 失败映射


@pytest.mark.parametrize("status", [401, 403])
async def test_credential_errors_are_not_retryable(status: int) -> None:
    recorder = _Recorder(status=status, body={"error": {"message": "invalid key"}})
    provider = _provider(recorder)

    with pytest.raises(LLMCredentialError) as excinfo:
        await provider.complete(_request())

    assert excinfo.value.status_code == 502
    assert getattr(excinfo.value, "retryable", False) is False
    await provider.aclose()


async def test_rate_limit_is_retryable() -> None:
    recorder = _Recorder(status=429, body={"error": {"message": "too many requests"}})
    provider = _provider(recorder)

    with pytest.raises(LLMRateLimitError) as excinfo:
        await provider.complete(_request())

    assert getattr(excinfo.value, "retryable", False) is True
    await provider.aclose()


async def test_bad_request_is_not_retryable() -> None:
    """400 通常意味着模型名写错或参数不被支持 —— 重试没有意义。"""
    recorder = _Recorder(status=400, body={"error": {"message": "model not found"}})
    provider = _provider(recorder)

    with pytest.raises(LLMBadRequestError) as excinfo:
        await provider.complete(_request())

    assert getattr(excinfo.value, "retryable", False) is False
    assert "model not found" in excinfo.value.details["upstream"]
    await provider.aclose()


@pytest.mark.parametrize("status", [500, 502, 503])
async def test_server_errors_are_retryable(status: int) -> None:
    recorder = _Recorder(status=status, body={"error": {"message": "server exploded"}})
    provider = _provider(recorder)

    with pytest.raises(LLMTransportError) as excinfo:
        await provider.complete(_request())

    assert getattr(excinfo.value, "retryable", False) is True
    await provider.aclose()


async def test_timeout_maps_to_timeout_error() -> None:
    recorder = _Recorder(raise_exc=httpx.ReadTimeout("too slow"))
    provider = _provider(recorder)

    with pytest.raises(LLMTimeoutError) as excinfo:
        await provider.complete(_request())

    assert excinfo.value.status_code == 504
    assert getattr(excinfo.value, "retryable", False) is True
    await provider.aclose()


async def test_connection_error_maps_to_transport_error() -> None:
    recorder = _Recorder(raise_exc=httpx.ConnectError("dns failed"))
    provider = _provider(recorder)

    with pytest.raises(LLMTransportError) as excinfo:
        await provider.complete(_request())

    assert getattr(excinfo.value, "retryable", False) is True
    await provider.aclose()


# ------------------------------------------------------------ 密钥不外泄


async def test_api_key_never_leaks_into_errors() -> None:
    """密钥绝不能出现在异常消息或 details 里 —— 它们会被写进日志、
    也会经统一错误处理器回到客户端。"""
    for status in (400, 401, 403, 429, 500):
        recorder = _Recorder(status=status, body={"error": {"message": f"boom {status}"}})
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
