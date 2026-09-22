"""项目与项目成员仓储。

Layer: Repository。

``list_for_user`` 是项目列表的**唯一**入口：Stage 1 它是个可选的过滤开关，
Stage 2 起它的入参固定为当前登录用户。查询逻辑一行没改 —— 这正是当初把
「按人过滤」放进 Repository 而不是 Router 的收益。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import func, select

from app.models.project import Project, ProjectMember
from app.repositories.base import BaseRepository

__all__ = ["ProjectRepository", "ProjectMemberRepository"]

_UNSCOPED_LISTING_ERROR = (
    "ProjectRepository.{name}() is intentionally disabled: project listing must always be "
    "scoped by membership. Use list_for_user(user_id) / count_for_user(user_id) instead."
)


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

    # 继承来的 list_all() / count_all() 会返回**全部**项目，绕过成员过滤。
    # 这两个方法在本类上没有合法用途，所以直接堵死而不是留一句注释 ——
    # 注释拦不住下一个人顺手调用它，而 Stage 2 的核心验收项正是
    # 「用户不能访问不属于自己的项目」。
    async def list_all(self, **kwargs: Any) -> list[Project]:
        raise NotImplementedError(_UNSCOPED_LISTING_ERROR.format(name="list_all"))

    async def count_all(self, **kwargs: Any) -> int:
        raise NotImplementedError(_UNSCOPED_LISTING_ERROR.format(name="count_all"))


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
