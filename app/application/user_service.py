"""用户用例：查询与「修改自己的资料」。

Layer: Application（Service）—— 编排一次业务用例，并**持有事务边界**。

注册不在这个类里 —— 它属于认证流程，在 ``app/application/auth_service.py``。
Stage 1 这里曾有一份 ``create_user``，Stage 2 已删除：同一个「创建用户」动作
保留两条实现，两边的邮箱归一化、查重规则迟早会走偏。

**已知取舍**：``list_users`` / ``get_user`` 对任何已登录用户开放。这是为了让
「添加项目成员」这条流程可用（你得先能查到那个人的 id）。代价是任何登录用户
都能看到全部用户的邮箱。在一个本地协作平台里这是可接受的，但如果要对外，
应该收紧成「只能看到与你有共同项目的人」，或者对其他人的 ``email`` 做脱敏。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import AuthorizationError, NotFoundError
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserUpdate

__all__ = ["UserService"]

logger = logging.getLogger(__name__)


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)

    async def get_user(self, user_id: UUID) -> User:
        user = await self._users.get(user_id)
        if user is None:
            raise NotFoundError.for_resource("USER", "User does not exist")
        return user

    async def list_users(self, *, limit: int = 50, offset: int = 0) -> tuple[list[User], int]:
        users = await self._users.list_all(limit=limit, offset=offset)
        total = await self._users.count_all()
        return users, total

    async def update_user(self, user_id: UUID, payload: UserUpdate, *, actor: User) -> User:
        """只允许改自己的资料。

        这条规则如果只靠前端不显示按钮来保证，那它就不是规则 —— 换个请求就绕过了。
        文档 §7.5 也专门提醒过审批类接口「不能只依靠前端按钮来控制权限」，
        同一个道理适用于所有写操作。
        """
        if actor.id != user_id:
            raise AuthorizationError(
                "You can only modify your own profile",
                code="USER_SELF_ONLY",
                details={"target_user_id": str(user_id)},
            )

        user = await self.get_user(user_id)
        changes = payload.model_dump(exclude_unset=True)

        if changes.get("display_name") is not None:
            user.display_name = changes["display_name"].strip()

        await self._session.commit()
        return user
