"""approvals 仓储。"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select

from app.domain.enums import ApprovalStatus
from app.models.approval import Approval
from app.repositories.base import BaseRepository

__all__ = ["ApprovalRepository"]


class ApprovalRepository(BaseRepository[Approval]):
    model = Approval

    async def list_by_requirement(
        self,
        requirement_id: UUID,
        *,
        status: ApprovalStatus | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Approval]:
        stmt = select(Approval).where(Approval.requirement_id == requirement_id)
        if status is not None:
            stmt = stmt.where(Approval.status == status.value)
        stmt = stmt.order_by(Approval.created_at.desc()).limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_by_requirement(
        self, requirement_id: UUID, *, status: ApprovalStatus | None = None
    ) -> int:
        stmt = select(func.count()).select_from(Approval).where(Approval.requirement_id == requirement_id)
        if status is not None:
            stmt = stmt.where(Approval.status == status.value)
        return int((await self.session.execute(stmt)).scalar_one())

    async def count_pending_global(self) -> int:
        """全局待审批数。给「审批中心」页面用的，不分项目。"""
        stmt = (
            select(func.count()).select_from(Approval).where(Approval.status == ApprovalStatus.PENDING.value)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    @staticmethod
    def utcnow() -> datetime:
        return datetime.now(UTC)
