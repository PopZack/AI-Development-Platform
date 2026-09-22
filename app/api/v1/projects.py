"""项目接口（文档 §7.2）。

Layer: Presentation（Router）。

Stage 1 的已知短板：``GET /projects`` 不带参数会列出全部项目。这是没有
认证的必然结果，**不能带到 Stage 2**。Stage 2 会把 JWT 主体作为
``owner_id`` 传进 Service，请求参数里的这个开关会被删掉。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.common.dependencies import LimitQuery, OffsetQuery, ProjectServiceDep
from app.common.openapi import COMMON_ERROR_RESPONSES, CREATE_ERROR_RESPONSES, READ_ERROR_RESPONSES
from app.schemas.project import (
    PaginatedProjectMembers,
    PaginatedProjects,
    ProjectCreate,
    ProjectMemberCreate,
    ProjectMemberRead,
    ProjectRead,
    ProjectUpdate,
)

router = APIRouter(prefix="/projects", tags=["projects"])


@router.post(
    "",
    response_model=ProjectRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建项目",
    responses=CREATE_ERROR_RESPONSES,
)
async def create_project(payload: ProjectCreate, service: ProjectServiceDep) -> ProjectRead:
    project = await service.create_project(payload)
    return ProjectRead.model_validate(project)


@router.get("", response_model=PaginatedProjects, summary="项目列表")
async def list_projects(
    service: ProjectServiceDep,
    owner_id: UUID | None = None,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> PaginatedProjects:
    projects, total = await service.list_projects(owner_id=owner_id, limit=limit, offset=offset)
    return PaginatedProjects(items=[ProjectRead.model_validate(p) for p in projects], total=total)


@router.get(
    "/{project_id}",
    response_model=ProjectRead,
    summary="项目详情",
    responses=READ_ERROR_RESPONSES,
)
async def get_project(project_id: UUID, service: ProjectServiceDep) -> ProjectRead:
    project = await service.get_project(project_id)
    return ProjectRead.model_validate(project)


@router.patch(
    "/{project_id}",
    response_model=ProjectRead,
    summary="修改项目",
    responses=READ_ERROR_RESPONSES,
)
async def update_project(project_id: UUID, payload: ProjectUpdate, service: ProjectServiceDep) -> ProjectRead:
    project = await service.update_project(project_id, payload)
    return ProjectRead.model_validate(project)


@router.get(
    "/{project_id}/members",
    response_model=PaginatedProjectMembers,
    summary="项目成员列表",
    responses=READ_ERROR_RESPONSES,
)
async def list_project_members(project_id: UUID, service: ProjectServiceDep) -> PaginatedProjectMembers:
    members = await service.list_members(project_id)
    return PaginatedProjectMembers(
        items=[ProjectMemberRead.model_validate(m) for m in members], total=len(members)
    )


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberRead,
    status_code=status.HTTP_201_CREATED,
    summary="添加项目成员",
    responses=COMMON_ERROR_RESPONSES,
)
async def add_project_member(
    project_id: UUID, payload: ProjectMemberCreate, service: ProjectServiceDep
) -> ProjectMemberRead:
    member = await service.add_member(project_id, payload)
    return ProjectMemberRead.model_validate(member)
