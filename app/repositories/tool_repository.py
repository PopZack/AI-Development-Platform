"""tool_calls 仓储。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from app.domain.enums import ToolCallStatus
from app.models.tool import ToolCall
from app.repositories.base import BaseRepository

__all__ = ["ToolCallRepository"]


class ToolCallRepository(BaseRepository[ToolCall]):
    model = ToolCall

    async def list_by_requirement(
        self,
        requirement_id: UUID,
        *,
        status: ToolCallStatus | None = None,
        limit: int = 100,
    ) -> list[ToolCall]:
        stmt = select(ToolCall).where(ToolCall.requirement_id == requirement_id)
        if status is not None:
            stmt = stmt.where(ToolCall.status == status.value)
        stmt = stmt.order_by(ToolCall.created_at.asc()).limit(limit)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_by_status(self, requirement_id: UUID, status: ToolCallStatus) -> int:
        stmt = (
            select(func.count())
            .select_from(ToolCall)
            .where(ToolCall.requirement_id == requirement_id, ToolCall.status == status.value)
        )
        return int((await self.session.execute(stmt)).scalar_one())

    @staticmethod
    def encode(value: Any) -> str | None:
        import json

        return json.dumps(value, ensure_ascii=False, default=str) if value is not None else None
