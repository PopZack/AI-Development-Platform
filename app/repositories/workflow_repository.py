"""工作流仓储。

Layer: Repository。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select

from app.models.workflow import WorkflowRun
from app.repositories.base import BaseRepository

__all__ = ["WorkflowRunRepository"]


class WorkflowRunRepository(BaseRepository[WorkflowRun]):
    model = WorkflowRun

    async def get_by_idempotency_key(self, idempotency_key: str) -> WorkflowRun | None:
        stmt = select(WorkflowRun).where(WorkflowRun.idempotency_key == idempotency_key)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_requirement(self, requirement_id: UUID) -> list[WorkflowRun]:
        stmt = (
            select(WorkflowRun)
            .where(WorkflowRun.requirement_id == requirement_id)
            .order_by(WorkflowRun.created_at.desc())
        )
        return list((await self.session.execute(stmt)).scalars().all())
