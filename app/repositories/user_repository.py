"""用户仓储。

Layer: Repository。
"""

from __future__ import annotations

from sqlalchemy import func, select

from app.models.user import User
from app.repositories.base import BaseRepository

__all__ = ["UserRepository"]


class UserRepository(BaseRepository[User]):
    model = User

    async def get_by_email(self, email: str) -> User | None:
        stmt = select(User).where(func.lower(User.email) == email.strip().lower())
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def email_exists(self, email: str) -> bool:
        stmt = select(User.id).where(func.lower(User.email) == email.strip().lower()).limit(1)
        return (await self.session.execute(stmt)).scalar_one_or_none() is not None
