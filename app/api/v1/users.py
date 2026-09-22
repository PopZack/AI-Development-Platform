"""用户接口。

Layer: Presentation（Router）—— 只做三件事：收请求、交 Service、包响应。

文档 §7.1 的 ``/auth/register`` 在 Stage 2 落地。Stage 1 先暴露 ``/users``
以满足 §11「用户、项目、需求 CRUD」的验收要求；Stage 2 引入 JWT 后，
这个路由会收缩为内部管理用途（或直接下线，改由 /auth/register 承接）。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, status

from app.common.dependencies import LimitQuery, OffsetQuery, UserServiceDep
from app.common.openapi import CREATE_ERROR_RESPONSES, READ_ERROR_RESPONSES
from app.schemas.user import PaginatedUsers, UserCreate, UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.post(
    "",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="创建用户",
    responses=CREATE_ERROR_RESPONSES,
)
async def create_user(payload: UserCreate, service: UserServiceDep) -> UserRead:
    user = await service.create_user(payload)
    return UserRead.model_validate(user)


@router.get("", response_model=PaginatedUsers, summary="用户列表")
async def list_users(
    service: UserServiceDep,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> PaginatedUsers:
    users, total = await service.list_users(limit=limit, offset=offset)
    return PaginatedUsers(items=[UserRead.model_validate(u) for u in users], total=total)


@router.get(
    "/{user_id}",
    response_model=UserRead,
    summary="用户详情",
    responses=READ_ERROR_RESPONSES,
)
async def get_user(user_id: UUID, service: UserServiceDep) -> UserRead:
    user = await service.get_user(user_id)
    return UserRead.model_validate(user)


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    summary="修改用户",
    responses=READ_ERROR_RESPONSES,
)
async def update_user(user_id: UUID, payload: UserUpdate, service: UserServiceDep) -> UserRead:
    user = await service.update_user(user_id, payload)
    return UserRead.model_validate(user)
