"""工作流仓储。

Layer: Repository。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select, update

from app.domain.enums import WorkflowStatus, WorkflowStep
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

    async def claim_start(self, run_id: UUID) -> bool:
        """原子地把 CREATED 运行认领为 RUNNING，返回是否认领成功。

        异步执行后 start 不再持有请求等结果 —— 两个请求（可能在**不同 worker**）
        同时对同一个 CREATED 运行调 start，靠「先读再判断」必然双双通过。
        唯一可靠的是数据库的原子条件更新：谁更新到了谁执行。
        """
        stmt = (
            update(WorkflowRun)
            .where(WorkflowRun.id == run_id, WorkflowRun.status == WorkflowStatus.CREATED.value)
            .values(
                status=WorkflowStatus.RUNNING.value,
                current_step=WorkflowStep.PRODUCT_AGENT.value,
            )
        )
        result = await self.session.execute(stmt)
        return result.rowcount == 1

    async def list_by_statuses(self, statuses: list[str]) -> list[WorkflowRun]:
        stmt = select(WorkflowRun).where(WorkflowRun.status.in_(statuses))
        return list((await self.session.execute(stmt)).scalars().all())

    async def get_status(self, run_id: UUID) -> WorkflowStatus | None:
        """只查状态列。刻意用独立查询而不是刷新 ORM 对象：身份映射会直接返回缓存。"""
        stmt = select(WorkflowRun.status).where(WorkflowRun.id == run_id)
        value = (await self.session.execute(stmt)).scalar_one_or_none()
        return WorkflowStatus(value) if value is not None else None
