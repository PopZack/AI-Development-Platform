"""项目与项目成员仓储。

Layer: Repository。

注意 ``list_for_user``：Stage 1 没有权限系统，所以列表按「是否指定 user_id」
分流；Stage 2 引入 JWT 后，这个方法的入参将固定为当前登录用户，
查询逻辑本身不用改 —— 这正是把「按人过滤」放进 Repository 而不是 Router 的收益。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import func, select

from app.models.project import Project, ProjectMember
from app.repositories.base import BaseRepository

__all__ = ["ProjectRepository", "ProjectMemberRepository"]


class ProjectRepository(BaseRepository[Project]):
    model = Project

    async def get_by_slug(self, slug: str) -> Project | None:
        stmt = select(Project).where(Project.slug == slug)
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_for_user(self, user_id: UUID, *, limit: int = 50, offset: int = 0) -> list[Project]:
        stmt = (
            select(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == user_id)
            .order_by(Project.created_at.desc())
            .limit(limit)
            .offset(offset)
        )
        return list((await self.session.execute(stmt)).scalars().all())

    async def count_for_user(self, user_id: UUID) -> int:
        stmt = (
            select(func.count())
            .select_from(Project)
            .join(ProjectMember, ProjectMember.project_id == Project.id)
            .where(ProjectMember.user_id == user_id)
        )
        return int((await self.session.execute(stmt)).scalar_one())


class ProjectMemberRepository(BaseRepository[ProjectMember]):
    model = ProjectMember

    async def get_member(self, project_id: UUID, user_id: UUID) -> ProjectMember | None:
        """签名与文档 §9.1 的示例保持一致：``get_member(project_id=..., user_id=...)``。"""
        stmt = select(ProjectMember).where(
            ProjectMember.project_id == project_id,
            ProjectMember.user_id == user_id,
        )
        return (await self.session.execute(stmt)).scalar_one_or_none()

    async def list_by_project(self, project_id: UUID) -> list[ProjectMember]:
        stmt = (
            select(ProjectMember)
            .where(ProjectMember.project_id == project_id)
            .order_by(ProjectMember.created_at.asc())
        )
        return list((await self.session.execute(stmt)).scalars().all())
