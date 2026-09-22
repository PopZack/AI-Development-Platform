"""用户用例。

Layer: Application（Service）—— 编排一次业务用例，并**持有事务边界**。

文档 §5.2 对 Service 的约束：不直接承载所有底层技术细节。
所以这里只做「查重 → 组装实体 → 交给仓储 → 提交」，SQL 全在 Repository，
密码技术细节全在 infrastructure/auth。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError, NotFoundError
from app.domain.enums import UserStatus
from app.infrastructure.auth.password import hash_password
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.user import UserCreate, UserUpdate

__all__ = ["UserService"]

logger = logging.getLogger(__name__)


class UserService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._users = UserRepository(session)

    async def create_user(self, payload: UserCreate) -> User:
        # 邮箱统一小写后落库，否则唯一索引挡不住 "A@x.com" 和 "a@x.com" 这种重复
        email = payload.email.strip().lower()
        if await self._users.email_exists(email):
            raise ConflictError(
                "Email already registered",
                code="EMAIL_ALREADY_REGISTERED",
                details={"email": email},
            )

        user = User(
            email=email,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name.strip(),
            status=UserStatus.ACTIVE.value,
        )
        await self._users.add(user)
        await self._session.commit()
        logger.info("user created | id=%s email=%s", user.id, user.email)
        return user

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
