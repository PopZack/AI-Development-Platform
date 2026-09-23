"""agent_runs / artifacts 仓储。

Layer: Repository。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select

from app.domain.enums import AgentRole, ArtifactType
from app.models.agent import AgentRun, Artifact
from app.repositories.base import BaseRepository

__all__ = ["AgentRunRepository", "ArtifactRepository"]


class AgentRunRepository(BaseRepository[AgentRun]):
    model = AgentRun

    async def list_by_execution(self, execution_id: UUID) -> list[AgentRun]:
        """同一次逻辑执行的所有尝试，按尝试顺序返回。

        这是 `attempt` + `execution_id` 这套设计存在的意义：
        能一眼看出「第一次回了坏 JSON、第二次改好了」，而不是只看到最终结果。
        """
        stmt = select(AgentRun).where(AgentRun.execution_id == execution_id).order_by(AgentRun.attempt.asc())
        return list((await self.session.execute(stmt)).scalars().all())

    async def list_by_requirement(
        self, requirement_id: UUID, *, agent_role: AgentRole | None = None
    ) -> list[AgentRun]:
        stmt = select(AgentRun).where(AgentRun.requirement_id == requirement_id)
        if agent_role is not None:
            stmt = stmt.where(AgentRun.agent_role == agent_role.value)
        stmt = stmt.order_by(AgentRun.created_at.asc())
        return list((await self.session.execute(stmt)).scalars().all())


class ArtifactRepository(BaseRepository[Artifact]):
    model = Artifact

    async def list_by_requirement(
        self,
        requirement_id: UUID,
        *,
        artifact_type: ArtifactType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[Artifact]:
        stmt = select(Artifact).where(Artifact.requirement_id == requirement_id)
        if artifact_type is not None:
            stmt = stmt.where(Artifact.type == artifact_type.value)
        # 按**创建时间**升序 —— version 是「同需求同类型内」的编号，
        # PRD v1 和 PATCH v1 都是 1，拿它做第一排序键时顺序只能靠运气：
        # 同一批交付物的 created_at 在 MySQL 上曾坍缩到同一秒
        # （DATETIME 默认秒精度，见 base.py 的 TimestampColumn），顺序直接随机
        # （真 MySQL 上 api 套件实测踩到）。时间为主、版本做并列时的次序才确定。
        stmt = stmt.order_by(Artifact.created_at.asc(), Artifact.version.asc()).limit(limit).offset(offset)
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_by_requirement(
        self, requirement_id: UUID, *, artifact_type: ArtifactType | None = None
    ) -> int:
        stmt = select(func.count()).select_from(Artifact).where(Artifact.requirement_id == requirement_id)
        if artifact_type is not None:
            stmt = stmt.where(Artifact.type == artifact_type.value)
        return int((await self.session.execute(stmt)).scalar_one())

    async def next_version(self, requirement_id: UUID, artifact_type: ArtifactType) -> int:
        """同一需求 + 同类型交付物的下一个版本号。

        需求被改动后要重跑 Agent，交付物就该产生新版本而不是覆盖旧的 ——
        否则「这份 PRD 对应的是哪一版需求」就说不清了。
        """
        stmt = select(func.max(Artifact.version)).where(
            Artifact.requirement_id == requirement_id,
            Artifact.type == artifact_type.value,
        )
        current = (await self.session.execute(stmt)).scalar_one_or_none()
        return int(current or 0) + 1

    async def latest(self, requirement_id: UUID, artifact_type: ArtifactType) -> Artifact | None:
        stmt = (
            select(Artifact)
            .where(
                Artifact.requirement_id == requirement_id,
                Artifact.type == artifact_type.value,
            )
            .order_by(Artifact.version.desc())
            .limit(1)
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()
