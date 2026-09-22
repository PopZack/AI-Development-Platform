"""LLM Provider 抽象层。

对外只暴露三样东西：抽象接口、请求/响应类型、以及按配置装配的工厂。
具体供应商实现（ark / mock）可以 import，但**业务代码不应该引用它们** ——
Agent 层只 ``from app.infrastructure.llm.base import LLMProvider``。
"""

from app.infrastructure.llm.base import (
    LLMMessage,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    LLMUsage,
)
from app.infrastructure.llm.errors import (
    LLMBadRequestError,
    LLMCredentialError,
    LLMError,
    LLMRateLimitError,
    LLMTimeoutError,
    LLMTransportError,
)
from app.infrastructure.llm.factory import SUPPORTED_PROVIDERS, create_llm_provider
from app.infrastructure.llm.mock import MockLLMProvider
from app.infrastructure.llm.retry import RetryingLLMProvider

__all__ = [
    "LLMMessage",
    "LLMProvider",
    "LLMRequest",
    "LLMResponse",
    "LLMUsage",
    "LLMError",
    "LLMTransportError",
    "LLMTimeoutError",
    "LLMRateLimitError",
    "LLMCredentialError",
    "LLMBadRequestError",
    "MockLLMProvider",
    "RetryingLLMProvider",
    "create_llm_provider",
    "SUPPORTED_PROVIDERS",
]
