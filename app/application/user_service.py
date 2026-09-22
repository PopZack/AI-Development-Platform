"""用户用例：查询与修改。

Layer: Application（Service）—— 编排一次业务用例，并**持有事务边界**。

注册不在这个类里 —— 它属于认证流程，在 ``app/application/auth_service.py``。
Stage 1 这里曾有一份 ``create_user``，Stage 2 已删除：同一个「创建用户」动作
保留两条实现，两边的邮箱归一化、查重规则迟早会走偏。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import NotFoundError
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

    async def update_user(self, user_id: UUID, payload: UserUpdate) -> User:
        user = await self.get_user(user_id)
        changes = payload.model_dump(exclude_unset=True)

        if changes.get("display_name") is not None:
            user.display_name = changes["display_name"].strip()
        if changes.get("status") is not None:
            user.status = str(changes["status"])

        await self._session.commit()
        return user
