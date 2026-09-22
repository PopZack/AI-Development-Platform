"""工作流接口（设计文档 §7.4）。

Layer: Presentation（Router）。

- ``POST /requirements/{id}/runs`` 创建工作流（幂等，Stage 2）
- ``GET  /runs/{id}`` 查询运行状态（Stage 2）
- ``POST /runs/{id}/start``     启动执行：一路跑到需要人工介入的停点
- ``POST /runs/{id}/resume``    从停点恢复（补丁审批批准后 / 驳回后返工）
- ``POST /runs/{id}/approve``   最终人工批准（仅 OWNER）
- ``POST /runs/{id}/reject``    最终人工驳回（仅 OWNER）
- ``POST /runs/{id}/cancel``    取消（非终态均可）
- ``GET  /runs/{id}/artifacts`` 这条工作流产出的交付物

幂等键走 ``Idempotency-Key`` 请求头（文档 §12.2 的示例就是这么发的），而不是请求体：
它的语义是「这次请求的标识」，重试时请求体可以完全不变，头天然跟着一起重发。

⚠️ start/resume 是**同步长请求**：内部会真实调模型（一次 20~40 秒）。
文档 §13 的异步执行 + SSE 属 Stage 5。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Header, Response, status
from pydantic import BaseModel, Field

from app.common.dependencies import (
    CurrentUserDep,
    WorkflowOrchestratorDep,
    WorkflowServiceDep,
)
from app.common.openapi import AUTH_ERROR_RESPONSES
from app.schemas.agent import AgentArtifactList, ArtifactRead
from app.schemas.workflow import RunExecuteResult, WorkflowRunRead

router = APIRouter(tags=["workflows"])

# 限制字符集不是为了好看：幂等键最终会落库、进日志、出现在排查结论里，
# 放任任意字符只会让日志和 grep 变难
IdempotencyKeyHeader = Header(
    alias="Idempotency-Key",
    min_length=1,
    max_length=255,
    pattern=r"^[A-Za-z0-9._:\-]+$",
    description=(
        "客户端生成的请求标识，例如 `requirement-001-v1`。"
        "同一个键重复调用只会产生一条工作流；换一份需求复用同一个键会返回 409。"
    ),
)


class ResumeBody(BaseModel):
    """恢复执行的请求体。

    ``approval_id`` 只在补丁审批暂停点必填；驳回/返工后的恢复不需要。
    """

    approval_id: UUID | None = Field(default=None, description="补丁审批的 id（start 暂停时返回的那个）")


@router.post(
    "/requirements/{requirement_id}/runs",
    response_model=WorkflowRunRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建工作流（幂等，需 OWNER 或 DEVELOPER）",
    description=(
        "必须带 `Idempotency-Key` 请求头。\n\n"
        "- 新建成功 → `201`\n"
        "- 命中同一个键（重试/双击） → `200`，并带响应头 `Idempotent-Replay: true`，"
        "返回的是当初那条工作流\n"
        "- 同一个键用在另一份需求上 → `409 IDEMPOTENCY_KEY_CONFLICT`\n\n"
        "只创建、不执行：状态停在 `CREATED`。实际执行用 `/runs/{id}/start`。"
    ),
    responses=AUTH_ERROR_RESPONSES,
)
async def create_workflow_run(
    requirement_id: UUID,
    response: Response,
    service: WorkflowServiceDep,
    current_user: CurrentUserDep,
    idempotency_key: str = IdempotencyKeyHeader,  # type: ignore[assignment]
) -> WorkflowRunRead:
    run, created = await service.create_run(requirement_id, idempotency_key, actor=current_user)

    if not created:
        # 重放：什么都没创建，所以不是 201。额外给一个响应头，否则调用方
        # 只能靠「200 vs 201」这一个信号猜，日志里也看不出发生过重放
        response.status_code = status.HTTP_200_OK
        response.headers["Idempotent-Replay"] = "true"

    return WorkflowRunRead.model_validate(run)


@router.get(
    "/runs/{run_id}",
    response_model=WorkflowRunRead,
    summary="查询运行状态",
    responses=AUTH_ERROR_RESPONSES,
)
async def get_workflow_run(
    run_id: UUID, service: WorkflowServiceDep, current_user: CurrentUserDep
) -> WorkflowRunRead:
    run = await service.get_run(run_id, actor=current_user)
    return WorkflowRunRead.model_validate(run)


@router.post(
    "/runs/{run_id}/start",
    response_model=RunExecuteResult,
    summary="启动执行（需 OWNER 或 DEVELOPER；慢，会真实调模型）",
    description=(
        "从 `CREATED` 一路执行：Product Agent → Architect Agent → Developer Agent →\n"
        "尝试写盘。到达第一个需要人工介入的停点时返回：\n\n"
        "- `paused=true, pause_reason=tool_approval`：补丁需要 OWNER 批准，\n"
        "  `approval_id` 直接给出；批准后带它调 `/resume`\n"
        "- Agent 失败 → 工作流变 `FAILED` 并返回错误\n\n"
        "只允许对 `CREATED` 状态的运行调用；重复调用返回 409。"
    ),
    responses=AUTH_ERROR_RESPONSES,
)
async def start_workflow_run(
    run_id: UUID, orchestrator: WorkflowOrchestratorDep, current_user: CurrentUserDep
) -> RunExecuteResult:
    outcome = await orchestrator.start(run_id, actor=current_user)
    return RunExecuteResult(
        run=WorkflowRunRead.model_validate(outcome.run),
        paused=outcome.paused,
        pause_reason=outcome.pause_reason,
        approval_id=outcome.approval_id,
    )


@router.post(
    "/runs/{run_id}/resume",
    response_model=RunExecuteResult,
    summary="从停点恢复执行（慢，会真实调模型）",
    description=(
        "- 补丁审批暂停点：请求体必须带 `approval_id`（OWNER 已批准）\n"
        "- `REJECTED` / `REVISION_REQUIRED`：不带 approval_id，重新走实现 → 测试 → 审查\n"
        "- 最终审批暂停点：不能用 resume，走 `/approve` 或 `/reject`"
    ),
    responses=AUTH_ERROR_RESPONSES,
)
async def resume_workflow_run(
    run_id: UUID,
    orchestrator: WorkflowOrchestratorDep,
    current_user: CurrentUserDep,
    body: ResumeBody | None = None,
) -> RunExecuteResult:
    outcome = await orchestrator.resume(
        run_id, actor=current_user, approval_id=body.approval_id if body else None
    )
    return RunExecuteResult(
        run=WorkflowRunRead.model_validate(outcome.run),
        paused=outcome.paused,
        pause_reason=outcome.pause_reason,
        approval_id=outcome.approval_id,
    )


@router.post(
    "/runs/{run_id}/approve",
    response_model=WorkflowRunRead,
    summary="最终人工批准（仅项目 OWNER）",
    description="只对 `(WAITING_APPROVAL, APPROVAL)` 状态有效。批准后工作流直接 `COMPLETED`。",
    responses=AUTH_ERROR_RESPONSES,
)
async def approve_workflow_run(
    run_id: UUID, orchestrator: WorkflowOrchestratorDep, current_user: CurrentUserDep
) -> WorkflowRunRead:
    run = await orchestrator.approve(run_id, actor=current_user)
    return WorkflowRunRead.model_validate(run)


@router.post(
    "/runs/{run_id}/reject",
    response_model=WorkflowRunRead,
    summary="最终人工驳回（仅项目 OWNER）",
    description="驳回后可调 `/resume` 让 Developer Agent 返工（REJECTED → REVISION_REQUIRED → 实现）。",
    responses=AUTH_ERROR_RESPONSES,
)
async def reject_workflow_run(
    run_id: UUID, orchestrator: WorkflowOrchestratorDep, current_user: CurrentUserDep
) -> WorkflowRunRead:
    run = await orchestrator.reject(run_id, actor=current_user)
    return WorkflowRunRead.model_validate(run)


@router.post(
    "/runs/{run_id}/cancel",
    response_model=WorkflowRunRead,
    summary="取消执行（需 OWNER 或 DEVELOPER）",
    description="非终态且未到 `APPROVED` 的运行都可以取消。",
    responses=AUTH_ERROR_RESPONSES,
)
async def cancel_workflow_run(
    run_id: UUID, orchestrator: WorkflowOrchestratorDep, current_user: CurrentUserDep
) -> WorkflowRunRead:
    run = await orchestrator.cancel(run_id, actor=current_user)
    return WorkflowRunRead.model_validate(run)


@router.get(
    "/runs/{run_id}/artifacts",
    response_model=AgentArtifactList,
    summary="这条工作流产出的交付物",
    responses=AUTH_ERROR_RESPONSES,
)
async def list_workflow_artifacts(
    run_id: UUID, orchestrator: WorkflowOrchestratorDep, current_user: CurrentUserDep
) -> AgentArtifactList:
    items, total = await orchestrator.list_artifacts(run_id, actor=current_user)
    return AgentArtifactList(
        items=[ArtifactRead.from_entity(a) for a in items],
        total=total,
    )
