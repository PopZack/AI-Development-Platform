"""按配置装配 LLM Provider。

Layer: Infrastructure（装配）。

**这是「换供应商只改配置」这句话的落点**：全项目只有这里知道有哪些供应商实现，
Agent / Prompt / 校验层都只认 ``LLMProvider`` 抽象。
"""

from __future__ import annotations

import logging

from app.common.exceptions import ValidationError
from app.config.settings import Settings
from app.infrastructure.llm.ark import ArkLLMProvider
from app.infrastructure.llm.base import LLMProvider
from app.infrastructure.llm.mock import MockLLMProvider
from app.infrastructure.llm.retry import RetryingLLMProvider

__all__ = ["create_llm_provider"]

logger = logging.getLogger(__name__)

SUPPORTED_PROVIDERS = ("ark", "mock")


def create_llm_provider(settings: Settings) -> LLMProvider:
    provider = settings.llm_provider.strip().lower()

    if provider == "mock":
        # 刻意**不**包重试：MockLLMProvider 是测试替身，而 RetryingLLMProvider 是
        # 被测机制之一。用被测机制把测试替身包起来，会让「脚本里放一个瞬时异常」
        # 这种最直观的写法产生难以预料的重试与脚本消耗。
        # 需要验证重试时，测试自己显式组合 RetryingLLMProvider(MockLLMProvider(...))。
        logger.warning("LLM_PROVIDER=mock —— 返回的是固定内容，不是模型生成的结果")
        return MockLLMProvider()

    if provider == "ark":
        _warn_if_incomplete(settings)
        inner = ArkLLMProvider.from_settings(settings)
        return RetryingLLMProvider(inner, max_retries=settings.llm_max_retries)

    raise ValidationError(
        f"Unsupported LLM_PROVIDER: {settings.llm_provider!r}",
        code="LLM_PROVIDER_UNSUPPORTED",
        details={"supported": list(SUPPORTED_PROVIDERS)},
    )


def _warn_if_incomplete(settings: Settings) -> None:
    """配置不完整只警告，不在启动时直接失败。

    理由：本地还没申请到密钥时，Stage 1/2 的接口应该照常可用 —— 它们根本不需要 LLM。
    真到调用时再由 ArkLLMProvider 抛出明确的 LLMCredentialError。
    生产环境由 Settings 的配置护栏强制要求密钥（见 config/settings.py）。
    """
    missing = [
        name
        for name, value in (("LLM_API_KEY", settings.llm_api_key), ("LLM_MODEL", settings.llm_model))
        if not value.strip()
    ]
    if missing:
        logger.warning(
            "LLM_PROVIDER=ark 但 %s 为空 —— 一旦有接口真正调用模型就会失败",
            " / ".join(missing),
        )
