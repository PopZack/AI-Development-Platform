"""用户接口。

Layer: Presentation（Router）—— 只做三件事：收请求、传 current_user、转交 Service。

这里是 Stage 2 收尾时补上鉴权的最后一个模块：

| 接口 | 要求 |
|---|---|
| ``GET /users`` | 登录（添加成员时得先能查到人） |
| ``GET /users/{id}`` | 登录 |
| ``PATCH /users/{id}`` | **仅本人**，且只能改 ``display_name`` |

不再有 ``POST /users`` —— 注册统一走 ``/auth/register``。
"""

from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter

from app.common.dependencies import CurrentUserDep, LimitQuery, OffsetQuery, UserServiceDep
from app.common.openapi import AUTH_ERROR_RESPONSES
from app.schemas.user import PaginatedUsers, UserRead, UserUpdate

router = APIRouter(prefix="/users", tags=["users"])


@router.get(
    "",
    response_model=PaginatedUsers,
    summary="用户列表",
    description="任何已登录用户可见 —— 添加项目成员前需要先查到对方的 id。",
    responses=AUTH_ERROR_RESPONSES,
)
async def list_users(
    service: UserServiceDep,
    current_user: CurrentUserDep,
    limit: LimitQuery = 50,
    offset: OffsetQuery = 0,
) -> PaginatedUsers:
    users, total = await service.list_users(limit=limit, offset=offset)
    return PaginatedUsers(items=[UserRead.model_validate(u) for u in users], total=total)


@router.get(
    "/{user_id}",
    response_model=UserRead,
    summary="用户详情",
    responses=AUTH_ERROR_RESPONSES,
)
async def get_user(user_id: UUID, service: UserServiceDep, current_user: CurrentUserDep) -> UserRead:
    user = await service.get_user(user_id)
    return UserRead.model_validate(user)


@router.patch(
    "/{user_id}",
    response_model=UserRead,
    summary="修改自己的资料",
    description="只能改自己的，且只能改 display_name —— 账号状态不属于自助修改范围。",
    responses=AUTH_ERROR_RESPONSES,
)
async def update_user(
    user_id: UUID,
    payload: UserUpdate,
    service: UserServiceDep,
    current_user: CurrentUserDep,
) -> UserRead:
    user = await service.update_user(user_id, payload, actor=current_user)
    return UserRead.model_validate(user)
