"""LLM Provider 抽象。

Layer: Infrastructure（抽象定义）。

设计文档 §14.1 / §11 Stage 3 的硬性约束：**Agent 层只依赖这里的抽象，
任何供应商 SDK 都不得渗透进去**。换供应商时只新增一个实现类 + 改配置，
Agent、Prompt、结构化输出校验一行都不用动。

刻意不引第三方 SDK（openai / volcengine 等），只用 httpx：
- 少一层依赖，少一处版本漂移
- 各家 SDK 的异常类型不一样，抽象层反而更难做得干净
- Ark 的 /api/v3/chat/completions 本身就是 OpenAI 兼容协议，直接发 HTTP 最直接
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Literal

__all__ = [
    "MessageRole",
    "LLMMessage",
    "LLMUsage",
    "LLMRequest",
    "LLMResponse",
    "LLMProvider",
]

MessageRole = Literal["system", "user", "assistant"]


@dataclass(frozen=True)
class LLMMessage:
    role: MessageRole
    content: str

    @classmethod
    def system(cls, content: str) -> LLMMessage:
        return cls(role="system", content=content)

    @classmethod
    def user(cls, content: str) -> LLMMessage:
        return cls(role="user", content=content)

    @classmethod
    def assistant(cls, content: str) -> LLMMessage:
        return cls(role="assistant", content=content)


@dataclass(frozen=True)
class LLMUsage:
    """Token 用量。要落进 agent_runs，否则没法算成本和排查「为什么这么慢」。"""

    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0

    @classmethod
    def from_openai_like(cls, raw: dict[str, Any] | None) -> LLMUsage:
        """从 OpenAI 兼容协议的 usage 字段构造。缺字段一律当 0，不抛异常。"""
        raw = raw or {}
        prompt = int(raw.get("prompt_tokens") or 0)
        completion = int(raw.get("completion_tokens") or 0)
        return cls(
            prompt_tokens=prompt,
            completion_tokens=completion,
            total_tokens=int(raw.get("total_tokens") or (prompt + completion)),
        )


@dataclass(frozen=True)
class LLMRequest:
    messages: Sequence[LLMMessage]
    temperature: float = 0.2
    max_tokens: int | None = None

    # 要求模型「只输出 JSON 对象」。
    #
    # 注意：这只是**请求**，不是保证。支持与否由具体模型决定；不支持的实现
    # 应当如实报错（上游会回 400，我们映射成 LLMBadRequestError），
    # 而不是静默忽略 —— 静默忽略会让上层以为拿到了 JSON 保证。
    # 真正的保证只能来自「拿到文本后用 Pydantic 校验」，那是 Agent Runtime 的事。
    json_mode: bool = False


@dataclass(frozen=True)
class LLMResponse:
    content: str
    model: str
    usage: LLMUsage = field(default_factory=LLMUsage)
    # 原始响应体。排查「模型到底回了什么」时必须能看到全文，
    # 而不是只看我们解析出来的那个字段
    raw: dict[str, Any] = field(default_factory=dict)


class LLMProvider(ABC):
    """所有供应商实现的共同接口。"""

    name: str = "unknown"

    @abstractmethod
    async def complete(self, request: LLMRequest) -> LLMResponse:
        """单轮补全。失败一律抛 ``app.infrastructure.llm.errors`` 里的类型。"""

    async def aclose(self) -> None:
        """释放底层连接。默认无操作，持有连接的实现需要覆盖。"""
        return None

    @property
    def closed(self) -> bool:
        """底层连接是否已释放。

        放在抽象上是为了让「资源有没有被回收」可以被外部统一检查 ——
        否则每种实现各写一个自己的私有属性，启动自检和测试都没法写成一样的断言。
        """
        return False
