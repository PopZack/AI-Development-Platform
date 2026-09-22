"""通用仓储基类。

Layer: Repository —— 只负责数据读写。

文档 §5.2 给 Repository 划的边界很明确：
- 负责：查询和保存数据库实体
- 不应该：决定业务是否允许执行

所以这里不出现任何「if 状态 == xxx 则不允许」的判断，
那些判断属于 domain/ 和 application/。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.infrastructure.db.base import Base

__all__ = ["BaseRepository"]


class BaseRepository[ModelT: Base]:
    model: type[ModelT]

    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def get(self, entity_id: UUID) -> ModelT | None:
        return await self.session.get(self.model, entity_id)

    async def add(self, entity: ModelT) -> ModelT:
        """加入会话并 ``flush()``。

        用 flush 而不是 commit：事务边界归 Service（文档 §5.3），
        但 flush 能让实体立刻拿到主键与时间戳，调用方无需再手动 refresh。
        """
        self.session.add(entity)
        await self.session.flush()
        return entity

    async def delete(self, entity: ModelT) -> None:
        await self.session.delete(entity)
        await self.session.flush()

    async def count_all(self) -> int:
        stmt = select(func.count()).select_from(self.model)
        return int((await self.session.execute(stmt)).scalar_one())

    async def list_all(self, *, limit: int = 50, offset: int = 0) -> list[ModelT]:
        stmt = (
            select(self.model)  # type: ignore[arg-type]
            .order_by(self.model.created_at.desc())  # type: ignore[attr-defined]
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.execute(stmt)).scalars().all())
