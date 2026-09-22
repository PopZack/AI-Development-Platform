"""Mock LLM Provider。

Layer: Infrastructure。

这不是「临时代码」，它要长期留着。原因：**有些分支用真实模型根本没法稳定复现**。
最典型的是文档 §14.3 那条硬性要求 —— 「模型输出必须先通过 Pydantic 校验，绝不落库」。
要验证「模型返回了非法 JSON 时系统会拒绝而不是把半成品写进数据库」，
你不可能靠反复真实调用来等模型输出坏 JSON。

用法：

    # 按脚本依次返回；脚本里可以混入异常，用来验证重试与失败处理
    provider = MockLLMProvider(script=[LLMTimeoutError("boom"), '{"title": "ok"}'])

    # 不传脚本 → 永远返回一段带明显标记的 JSON（供本地手跑，不会被误认成模型输出）
    provider = MockLLMProvider()
"""

from __future__ import annotations

import json

from app.common.exceptions import ProviderError
from app.infrastructure.llm.base import LLMProvider, LLMRequest, LLMResponse, LLMUsage

__all__ = ["MockLLMProvider"]

# 默认输出刻意带上醒目的标记：本地 LLM_PROVIDER=mock 时看到这行字就知道
# 内容不是模型生成的，避免把它当成真实结果
_DEFAULT_CONTENT = json.dumps(
    {
        "_mock": True,
        "note": "MockLLMProvider 的默认输出，不是模型生成的内容。测试请用 script= 指定响应。",
    },
    ensure_ascii=False,
)

ScriptItem = str | Exception


class MockLLMProvider(LLMProvider):
    name = "mock"

    def __init__(self, script: list[ScriptItem] | None = None, *, model: str = "mock-model") -> None:
        # 必须区分「调用方没给脚本」和「脚本已经用完」—— 两者都表现为脚本列表为空，
        # 但行为必须完全不同：
        #   没给脚本 → 永远回默认内容（本地手跑用）
        #   用完了   → 报错。否则测试少写一条响应就会变成「看起来通过、其实没测到东西」
        self._script_provided = script is not None
        # 复制一份，避免调用方后续改动列表影响已构造好的 provider
        self._script: list[ScriptItem] = list(script or [])
        self._model = model
        self.calls: list[LLMRequest] = []
        self._closed = False

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls.append(request)
        self._assert_not_closed()

        if not self._script:
            if self._script_provided:
                raise ProviderError(
                    "MockLLMProvider script is exhausted — the test scripted fewer responses "
                    "than the code under test actually requested",
                    code="MOCK_SCRIPT_EXHAUSTED",
                    details={"call_count": len(self.calls)},
                )
            return self._respond(_DEFAULT_CONTENT, request)

        item = self._script.pop(0)

        # 脚本里混入异常是刻意的能力：重试、降级、失败上报这些路径需要它
        if isinstance(item, Exception):
            raise item

        return self._respond(item, request)

    def _respond(self, content: str, request: LLMRequest) -> LLMResponse:
        # 粗算 token（4 字符 ≈ 1 token），只是为了让 agent_runs 里有东西可看，
        # 不要拿它做成本核算
        prompt_chars = sum(len(m.content) for m in request.messages)
        return LLMResponse(
            content=content,
            model=self._model,
            usage=LLMUsage(
                prompt_tokens=prompt_chars // 4,
                completion_tokens=len(content) // 4,
                total_tokens=(prompt_chars + len(content)) // 4,
            ),
            raw={"mock": True, "json_mode": request.json_mode},
        )

    def _assert_not_closed(self) -> None:
        if self._closed:
            raise ProviderError("Mock provider is closed", code="PROVIDER_UNAVAILABLE", status_code=502)

    async def aclose(self) -> None:
        self._closed = True

    @property
    def closed(self) -> bool:
        return self._closed

    # ------------------------------------------------------------ 测试辅助

    def enqueue(self, *items: ScriptItem) -> None:
        """往脚本尾部追加响应。

        用于「先让前几次失败、验证失败路径，再补一条成功响应验证可恢复」这类用例 ——
        构造时无法预知要追加几条，只能事后补。
        """
        self._script.extend(items)

    @property
    def remaining(self) -> int:
        """脚本还剩几条。测试里用它断言「重试确实多调了一次」。"""
        return len(self._script)

    def last_prompt(self) -> str:
        """最近一次调用的完整提示词，用来断言需求内容真的进了 Prompt。"""
        if not self.calls:
            raise AssertionError("MockLLMProvider.complete() has never been called")
        return "\n\n".join(f"[{m.role}]\n{m.content}" for m in self.calls[-1].messages)
