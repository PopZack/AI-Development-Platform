"""需求仓储。

Layer: Repository。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select

from app.models.requirement import Requirement
from app.repositories.base import BaseRepository

__all__ = ["RequirementRepository"]


class RequirementRepository(BaseRepository[Requirement]):
    model = Requirement

    async def list_by_project(
        self, project_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> list[Requirement]:
        stmt = (
            select(Requirement)
            .where(Requirement.project_id == project_id)
            .order_by(Requirement.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_by_project(self, project_id: UUID) -> int:
        stmt = select(func.count()).select_from(Requirement).where(Requirement.project_id == project_id)
        return int((await self.session.execute(stmt)).scalar_one())
