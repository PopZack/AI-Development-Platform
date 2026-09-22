"""Agent Runtime 测试：Prompt 组装 → 调 Provider → Pydantic 校验 → 内容层重试 → 落库。

**全部用 MockLLMProvider，不打真实网络。** 这正是 Mock 存在的理由：
「模型返回了非法 JSON」这个分支用真实模型无法稳定复现，
而它恰恰是文档 §14.3 那条硬线要守住的场景。

放在 integration/ 而不是 unit/：这里用真实 SQLite 验证落库结果，
属于「数据真的写进去了吗」这一类断言。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID, uuid4

import pytest
from pydantic import BaseModel, Field
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.runtime import AgentContext, AgentRuntime, AgentSpec
from app.common.exceptions import AgentOutputError, ProviderError
from app.domain.enums import AgentRole, AgentRunStatus
from app.infrastructure.llm import LLMCredentialError, LLMTimeoutError, MockLLMProvider
from app.models.agent import AgentRun, Artifact
from app.models.project import Project, ProjectMember
from app.models.requirement import Requirement
from app.models.user import User


class _Plan(BaseModel):
    """测试用的输出 Schema：包含必填、默认值和列表，覆盖面广一点。"""

    title: str = Field(min_length=1)
    priority: str = "P1"
    acceptance_criteria: list[str] = Field(default_factory=list)


@pytest.fixture
async def requirement_id(db_session: AsyncSession) -> UUID:
    """造一条**真实**的需求链路（user → project → member → requirement）。

    不能用随机 UUID 糊过去：``agent_runs.requirement_id`` 是 NOT NULL 外键，
    而外键现在是真正生效的（见 session.py 的 PRAGMA foreign_keys）。
    用假 id 会让测试依赖「SQLite 默认不强制外键」这个巧合 ——
    哪天有人打开它，测试就会莫名其妙地全红。
    """
    suffix = uuid4().hex[:8]
    user = User(
        email=f"agent-{suffix}@example.com",
        password_hash="x",
        display_name="Agent Test User",
        status="ACTIVE",
    )
    db_session.add(user)
    await db_session.flush()

    project = Project(
        name="Agent Test Project", slug=f"agent-test-{suffix}", owner_id=user.id, status="ACTIVE"
    )
    db_session.add(project)
    await db_session.flush()

    db_session.add(ProjectMember(project_id=project.id, user_id=user.id, role="OWNER"))

    requirement = Requirement(
        project_id=project.id,
        title="做一个待办接口",
        description="实现 Todo 的增删改查",
        status="DRAFT",
        priority="P0",
        created_by=user.id,
        version=1,
    )
    db_session.add(requirement)
    await db_session.commit()
    return requirement.id


def _spec() -> AgentSpec:
    return AgentSpec(
        role=AgentRole.PRODUCT,
        system_prompt="你是需求分析师，只输出 JSON。",
        output_model=_Plan,
    )


def _runtime(
    session: AsyncSession, script: list[Any] | None = None, **kwargs: Any
) -> tuple[AgentRuntime, MockLLMProvider]:
    provider = MockLLMProvider(script=script)
    return AgentRuntime(session, provider, **kwargs), provider


# ------------------------------------------------------------------ 成功路径


async def test_successful_run_validates_and_persists(db_session: AsyncSession, requirement_id: UUID) -> None:
    runtime, _ = _runtime(db_session, ['{"title": "实现 Todo API", "acceptance_criteria": ["可创建"]}'])

    outcome = await runtime.run(
        _spec(), user_prompt="做一个待办接口", context=AgentContext(requirement_id=requirement_id)
    )

    assert isinstance(outcome.output, _Plan)
    assert outcome.output.title == "实现 Todo API"
    assert outcome.attempts == 1

    run = (await db_session.execute(select(AgentRun))).scalar_one()
    assert run.status == AgentRunStatus.SUCCEEDED.value
    assert run.agent_role == AgentRole.PRODUCT.value
    assert run.attempt == 1
    assert run.execution_id == outcome.execution_id
    assert run.requirement_id == requirement_id
    assert run.parsed_json == {
        "title": "实现 Todo API",
        "priority": "P1",
        "acceptance_criteria": ["可创建"],
    }
    assert run.output_json is not None
    assert run.error_code is None


async def test_prompt_contains_system_and_user_messages(
    db_session: AsyncSession, requirement_id: UUID
) -> None:
    runtime, provider = _runtime(db_session, ['{"title": "x"}'])

    await runtime.run(
        _spec(),
        user_prompt="需求原文：做一个待办接口",
        context=AgentContext(requirement_id=requirement_id),
    )

    prompt = provider.last_prompt()
    assert "[system]" in prompt
    assert "你是需求分析师" in prompt
    assert "需求原文：做一个待办接口" in prompt


async def test_json_mode_is_requested_from_provider(db_session: AsyncSession, requirement_id: UUID) -> None:
    """必须显式要求 JSON 模式 —— 这是结构化输出的第一道保障。"""
    runtime, provider = _runtime(db_session, ['{"title": "x"}'])

    await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    assert provider.calls[-1].json_mode is True


async def test_usage_and_latency_are_recorded(db_session: AsyncSession, requirement_id: UUID) -> None:
    runtime, _ = _runtime(db_session, ['{"title": "x"}'])

    await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    run = (await db_session.execute(select(AgentRun))).scalar_one()
    assert run.total_tokens > 0
    assert run.latency_ms >= 0
    assert run.model == "mock-model"
    # Prompt 也要留底：排查「模型为什么回错」时得能看到当时问了什么
    assert run.system_prompt and run.user_prompt


# ------------------------------------------- 硬线：非法输出绝不落进 parsed_json


@pytest.mark.parametrize(
    ("bad_output", "expected_reason_fragment"),
    [
        ("这不是 JSON，是模型在聊天", "不是合法 JSON"),
        ("", "空内容"),
        ('["数组不是对象"]', "必须是 JSON 对象"),
        ('{"priority": "P0"}', "不符合 _Plan"),  # 缺必填 title
        ('{"title": ""}', "不符合 _Plan"),  # title 违反 min_length
    ],
)
async def test_invalid_output_never_reaches_parsed_json(
    db_session: AsyncSession,
    requirement_id: UUID,
    bad_output: str,
    expected_reason_fragment: str,
) -> None:
    """**文档 §14.3 的硬线**：模型输出未通过校验时，绝不能落进 ``parsed_json``。

    这一组用例只有用 Mock 才写得出来 —— 靠真实模型等它输出坏 JSON 是不现实的。
    """
    runtime, _ = _runtime(db_session, [bad_output, bad_output], max_output_attempts=2)

    with pytest.raises(AgentOutputError) as excinfo:
        await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    assert excinfo.value.code == "AGENT_OUTPUT_INVALID"

    runs = (await db_session.execute(select(AgentRun).order_by(AgentRun.attempt))).scalars().all()
    assert len(runs) == 2
    for run in runs:
        assert run.status == AgentRunStatus.INVALID_OUTPUT.value
        assert run.parsed_json is None, "校验没过的输出泄漏进了 parsed_json"
        assert run.output_json == bad_output, "原始输出必须留底，否则没法排障"
        assert expected_reason_fragment in (run.error_message or "")


async def test_all_attempts_recorded_with_shared_execution_id(
    db_session: AsyncSession, requirement_id: UUID
) -> None:
    """按次记录的意义：能看出「第一次坏了、第二次改好了」，而不是只看到最终结果。"""
    runtime, _ = _runtime(db_session, ["不是 JSON", '{"title": "第二次改好了"}'], max_output_attempts=2)

    outcome = await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    assert outcome.attempts == 2
    runs = (await db_session.execute(select(AgentRun).order_by(AgentRun.attempt))).scalars().all()
    assert [r.attempt for r in runs] == [1, 2]
    assert {r.execution_id for r in runs} == {outcome.execution_id}
    assert runs[0].status == AgentRunStatus.INVALID_OUTPUT.value
    assert runs[0].parsed_json is None
    assert runs[1].status == AgentRunStatus.SUCCEEDED.value
    assert runs[1].parsed_json is not None


# ------------------------------------------------------------ 内容层重试


async def test_retry_feeds_the_failure_back_to_the_model(
    db_session: AsyncSession, requirement_id: UUID
) -> None:
    """重试不能只是把同一个 Prompt 再发一遍 —— 那样只是赌模型这次心情好。

    第二次请求必须同时带上「上一次的原始输出」和「为什么不合法」。
    """
    runtime, provider = _runtime(db_session, ['{"priority": "P0"}', '{"title": "ok"}'], max_output_attempts=2)

    await runtime.run(_spec(), user_prompt="原始需求", context=AgentContext(requirement_id=requirement_id))

    second = provider.calls[1]
    assert len(second.messages) == 4, "第二轮应是 system + user + assistant(坏输出) + user(纠正)"
    assert second.messages[2].role == "assistant"
    assert second.messages[2].content == '{"priority": "P0"}'
    assert second.messages[3].role == "user"
    assert "没有通过校验" in second.messages[3].content
    assert "_Plan" in second.messages[3].content
    assert second.messages[1].content == "原始需求", "原始需求不能被丢掉"


async def test_no_retry_when_first_attempt_succeeds(db_session: AsyncSession, requirement_id: UUID) -> None:
    runtime, provider = _runtime(db_session, ['{"title": "ok"}'], max_output_attempts=3)

    await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    assert len(provider.calls) == 1
    assert provider.remaining == 0


async def test_max_output_attempts_is_respected(db_session: AsyncSession, requirement_id: UUID) -> None:
    runtime, provider = _runtime(db_session, ["坏的", "还是坏的", "依然坏"], max_output_attempts=3)

    with pytest.raises(AgentOutputError):
        await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    assert len(provider.calls) == 3
    total = (await db_session.execute(select(func.count()).select_from(AgentRun))).scalar_one()
    assert total == 3


# ------------------------------------------ 传输层失败与内容层失败必须分开


async def test_transport_failure_is_recorded_and_reraised(
    db_session: AsyncSession, requirement_id: UUID
) -> None:
    """传输层失败记成 FAILED，与「模型不听话」的 INVALID_OUTPUT 区分开。

    混成一个状态就没法回答「最近失败是因为模型不听话还是基础设施不稳」，
    而这两种情况的处置方式完全不同。
    """
    runtime, _ = _runtime(db_session, [LLMTimeoutError("超时了")])

    with pytest.raises(LLMTimeoutError):
        await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    run = (await db_session.execute(select(AgentRun))).scalar_one()
    assert run.status == AgentRunStatus.FAILED.value
    assert run.error_code == "TIMEOUT"
    assert run.parsed_json is None
    assert run.output_json is None


async def test_transport_failure_is_not_retried_by_the_runtime(
    db_session: AsyncSession, requirement_id: UUID
) -> None:
    """传输层已经在 Provider 内部重试过，这里再套一层只会让退避成倍叠加。"""
    runtime, provider = _runtime(
        db_session, [LLMTimeoutError("超时"), '{"title": "不该被用到"}'], max_output_attempts=3
    )

    with pytest.raises(LLMTimeoutError):
        await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    assert provider.remaining == 1, "脚本第二项不该被消费 —— 说明 Runtime 又重试了一次"


async def test_credential_error_is_recorded_with_its_own_code(
    db_session: AsyncSession, requirement_id: UUID
) -> None:
    runtime, _ = _runtime(db_session, [LLMCredentialError("密钥无效")])

    with pytest.raises(LLMCredentialError):
        await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    run = (await db_session.execute(select(AgentRun))).scalar_one()
    assert run.status == AgentRunStatus.FAILED.value
    assert run.error_code == "PROVIDER_CREDENTIAL_INVALID"
    assert run.error_message == "密钥无效"


async def test_non_app_exception_is_not_swallowed(db_session: AsyncSession, requirement_id: UUID) -> None:
    """Provider 抛出未预期异常时原样向上抛，不要伪装成业务错误。

    否则真 bug 会被包装成一个看起来很正常的 AGENT_OUTPUT_INVALID。
    """
    runtime, _ = _runtime(db_session, [RuntimeError("provider 内部炸了")])

    with pytest.raises(RuntimeError):
        await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))


async def test_provider_error_base_class_is_treated_as_transport_failure(
    db_session: AsyncSession, requirement_id: UUID
) -> None:
    """只要是 AppError，都按「传输层失败」记录并向上抛。"""
    runtime, _ = _runtime(db_session, [ProviderError("通用上游错误")])

    with pytest.raises(ProviderError):
        await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    run = (await db_session.execute(select(AgentRun))).scalar_one()
    assert run.status == AgentRunStatus.FAILED.value


# ------------------------------------------------------------------ 参数边界


async def test_max_output_attempts_lower_bound_is_one(db_session: AsyncSession, requirement_id: UUID) -> None:
    """传 0 或负数不能变成「一次都不试」—— 那会静默地什么都不做。"""
    runtime, provider = _runtime(db_session, ["坏的"], max_output_attempts=0)

    with pytest.raises(AgentOutputError):
        await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    assert len(provider.calls) == 1


async def test_workflow_run_id_is_null_when_run_standalone(
    db_session: AsyncSession, requirement_id: UUID
) -> None:
    """独立跑（未挂在工作流下）时 workflow_run_id 为 NULL，这是允许的。"""
    runtime, _ = _runtime(db_session, ['{"title": "x"}'])

    await runtime.run(_spec(), user_prompt="x", context=AgentContext(requirement_id=requirement_id))

    run = (await db_session.execute(select(AgentRun))).scalar_one()
    assert run.workflow_run_id is None


# ------------------------------------------------------------ 外键真的生效了


async def test_foreign_keys_are_enforced(db_session: AsyncSession) -> None:
    """SQLite 默认不强制外键。这条用例确认我们打开的开关是真的有效。"""
    db_session.add(
        AgentRun(
            execution_id=uuid4(),
            attempt=1,
            requirement_id=uuid4(),  # 不存在
            agent_role=AgentRole.PRODUCT.value,
            status=AgentRunStatus.SUCCEEDED.value,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


async def test_deleting_requirement_cascades_to_agent_runs_and_artifacts(
    db_session: AsyncSession, requirement_id: UUID
) -> None:
    """删需求要级联清掉 agent_runs / artifacts。

    模型上声明了 ``ondelete="CASCADE"``，但 SQLite 不开 PRAGMA 时它只是装饰 ——
    会留下指向不存在需求的孤儿记录。声明了却不生效的约束比不声明更危险，
    因为它让人以为有保护。
    """
    db_session.add(
        AgentRun(
            execution_id=uuid4(),
            attempt=1,
            requirement_id=requirement_id,
            agent_role=AgentRole.PRODUCT.value,
            status=AgentRunStatus.SUCCEEDED.value,
        )
    )
    db_session.add(
        Artifact(
            requirement_id=requirement_id,
            type="PRD",
            version=1,
            content_json={"title": "x"},
        )
    )
    await db_session.commit()

    requirement = await db_session.get(Requirement, requirement_id)
    assert requirement is not None
    await db_session.delete(requirement)
    await db_session.commit()

    runs = (await db_session.execute(select(func.count()).select_from(AgentRun))).scalar_one()
    artifacts = (await db_session.execute(select(func.count()).select_from(Artifact))).scalar_one()
    assert runs == 0
    assert artifacts == 0
