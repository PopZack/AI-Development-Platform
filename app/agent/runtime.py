"""Agent 统一运行时。

Layer: Agent（横向层，编排 Provider + 校验 + 落库）。

**这个文件是设计文档 §14.3 那条硬线的落点**：

    AI 负责理解、生成和分析；后端负责权限、状态、数据和工具边界。
    模型输出必须先通过 Pydantic 校验，不允许直接把模型输出写入数据库。

所以下面这段流程里，`parsed_json` 只在**校验通过后**才被赋值；
未通过校验的输出只留在 `output_json`（原文）+ `error_message`（为什么不合格）里。
这一点不能有任何「先写进去再校验」的变体。

### 内容层重试为什么要带上失败原因

重试时不是把同一个 Prompt 再发一遍 —— 那样只是赌模型这次心情好。第二次会把
**上一次的原始输出**和**校验报错**一起发回去，让模型知道要改什么。
这是内容层重试唯一有意义的形态，也是它必须和传输层重试分开的原因。

### 传输层失败不在这里重试

Provider 抛出的网络/超时/限流错误已经由 `RetryingLLMProvider` 处理过一轮。
到这里还抛出来，说明重试也没救，直接记录为 `FAILED` 并向上抛 ——
不再套一层重试，否则退避时间会成倍叠加。
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from json import dumps, loads
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import AgentOutputError, AppError
from app.domain.enums import AgentRole, AgentRunStatus
from app.infrastructure.llm.base import LLMMessage, LLMProvider, LLMRequest, LLMUsage
from app.models.agent import AgentRun
from app.repositories.agent_repository import AgentRunRepository

__all__ = ["AgentContext", "AgentOutcome", "AgentSpec", "AgentRuntime"]

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class AgentSpec:
    """一次 Agent 调用的静态定义：我是谁、我的指令是什么、输出长什么样。"""

    role: AgentRole
    system_prompt: str
    #: 输出必须能通过这个模型的校验。这是硬约束，不是建议
    output_model: type[BaseModel]
    temperature: float = 0.2
    max_tokens: int | None = None


@dataclass(frozen=True)
class AgentContext:
    """这次执行挂在哪个业务对象上。"""

    requirement_id: UUID
    workflow_run_id: UUID | None = None


@dataclass
class AgentOutcome:
    """成功的结果。

    ``agent_run_id`` 是**成功那一次**的 agent_runs 主键 —— artifact 要挂在它上面，
    这样「这份 PRD 是哪次调用产出的」可以一路查到原始输出与 token 用量。
    """

    output: BaseModel
    raw_content: str
    attempts: int
    execution_id: UUID
    agent_run_id: UUID | None = None
    model: str | None = None
    usage: LLMUsage = field(default_factory=LLMUsage)
    latency_ms: int = 0

    @property
    def parsed(self) -> dict[str, Any]:
        return self.output.model_dump(mode="json")


class AgentRuntime:
    def __init__(
        self,
        session: AsyncSession,
        provider: LLMProvider,
        *,
        max_output_attempts: int = 2,
        slow_call_seconds: float = 60.0,
    ) -> None:
        self._session = session
        self._provider = provider
        self._max_attempts = max(1, max_output_attempts)
        # 慢调用告警阈值（秒），0 = 关闭。provider 延迟天然波动，这是**告警**
        # 而不是失败判定：让运维在撞上 llm_timeout_seconds 之前看到变慢趋势
        self._slow_call_seconds = max(0.0, slow_call_seconds)
        self._runs = AgentRunRepository(session)

    async def run(self, spec: AgentSpec, *, user_prompt: str, context: AgentContext) -> AgentOutcome:
        execution_id = uuid4()
        messages: list[LLMMessage] = [
            LLMMessage.system(spec.system_prompt),
            LLMMessage.user(user_prompt),
        ]
        last_error: str = ""
        last_output: str = ""

        for attempt in range(1, self._max_attempts + 1):
            started = time.perf_counter()
            try:
                response = await self._provider.complete(
                    LLMRequest(
                        messages=list(messages),
                        temperature=spec.temperature,
                        max_tokens=spec.max_tokens,
                        json_mode=True,
                    )
                )
            except AppError as exc:
                # 传输层已经在 provider 内部重试过，这里再重试只会叠加退避
                await self._record(
                    execution_id=execution_id,
                    attempt=attempt,
                    spec=spec,
                    context=context,
                    status=AgentRunStatus.FAILED,
                    latency_ms=_ms_since(started),
                    error_code=exc.code,
                    error_message=exc.message,
                    user_prompt=user_prompt,
                )
                raise

            latency_ms = _ms_since(started)
            last_output = response.content
            parsed, failure = self._parse_and_validate(spec, response.content)

            if parsed is not None:
                run = await self._record(
                    execution_id=execution_id,
                    attempt=attempt,
                    spec=spec,
                    context=context,
                    status=AgentRunStatus.SUCCEEDED,
                    latency_ms=latency_ms,
                    user_prompt=user_prompt,
                    output=response.content,
                    parsed=parsed.model_dump(mode="json"),
                    model=response.model,
                    usage=response.usage,
                )
                logger.info(
                    "agent run succeeded | role=%s exec=%s attempt=%s tokens=%s latency=%sms",
                    spec.role,
                    execution_id,
                    attempt,
                    response.usage.total_tokens,
                    latency_ms,
                )
                # T10 慢调用告警：provider 变慢（模型侧波动/排队）的趋势信号 ——
                # 在撞上 llm_timeout_seconds 之前给人留出反应时间。
                # 是告警不是断言：阈值写成失败判定会把正常波动变成故障
                latency_s = latency_ms / 1000
                if self._slow_call_seconds and latency_s >= self._slow_call_seconds:
                    logger.warning(
                        "slow llm call | role=%s exec=%s latency=%.1fs "
                        "threshold=%.0fs tokens=%s —— provider 变慢的趋势信号，关注延迟与超时风险",
                        spec.role,
                        execution_id,
                        latency_s,
                        self._slow_call_seconds,
                        response.usage.total_tokens,
                    )
                return AgentOutcome(
                    output=parsed,
                    raw_content=response.content,
                    attempts=attempt,
                    execution_id=execution_id,
                    agent_run_id=run.id,
                    model=response.model,
                    usage=response.usage,
                    latency_ms=latency_ms,
                )

            last_error = failure or "unknown validation failure"
            await self._record(
                execution_id=execution_id,
                attempt=attempt,
                spec=spec,
                context=context,
                status=AgentRunStatus.INVALID_OUTPUT,
                latency_ms=latency_ms,
                user_prompt=user_prompt,
                output=response.content,
                # 校验没过 → parsed 必须是 NULL。这就是 §14.3 那条线
                parsed=None,
                model=response.model,
                usage=response.usage,
                error_code="AGENT_OUTPUT_INVALID",
                error_message=last_error,
            )
            logger.warning(
                "agent output invalid | role=%s exec=%s attempt=%s/%s reason=%s",
                spec.role,
                execution_id,
                attempt,
                self._max_attempts,
                last_error,
            )

            if attempt < self._max_attempts:
                # 把坏输出和原因一起带回去 —— 否则重试只是把同一个 Prompt 再发一遍，
                # 等于赌模型这次心情好
                messages.append(LLMMessage.assistant(response.content))
                messages.append(LLMMessage.user(_correction_prompt(spec, last_error)))

        raise AgentOutputError(
            f"{spec.role} failed to produce schema-valid output after {self._max_attempts} attempts",
            details={
                "agent_role": spec.role.value,
                "execution_id": str(execution_id),
                "attempts": self._max_attempts,
                "last_error": last_error,
                "last_output": last_output[:1000],
            },
        )

    # ------------------------------------------------------------------ 校验

    @staticmethod
    def _parse_and_validate(spec: AgentSpec, content: str) -> tuple[BaseModel | None, str | None]:
        """返回 ``(模型实例, 失败原因)``，两者恰好一个为 None。

        先把 JSON 解析与 Schema 校验的失败原因分开措辞 —— 报错信息会进
        ``agent_runs.error_message``，也是重试时反馈给模型的内容。
        「你回的不是 JSON」和「你回的 JSON 少了一个 required 字段」
        对模型来说是两种完全不同的修正动作。
        """
        if not content.strip():
            return None, "模型返回了空内容"

        try:
            payload = loads(content)
        except ValueError as exc:
            return None, f"输出不是合法 JSON：{exc}"

        if not isinstance(payload, dict):
            return None, f"输出必须是 JSON 对象，实际是 {type(payload).__name__}"

        try:
            return spec.output_model.model_validate(payload), None
        except ValidationError as exc:
            return None, f"输出不符合 {spec.output_model.__name__}：{_compact_errors(exc)}"

    # ------------------------------------------------------------------ 落库

    async def _record(
        self,
        *,
        execution_id: UUID,
        attempt: int,
        spec: AgentSpec,
        context: AgentContext,
        status: AgentRunStatus,
        latency_ms: int,
        user_prompt: str,
        output: str | None = None,
        parsed: dict[str, Any] | None = None,
        model: str | None = None,
        usage: LLMUsage | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
    ) -> AgentRun:
        usage = usage or LLMUsage()
        run = AgentRun(
            execution_id=execution_id,
            attempt=attempt,
            requirement_id=context.requirement_id,
            workflow_run_id=context.workflow_run_id,
            agent_role=spec.role.value,
            status=status.value,
            model=model,
            prompt_tokens=usage.prompt_tokens,
            completion_tokens=usage.completion_tokens,
            total_tokens=usage.total_tokens,
            latency_ms=latency_ms,
            system_prompt=spec.system_prompt,
            user_prompt=user_prompt,
            output_json=output,
            parsed_json=parsed,
            error_code=error_code,
            error_message=error_message,
        )
        await self._runs.add(run)
        await self._session.commit()
        return run


def _correction_prompt(spec: AgentSpec, failure: str) -> str:
    return (
        f"你上一次的输出没有通过校验：{failure}\n\n"
        f"请修正后重新输出。要求：只输出一个符合 {spec.output_model.__name__} 结构的 JSON 对象，"
        "不要任何解释、不要 Markdown 代码块围栏。"
    )


def _compact_errors(exc: ValidationError) -> str:
    """把 Pydantic 的错误列表压成一行。

    这段文本会进数据库、进日志、还要发给模型，所以不能是几百行的结构体转储。
    """
    return dumps(exc.errors(include_url=False, include_input=False), ensure_ascii=False)


def _ms_since(started: float) -> int:
    return int((time.perf_counter() - started) * 1000)
