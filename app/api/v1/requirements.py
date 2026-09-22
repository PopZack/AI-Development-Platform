"""需求接口（文档 §7.3）。

Layer: Presentation（Router）。

这里只有 4 个 Stage 1 接口。文档 §7.3 里的
``POST /requirements/{id}/analyze`` 与 ``/plan`` 属于 Stage 3 ——
它们要调 LLM，必须等 Agent Runtime 与 Pydantic 输出校验就位后再加，
否则会出现「模型输出直接落库」这种文档 §14.3 明确禁止的做法。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.common.dependencies import LimitQuery, OffsetQuery, RequirementServiceDep
from app.common.openapi import CREATE_ERROR_RESPONSES, READ_ERROR_RESPONSES
from app.schemas.requirement import (
    PaginatedRequirements,
    RequirementCreate,
    RequirementRead,
    RequirementUpdate,
)

# 路径同时挂在 /projects/{project_id}/requirements 与 /requirements/{id} 下，
# 所以 router 不设 prefix，避免出现 /requirements/projects/... 这种畸形路径
router = APIRouter(tags=["requirements"])


@router.post(
    "/projects/{project_id}/requirements",
    response_model=RequirementRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建需求",
    responses=CREATE_ERROR_RESPONSES,
)
async def create_requirement(
    project_id: UUID, payload: RequirementCreate, service: RequirementServiceDep
) -> RequirementRead:
    requirement = await service.create_requirement(project_id, payload)
    return RequirementRead.model_validate(requirement)


@router.get(
    "/projects/{project_id}/requirements",
    response_model=PaginatedRequirements,
    summary="项目下的需求列表",
    responses=READ_ERROR_RESPONSES,
)
async def list_requirements(
    project_id: UUID,
    service: RequirementServiceDep,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> PaginatedRequirements:
    items, total = await service.list_requirements(project_id, limit=limit, offset=offset)
    return PaginatedRequirements(items=[RequirementRead.model_validate(r) for r in items], total=total)


@router.get(
    "/requirements/{requirement_id}",
    response_model=RequirementRead,
    summary="需求详情",
    responses=READ_ERROR_RESPONSES,
)
async def get_requirement(requirement_id: UUID, service: RequirementServiceDep) -> RequirementRead:
    requirement = await service.get_requirement(requirement_id)
    return RequirementRead.model_validate(requirement)


@router.patch(
    "/requirements/{requirement_id}",
    response_model=RequirementRead,
    summary="修改需求",
    description="不允许修改 status —— 状态迁移由 Domain 规则控制（文档 §3.3）。内容变更会使 version 自增。",
    responses=CREATE_ERROR_RESPONSES,
)
async def update_requirement(
    requirement_id: UUID, payload: RequirementUpdate, service: RequirementServiceDep
) -> RequirementRead:
    requirement = await service.update_requirement(requirement_id, payload)
    return RequirementRead.model_validate(requirement)
