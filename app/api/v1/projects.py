"""项目接口（文档 §7.2）。

Layer: Presentation（Router）—— 只做三件事：收请求、传 current_user、转交 Service。

权限要求（越权统一 403；采取「诚实 403」而非「伪装 404」，因为项目 ID 是 UUID
不可枚举，泄露「资源存在」的风险很低，而 403 对排查问题明显更有帮助）：

| 接口 | 要求 |
|---|---|
| ``POST /projects`` | 登录。owner 取自令牌，请求体里没有这个字段 |
| ``GET /projects`` | 登录。只返回自己参与的项目，**没有**查看别人列表的开关 |
| ``GET /projects/{id}`` | 项目成员 |
| ``PATCH /projects/{id}`` | OWNER |
| ``GET /projects/{id}/members`` | 项目成员 |
| ``POST /projects/{id}/members`` | OWNER |
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.common.dependencies import CurrentUserDep, LimitQuery, OffsetQuery, ProjectServiceDep
from app.common.openapi import AUTH_ERROR_RESPONSES
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
    description="创建者自动成为项目 Owner。owner 取自访问令牌，请求体里没有这个字段。",
    responses=AUTH_ERROR_RESPONSES,
)
async def create_project(
    payload: ProjectCreate, service: ProjectServiceDep, current_user: CurrentUserDep
) -> ProjectRead:
    project = await service.create_project(payload, actor=current_user)
    return ProjectRead.model_validate(project)


@router.get(
    "",
    response_model=PaginatedProjects,
    summary="我参与的项目列表",
    responses=AUTH_ERROR_RESPONSES,
)
async def list_projects(
    service: ProjectServiceDep,
    current_user: CurrentUserDep,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> PaginatedProjects:
    projects, total = await service.list_projects(actor=current_user, limit=limit, offset=offset)
    return PaginatedProjects(items=[ProjectRead.model_validate(p) for p in projects], total=total)


@router.get(
    "/{project_id}",
    response_model=ProjectRead,
    summary="项目详情",
    responses=AUTH_ERROR_RESPONSES,
)
async def get_project(
    project_id: UUID, service: ProjectServiceDep, current_user: CurrentUserDep
) -> ProjectRead:
    project = await service.get_project(project_id, actor=current_user)
    return ProjectRead.model_validate(project)


@router.patch(
    "/{project_id}",
    response_model=ProjectRead,
    summary="修改项目（仅 OWNER）",
    responses=AUTH_ERROR_RESPONSES,
)
async def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    service: ProjectServiceDep,
    current_user: CurrentUserDep,
) -> ProjectRead:
    project = await service.update_project(project_id, payload, actor=current_user)
    return ProjectRead.model_validate(project)


@router.get(
    "/{project_id}/members",
    response_model=PaginatedProjectMembers,
    summary="项目成员列表",
    responses=AUTH_ERROR_RESPONSES,
)
async def list_project_members(
    project_id: UUID, service: ProjectServiceDep, current_user: CurrentUserDep
) -> PaginatedProjectMembers:
    members = await service.list_members(project_id, actor=current_user)
    return PaginatedProjectMembers(
        items=[ProjectMemberRead.model_validate(m) for m in members], total=len(members)
    )


@router.post(
    "/{project_id}/members",
    response_model=ProjectMemberRead,
    status_code=status.HTTP_201_CREATED,
    summary="添加项目成员（仅 OWNER）",
    responses=AUTH_ERROR_RESPONSES,
)
async def add_project_member(
    project_id: UUID,
    payload: ProjectMemberCreate,
    service: ProjectServiceDep,
    current_user: CurrentUserDep,
) -> ProjectMemberRead:
    member = await service.add_member(project_id, payload, actor=current_user)
    return ProjectMemberRead.model_validate(member)
