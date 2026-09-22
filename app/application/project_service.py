"""项目用例。

Layer: Application（Service）。

这里能看清「事务边界归 Service」的实际含义：创建项目要写两张表
（projects + project_members），两次 ``add`` 之后只 ``commit`` 一次。
如果第二步失败，第一步也会一起回滚，不会留下一个没有 Owner 的孤儿项目。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError, NotFoundError
from app.common.utils import random_suffix, slugify
from app.domain.enums import ProjectRole, ProjectStatus, UserStatus
from app.models.project import Project, ProjectMember
from app.repositories.project_repository import ProjectMemberRepository, ProjectRepository
from app.repositories.user_repository import UserRepository
from app.schemas.project import ProjectCreate, ProjectMemberCreate, ProjectUpdate

__all__ = ["ProjectService"]

logger = logging.getLogger(__name__)

_SLUG_ATTEMPTS = 5


class ProjectService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._members = ProjectMemberRepository(session)
        self._users = UserRepository(session)

    async def create_project(self, payload: ProjectCreate) -> Project:
        owner = await self._users.get(payload.owner_id)
        if owner is None:
            raise NotFoundError.for_resource("USER", "Project owner does not exist")
        if owner.status != UserStatus.ACTIVE:
            raise ConflictError(
                "Project owner account is not active",
                code="OWNER_NOT_ACTIVE",
                details={"owner_id": str(owner.id), "owner_status": owner.status},
            )

        slug = await self._resolve_slug(payload)

        project = Project(
            name=payload.name.strip(),
            slug=slug,
            description=payload.description,
            owner_id=owner.id,
            status=ProjectStatus.ACTIVE.value,
        )
        await self._projects.add(project)

        # 文档 §3.2 流程 A：创建项目后当前用户成为项目 Owner
        await self._members.add(
            ProjectMember(
                project_id=project.id,
                user_id=owner.id,
                role=ProjectRole.OWNER.value,
            )
        )

        # 唯一的提交点：上面两个 insert 在一个事务里
        await self._session.commit()
        logger.info("project created | id=%s slug=%s owner=%s", project.id, project.slug, owner.id)
        return project

    async def _resolve_slug(self, payload: ProjectCreate) -> str:
        """客户端给了 slug 就必须唯一；没给就按名字生成，冲突时补随机后缀。"""
        if payload.slug:
            if await self._projects.get_by_slug(payload.slug) is not None:
                raise ConflictError(
                    "Project slug already in use",
                    code="PROJECT_SLUG_TAKEN",
                    details={"slug": payload.slug},
                )
            return payload.slug

        base = slugify(payload.name) or "project"
        candidate = base
        for _ in range(_SLUG_ATTEMPTS):
            if await self._projects.get_by_slug(candidate) is None:
                return candidate
            # 中文名 slugify 后可能为空，此时候选值就是 "project-<random>"
            candidate = f"{base}-{random_suffix()}"

        raise ConflictError(
            "Unable to allocate a unique project slug",
            code="PROJECT_SLUG_TAKEN",
            details={"base_slug": base, "attempts": _SLUG_ATTEMPTS},
        )

    async def get_project(self, project_id: UUID) -> Project:
        project = await self._projects.get(project_id)
        if project is None:
            raise NotFoundError.for_resource("PROJECT", "Project does not exist")
        return project

    async def list_projects(
        self, *, owner_id: UUID | None = None, limit: int = 50, offset: int = 0
    ) -> tuple[list[Project], int]:
        """``owner_id`` 传了就按成员关系过滤，不传就列全部。

        Stage 1 靠调用方自觉；Stage 2 起这个参数由 JWT 主体填充，不再可选。
        """
        if owner_id is not None:
            projects = await self._projects.list_for_user(owner_id, limit=limit, offset=offset)
            total = await self._projects.count_for_user(owner_id)
            return projects, total
        projects = await self._projects.list_all(limit=limit, offset=offset)
        total = await self._projects.count_all()
        return projects, total

    async def update_project(self, project_id: UUID, payload: ProjectUpdate) -> Project:
        project = await self.get_project(project_id)
        changes = payload.model_dump(exclude_unset=True)

        # slug 刻意不允许修改：它是对外标识，改了会让已有链接失效
        if changes.get("name") is not None:
            project.name = changes["name"].strip()
        if "description" in changes:
            project.description = changes["description"]
        if changes.get("status") is not None:
            project.status = str(changes["status"])

        await self._session.commit()
        return project

    async def add_member(self, project_id: UUID, payload: ProjectMemberCreate) -> ProjectMember:
        project = await self.get_project(project_id)

        user = await self._users.get(payload.user_id)
        if user is None:
            raise NotFoundError.for_resource("USER", "User to add does not exist")

        if await self._members.get_member(project_id, payload.user_id) is not None:
            raise ConflictError(
                "User is already a member of this project",
                code="PROJECT_MEMBER_EXISTS",
                details={"project_id": str(project_id), "user_id": str(payload.user_id)},
            )

        # 业务规则：OWNER 角色只能落在项目 owner 本人身上，
        # 否则会出现「两个 Owner」或「Owner 不是 owner」这类脏数据
        if payload.role is ProjectRole.OWNER and project.owner_id != payload.user_id:
            raise ConflictError(
                "OWNER role can only be granted to the project owner",
                code="OWNER_ROLE_MISMATCH",
                details={"project_owner_id": str(project.owner_id)},
            )

        member = ProjectMember(
            project_id=project_id,
            user_id=payload.user_id,
            role=payload.role.value,
        )
        await self._members.add(member)
        await self._session.commit()
        return member

    async def list_members(self, project_id: UUID) -> list[ProjectMember]:
        await self.get_project(project_id)
        return await self._members.list_by_project(project_id)

    async def get_membership(self, project_id: UUID, user_id: UUID) -> ProjectMember | None:
        """给 Stage 2 的权限依赖用：非成员返回 None，由上层决定是 403 还是 404。"""
        return await self._members.get_member(project_id, user_id)
