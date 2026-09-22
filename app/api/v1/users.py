"""用户接口。

Layer: Presentation（Router）—— 只做三件事：收请求、交 Service、包响应。

注册入口已由 ``POST /auth/register`` 承接，所以这里的 ``POST /users``
在 Stage 2 下线了：**同一个业务动作保留两条入口，两边的校验规则迟早会走偏**
（比如一边统一小写邮箱、另一边忘了）。留下的是查询与修改。

本模块的接口目前尚未强制认证 —— 给业务路由加认证与资源级权限是 Stage 2 的
下一个提交。在那之前不要部署到任何共享环境。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from app.common.dependencies import LimitQuery, OffsetQuery, UserServiceDep
from app.common.openapi import READ_ERROR_RESPONSES
from app.schemas.user import PaginatedUsers, UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


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
