"""工作流接口（设计文档 §7.4）。

Layer: Presentation（Router）。

Stage 2 只暴露两个接口：

- ``POST /requirements/{id}/runs`` 创建工作流（幂等）
- ``GET /runs/{run_id}`` 查询运行状态

文档 §7.4 里的 ``start`` / ``pause`` / ``resume`` / ``cancel`` 与 ``artifacts``
**刻意没有实现**：它们要驱动状态机、要跑 Agent、要产生交付物，分别属于 Stage 4 和
Stage 3。现在加上只会是一组点了没反应的接口，比不提供更糟 —— 调用方会以为功能可用。

幂等键走 ``Idempotency-Key`` 请求头（文档 §12.2 的示例就是这么发的），而不是请求体：
它的语义是「这次请求的标识」，重试时请求体可以完全不变，头天然跟着一起重发。
"""

from __future__ import annotations

from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Header, Response, status

from app.common.dependencies import CurrentUserDep, WorkflowServiceDep
from app.common.openapi import AUTH_ERROR_RESPONSES
from app.schemas.workflow import WorkflowRunRead

router = APIRouter(tags=["workflows"])

# 限制字符集不是为了好看：幂等键最终会落库、进日志、出现在排查结论里，
# 放任任意字符只会让日志和 grep 变难
IdempotencyKeyHeader = Annotated[
    str,
    Header(
        alias="Idempotency-Key",
        min_length=1,
        max_length=255,
        pattern=r"^[A-Za-z0-9._:\-]+$",
        description=(
            "客户端生成的请求标识，例如 `requirement-001-v1`。"
            "同一个键重复调用只会产生一条工作流；换一份需求复用同一个键会返回 409。"
        ),
    ),
]


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
        "只创建、不执行：状态停在 `CREATED`，`current_step` 为 `PENDING`，"
        "`started_at` 仍为 `null`。实际推进是 Stage 4 的事。"
    ),
    responses=AUTH_ERROR_RESPONSES,
)
async def create_workflow_run(
    requirement_id: UUID,
    response: Response,
    service: WorkflowServiceDep,
    current_user: CurrentUserDep,
    idempotency_key: IdempotencyKeyHeader,
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
