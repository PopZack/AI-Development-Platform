"""项目用例。

Layer: Application（Service）。

两个关键约束，都是这个提交要落实的：

1. **owner 取自 JWT 主体，不接受客户端指定。** 允许客户端传 ``owner_id`` 等于给了
   提权入口：任何登录用户都能创建一个「归属别人」的项目，而文档 §11 Stage 2 的验收项
   「用户不能访问不属于自己的项目」也就永远不可能真正成立。
2. **每个方法都要求显式传入 ``actor``。** 不提供省略身份的重载 —— 让「必须知道
   是谁在操作」在函数签名上就看得见，而不是靠调用方自觉。

事务边界仍然只在这一层：创建项目要写两张表（projects + project_members），
两次 ``add`` 之后只 ``commit`` 一次，避免留下没有 Owner 的孤儿项目。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.project_access import ProjectAccessGuard
from app.common.exceptions import ConflictError, NotFoundError
from app.common.utils import random_suffix, slugify
from app.domain.enums import ProjectRole, ProjectStatus
from app.domain.project import MANAGE_ROLES, READ_ROLES
from app.models.project import Project, ProjectMember
from app.models.user import User
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
        self._access = ProjectAccessGuard(session)

    # ------------------------------------------------------------------ 创建

    async def create_project(self, payload: ProjectCreate, *, actor: User) -> Project:
        slug = await self._resolve_slug(payload)

        # owner 只可能来自令牌主体。注意 payload 里已经没有 owner_id 这个字段了 ——
        # 不是「忽略客户端传的值」，而是根本没有这个入口
        project = Project(
            name=payload.name.strip(),
            slug=slug,
            description=payload.description,
            owner_id=actor.id,
            status=ProjectStatus.ACTIVE.value,
        )
        await self._projects.add(project)

        # 文档 §3.2 流程 A：创建项目后当前用户成为项目 Owner。
        # 这一行和上面共用同一个事务 —— 少了它，创建者反而进不去自己的项目
        await self._members.add(
            ProjectMember(
                project_id=project.id,
                user_id=actor.id,
                role=ProjectRole.OWNER.value,
            )
        )

        await self._session.commit()
        logger.info("project created | id=%s slug=%s owner=%s", project.id, project.slug, actor.id)
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

    # ------------------------------------------------------------------ 读取

    async def get_project(self, project_id: UUID, *, actor: User) -> Project:
        project = await self._access.load_project(project_id)
        await self._access.require(project, actor, READ_ROLES, action="read this project")
        return project

    async def list_projects(
        self, *, actor: User, limit: int = 50, offset: int = 0
    ) -> tuple[list[Project], int]:
        """只返回 ``actor`` 参与的项目。

        Stage 1 的 ``owner_id`` 查询参数已删除 —— 那个参数让调用方可以指定「看谁的
        项目列表」，而列表内容的可见范围不该由请求参数决定。现在是登录即可，
        但范围内的项目永远只可能是自己参与的。
        """
        projects = await self._projects.list_for_user(actor.id, limit=limit, offset=offset)
        total = await self._projects.count_for_user(actor.id)
        return projects, total

    # ------------------------------------------------------------------ 修改

    async def update_project(self, project_id: UUID, payload: ProjectUpdate, *, actor: User) -> Project:
        project = await self._access.load_project(project_id)
        await self._access.require(project, actor, MANAGE_ROLES, action="update this project")

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

    # ------------------------------------------------------------------ 成员

    async def add_member(
        self, project_id: UUID, payload: ProjectMemberCreate, *, actor: User
    ) -> ProjectMember:
        project = await self._access.load_project(project_id)
        await self._access.require(project, actor, MANAGE_ROLES, action="manage members")

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
        logger.info("member added | project=%s user=%s role=%s", project_id, payload.user_id, payload.role)
        return member

    async def list_members(self, project_id: UUID, *, actor: User) -> list[ProjectMember]:
        project = await self._access.load_project(project_id)
        await self._access.require(project, actor, READ_ROLES, action="read project members")
        return await self._members.list_by_project(project_id)
