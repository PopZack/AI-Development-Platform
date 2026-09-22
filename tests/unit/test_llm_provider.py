"""Provider 抽象层测试：Mock 实现、重试包装、工厂装配、配置护栏。

把 Ark 网络实现单独放在 test_ark_provider.py。
"""

from __future__ import annotations

import pytest

from app.common.exceptions import ProviderError, ValidationError
from app.config.settings import DEFAULT_JWT_SECRET, Settings
from app.infrastructure.llm import (
    LLMCredentialError,
    LLMMessage,
    LLMRequest,
    LLMTimeoutError,
    LLMTransportError,
    MockLLMProvider,
    RetryingLLMProvider,
    create_llm_provider,
)
from app.infrastructure.llm.ark import ArkLLMProvider
from app.infrastructure.llm.base import LLMUsage


def _request(text: str = "你好") -> LLMRequest:
    return LLMRequest(messages=[LLMMessage.user(text)])


def _settings(**overrides: object) -> Settings:
    base: dict[str, object] = {
        "app_env": "test",
        "llm_provider": "mock",
        "llm_api_key": "test-key",
        "llm_model": "test-model",
    }
    base.update(overrides)
    return Settings(**base)


# ------------------------------------------------------------------ 基础类型


def test_usage_tolerates_missing_fields() -> None:
    """上游 usage 字段可能缺失或改名。缺就直接当 0，不能因此把整次调用判失败。"""
    assert LLMUsage.from_openai_like(None) == LLMUsage()
    assert LLMUsage.from_openai_like({}) == LLMUsage()
    assert LLMUsage.from_openai_like({"prompt_tokens": 3, "completion_tokens": 4}) == LLMUsage(
        prompt_tokens=3, completion_tokens=4, total_tokens=7
    )


def test_message_helpers_set_roles() -> None:
    assert LLMMessage.system("s").role == "system"
    assert LLMMessage.user("u").role == "user"
    assert LLMMessage.assistant("a").role == "assistant"


# --------------------------------------------------------------- Mock Provider


async def test_mock_returns_marked_default_without_script() -> None:
    """默认输出必须自带醒目标记，避免本地手跑时被误认成模型生成的内容。"""
    provider = MockLLMProvider()

    response = await provider.complete(_request())

    assert "_mock" in response.content
    assert "不是模型生成的内容" in response.content


async def test_mock_returns_scripted_responses_in_order() -> None:
    provider = MockLLMProvider(script=['{"a": 1}', '{"b": 2}'])

    first = await provider.complete(_request())
    second = await provider.complete(_request())

    assert first.content == '{"a": 1}'
    assert second.content == '{"b": 2}'
    assert provider.remaining == 0


async def test_mock_raises_scripted_exception() -> None:
    """脚本里混入异常是刻意的能力 —— 重试与失败处理路径需要它。"""
    provider = MockLLMProvider(script=[LLMTimeoutError("boom"), '{"ok": true}'])

    with pytest.raises(LLMTimeoutError):
        await provider.complete(_request())

    assert (await provider.complete(_request())).content == '{"ok": true}'


async def test_mock_fails_loudly_when_script_is_exhausted() -> None:
    """脚本用完必须报错，不能静默回落到默认内容。

    静默回落会让「测试少写了一条响应」变成一个看起来通过、其实没测到东西的用例。
    """
    provider = MockLLMProvider(script=['{"only": "one"}'])
    await provider.complete(_request())

    with pytest.raises(ProviderError):
        await provider.complete(_request())


async def test_mock_records_calls_and_exposes_prompt() -> None:
    provider = MockLLMProvider()
    await provider.complete(_request("实现一个待办事项接口"))

    assert len(provider.calls) == 1
    assert "实现一个待办事项接口" in provider.last_prompt()


async def test_mock_rejects_calls_after_close() -> None:
    provider = MockLLMProvider()
    await provider.aclose()

    with pytest.raises(ProviderError):
        await provider.complete(_request())


# ------------------------------------------------------------ 重试包装


class _FlakyProvider(MockLLMProvider):
    """按脚本抛异常 / 返回内容，用来驱动重试逻辑。"""


async def test_retry_recovers_from_transient_failure() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    inner = _FlakyProvider(script=[LLMTransportError("抖动"), '{"ok": true}'])
    provider = RetryingLLMProvider(inner, max_retries=2, backoff_seconds=0.5, sleep=fake_sleep)

    response = await provider.complete(_request())

    assert response.content == '{"ok": true}'
    # 第一次失败后重试一次，退避 0.5s
    assert sleeps == [0.5]


async def test_retry_does_not_retry_credential_errors() -> None:
    """401/403 重试一万次也一样失败，只会把配置问题埋进重试日志。"""
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    inner = _FlakyProvider(script=[LLMCredentialError("密钥无效")])
    provider = RetryingLLMProvider(inner, max_retries=3, sleep=fake_sleep)

    with pytest.raises(LLMCredentialError):
        await provider.complete(_request())

    assert sleeps == []


async def test_retry_gives_up_after_max_retries_and_uses_exponential_backoff() -> None:
    sleeps: list[float] = []

    async def fake_sleep(seconds: float) -> None:
        sleeps.append(seconds)

    inner = _FlakyProvider(script=[LLMTransportError("a"), LLMTransportError("b"), LLMTransportError("c")])
    provider = RetryingLLMProvider(inner, max_retries=2, backoff_seconds=0.5, sleep=fake_sleep)

    with pytest.raises(LLMTransportError):
        await provider.complete(_request())

    # 总共 3 次调用（1 次原始 + 2 次重试），退避 0.5 → 1.0
    assert sleeps == [0.5, 1.0]
    assert inner.remaining == 0


async def test_retry_with_zero_max_retries_passes_through() -> None:
    inner = _FlakyProvider(script=[LLMTransportError("a")])
    provider = RetryingLLMProvider(inner, max_retries=0)

    with pytest.raises(LLMTransportError):
        await provider.complete(_request())


async def test_retry_wrapper_forwards_closed_state() -> None:
    """包装器对「连接关了吗」必须透明。

    回归测试：RetryingLLMProvider 曾经没有转发 ``closed``，继承了基类那个恒为
    False 的默认实现 —— 于是 lifespan 里明明已经 aclose()，外部检查结果却是 False。
    资源泄漏检查给出假警报比不做检查更糟：它让人去查一个不存在的问题。
    """
    inner = MockLLMProvider()
    provider = RetryingLLMProvider(inner, max_retries=1)

    assert provider.closed is False

    await provider.aclose()

    assert inner.closed is True
    assert provider.closed is True


def test_base_provider_reports_not_closed_by_default() -> None:
    """不持有连接的实现不用各自写一遍「未关闭」。"""
    assert MockLLMProvider().closed is False


# ------------------------------------------------------------ 工厂装配


def test_factory_builds_mock_without_retry_wrapper() -> None:
    """测试替身不该被「被测机制」包住 —— 否则脚本里放一个瞬时异常会变得难以预料。"""
    provider = create_llm_provider(_settings(llm_provider="mock"))

    assert isinstance(provider, MockLLMProvider)


def test_factory_builds_ark_with_retry_wrapper() -> None:
    provider = create_llm_provider(_settings(llm_provider="ark"))

    assert isinstance(provider, RetryingLLMProvider)


def test_factory_rejects_unknown_provider() -> None:
    with pytest.raises(ValidationError) as excinfo:
        create_llm_provider(_settings(llm_provider="openai"))

    assert excinfo.value.code == "LLM_PROVIDER_UNSUPPORTED"
    assert "ark" in excinfo.value.details["supported"]


def test_factory_tolerates_incomplete_ark_config_locally() -> None:
    """本地还没拿到密钥时，Stage 1/2 的接口应该照常可用 —— 它们不需要 LLM。

    真到调用时再由 ArkLLMProvider 抛出明确的凭据错误，而不是启动就崩。
    """
    provider = create_llm_provider(_settings(llm_provider="ark", llm_api_key="", llm_model=""))

    assert isinstance(provider, RetryingLLMProvider)


# ------------------------------------------------------------ 配置护栏


def test_production_rejects_mock_provider() -> None:
    """生产上用 Mock 等于整个平台在演假戏，而且从接口响应上完全看不出来。"""
    with pytest.raises(ValueError, match="mock"):
        Settings(
            app_env="prod",
            jwt_secret_key="a" * 40,
            llm_provider="mock",
            llm_api_key="k",
            llm_model="m",
        )


def test_production_requires_llm_credentials() -> None:
    with pytest.raises(ValueError, match="LLM_API_KEY"):
        Settings(
            app_env="prod",
            jwt_secret_key="a" * 40,
            llm_provider="ark",
            llm_api_key="",
            llm_model="deepseek",
        )


def test_local_environment_allows_empty_llm_config() -> None:
    """本地不该被这条护栏拦住。"""
    settings = Settings(app_env="local", jwt_secret_key=DEFAULT_JWT_SECRET, llm_api_key="")
    assert settings.llm_api_key == ""


async def test_ark_provider_is_constructible_from_settings() -> None:
    provider = ArkLLMProvider.from_settings(
        _settings(llm_provider="ark", llm_model="deepseek-v4-flash-260425")
    )
    assert provider.name == "ark"
    await provider.aclose()
