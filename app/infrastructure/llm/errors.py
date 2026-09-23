"""LLM Provider 的失败分类。

Layer: Infrastructure。

**为什么要细分，而不是一律抛 ProviderError**：因为「可不可以重试」完全不同。

- 网络抖动 / 连接被重置 / 5xx / 429 → 重试有意义（抖动是随机的）
- 401 / 403（密钥错或没权限）、400（请求参数不对）→ **重试一万次也一样失败**，
  只会白白烧时间和配额，还会把真正的配置问题埋在重试日志里
- **超时 → 不重试**，理由见 ``LLMTimeoutError`` 的说明（它不是抖动）
- 输出不合 Schema → 属于内容问题，由 Agent Runtime 决定要不要重新问一次，
  不是传输层的事

``retryable`` 只是个标记，重试逻辑在 ``retry.py``；把它做成属性而不是
``isinstance`` 判断，是为了让新增错误类型时不用回头改重试代码。
"""

from __future__ import annotations

from app.common.exceptions import ProviderError, UpstreamTimeoutError

__all__ = [
    "LLMError",
    "LLMTransportError",
    "LLMTimeoutError",
    "LLMRateLimitError",
    "LLMCredentialError",
    "LLMBadRequestError",
]

# 语义别名：调用方 import LLMError 更自然，实际类型仍是 common 里那套统一异常
LLMError = ProviderError


class LLMTransportError(ProviderError):
    """网络失败 / 上游 5xx。可重试。"""

    code = "PROVIDER_UNAVAILABLE"
    default_message = "LLM provider is unreachable"
    retryable = True


class LLMTimeoutError(UpstreamTimeoutError):
    """调用超时。**不可重试** —— 它和「网络抖动」是两件事。

    抖动是随机的，重试有意义；超时说明 provider 在忙着（推理中或排队），
    立刻重试等于**再压一份同样的负载**，而且大概率再超一次。

    实测代价（2026-09-23，deepseek-v4-flash，非流式）：一次 Developer 调用有
    300s+ 的服务端推理期，客户端在整段推理期里收不到任何字节。
    ``LLM_MAX_RETRIES=2`` 意味着**共试 3 次** —— 一个注定失败的请求白等 540s
    才报错，还额外压了 provider 三次。

    改判成不可重试之后：最坏等待 540s → 180s，快速失败，由调用方决定
    要不要挑更好的时机重来。**真正治本的是流式**（见 ``ark.py`` 里
    「为什么必须 stream」）—— 流式把读超时的语义从「等整包」换成
    「等下一个数据块」（实测块间最大 0.5s），这类超时本身就基本消失了。
    剩下的超时才是「provider 真的卡住了」，此时快速失败比重试更符合直觉。
    """

    retryable = False


class LLMRateLimitError(ProviderError):
    """被限流（429）。可重试，但要退避。"""

    code = "PROVIDER_RATE_LIMITED"
    default_message = "LLM provider rate limit exceeded"
    retryable = True


class LLMCredentialError(ProviderError):
    """密钥无效 / 无权访问。**不可重试** —— 这是配置问题，不是抖动。

    这类错误必须在日志里显眼地出现。如果被重试逻辑吞掉，表现就是
    「调用很慢然后失败」，而真正的原因（密钥错了）要翻很久才找得到。
    """

    code = "PROVIDER_CREDENTIAL_INVALID"
    default_message = "LLM provider rejected the credential"
    retryable = False


class LLMBadRequestError(ProviderError):
    """请求本身不合法（400），例如模型名不存在、参数不被支持。**不可重试**。"""

    code = "PROVIDER_REQUEST_INVALID"
    default_message = "LLM provider rejected the request"
    retryable = False
