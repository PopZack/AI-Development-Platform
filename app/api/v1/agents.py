"""Agent 驱动的接口（文档 §7.3 的 analyze / plan）。

Layer: Presentation（Router）。

这两个接口和普通 CRUD 有本质区别：**它们会真的去调模型**。带来三件事：

1. **很慢** —— 实测一次 20~30 秒。这是 Stage 3 的现状；
   文档 §13 的异步执行 + SSE 属 Stage 5。
2. **花钱** —— 每次几十上千 token。所以权限要求**写角色**
   （OWNER / DEVELOPER），VIEWER 能看结果但不能触发。
3. **会改需求状态** —— analyze 会把 DRAFT 推到 ANALYZING，
   所以没有做过 analyze 就不能 plan（状态机会直接拒掉 DRAFT → DESIGNED）。

``agent_runs`` 刻意不通过 API 暴露 —— 它是运维排障数据，不是产品数据。
"""

from __future__ import annotations

from pathlib import Path
from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Query

from app.common.dependencies import (
    AgentServiceDep,
    CurrentUserDep,
    LimitQuery,
    OffsetQuery,
    SettingsDep,
)
from app.common.openapi import AUTH_ERROR_RESPONSES
from app.domain.enums import ArtifactType, WorkflowStatus, WorkflowStep
from app.schemas.agent import (
    AgentArtifactList,
    ArtifactRead,
    DeliverablesSummary,
    RequirementBrief,
    WorkflowRunBrief,
)

router = APIRouter(tags=["agents"])


@router.post(
    "/requirements/{requirement_id}/analyze",
    response_model=ArtifactRead,
    status_code=201,
    summary="运行 Product Agent，生成 PRD（需 OWNER 或 DEVELOPER）",
    description=(
        "同步接口，**实测一次 20~30 秒**（等待模型返回）。\n\n"
        "状态迁移：`DRAFT → ANALYZING`（需求改过之后可从 `DESIGNED` 重跑）。\n"
        "产出会同时写入：\n\n"
        "- `artifacts`（按版本递增，保留历史）\n"
        "- `requirements.prd_json`（当前版本快照，便于读取需求时一次拿到）\n\n"
        "模型输出未通过 Schema 校验时，需求会变成 `FAILED`，可以重新调用本接口重试。"
    ),
    responses=AUTH_ERROR_RESPONSES,
)
async def analyze_requirement(
    requirement_id: UUID, service: AgentServiceDep, current_user: CurrentUserDep
) -> ArtifactRead:
    artifact = await service.analyze_requirement(requirement_id, actor=current_user)
    return ArtifactRead.from_entity(artifact)


@router.post(
    "/requirements/{requirement_id}/plan",
    response_model=ArtifactRead,
    status_code=201,
    summary="运行 Architect Agent，生成技术设计（需 OWNER 或 DEVELOPER）",
    description=(
        "同步接口，**实测一次 20~30 秒**。\n\n"
        "**必须先调用过 analyze**：状态迁移要求 `ANALYZING → DESIGNED`，"
        "从 `DRAFT` 直接发起会被 `409 REQUIREMENT_STATUS_CONFLICT` 拒掉。\n"
        "产出写入 `artifacts`（按版本递增）。"
    ),
    responses=AUTH_ERROR_RESPONSES,
)
async def plan_requirement(
    requirement_id: UUID, service: AgentServiceDep, current_user: CurrentUserDep
) -> ArtifactRead:
    artifact = await service.plan_requirement(requirement_id, actor=current_user)
    return ArtifactRead.from_entity(artifact)


@router.get(
    "/requirements/{requirement_id}/artifacts",
    response_model=AgentArtifactList,
    summary="列出该需求已生成的交付物",
    description="按版本升序返回。`type` 可选：`PRD` / `ARCHITECTURE`。",
    responses=AUTH_ERROR_RESPONSES,
)
async def list_artifacts(
    requirement_id: UUID,
    service: AgentServiceDep,
    current_user: CurrentUserDep,
    type: Annotated[ArtifactType | None, Query(description="按交付物类型过滤")] = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> AgentArtifactList:
    items, total = await service.list_artifacts(
        requirement_id,
        actor=current_user,
        artifact_type=type,
        limit=limit,
        offset=offset,
    )
    return AgentArtifactList(items=[ArtifactRead.from_entity(a) for a in items], total=total)


@router.get(
    "/requirements/{requirement_id}/deliverables",
    response_model=DeliverablesSummary,
    summary="交付物汇总：每种类型取最新版本 + 工作区文件清单",
    description=(
        "Stage 4 验收项「用户可以查看完整交付物」的落点：\n\n"
        "- `deliverables` 按类型取**最新版本**（历史版本用 `/artifacts` 带过滤查）\n"
        "- `workspace_files` 是工具网关实际写入工作区的文件\n"
        "- `latest_run` 是最近一次工作流（可能为 null）\n\n"
        "项目成员（含 VIEWER）可读。"
    ),
    responses=AUTH_ERROR_RESPONSES,
)
async def get_deliverables_summary(
    requirement_id: UUID,
    service: AgentServiceDep,
    settings: SettingsDep,
    current_user: CurrentUserDep,
) -> DeliverablesSummary:
    summary = await service.deliverables_summary(
        requirement_id, actor=current_user, workspace_root=Path(settings.workspace_root)
    )
    return DeliverablesSummary(
        requirement=RequirementBrief(
            id=summary["requirement"].id,
            title=summary["requirement"].title,
            status=summary["requirement"].status,
        ),
        latest_run=(
            WorkflowRunBrief(
                id=summary["latest_run"].id,
                status=WorkflowStatus(summary["latest_run"].status),
                current_step=WorkflowStep(summary["latest_run"].current_step),
            )
            if summary["latest_run"]
            else None
        ),
        deliverables=[ArtifactRead.from_entity(a) for a in summary["deliverables"]],
        workspace_files=summary["workspace_files"],
    )
