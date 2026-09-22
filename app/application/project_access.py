"""项目级权限闸门。

Layer: Application（Service 的共用协作对象）。

**为什么权限检查放在这里，而不是像文档 §9.1 那样写成 FastAPI 依赖？**

1. ``/requirements/{requirement_id}`` 这类路径里没有 ``project_id``，依赖必须先把
   requirement 查出来才知道该校验哪个项目 —— 那就等于把业务查询塞进依赖层；
   放在这里，一次查询就能同时拿到「资源 + 成员关系」。
2. 角色规则是业务规则（``OWNER 才能改项目``、``VIEWER 不能写``），属于
   application/domain 的判断。塞进依赖会让依赖层长出业务逻辑，和文档 §5.2
   给各层划的边界打架。
3. 可以直接单测：Service 方法带 ``actor`` 参数，不用起 HTTP 就能验完整权限矩阵。

Router 依然只做「收请求 → 传 current_user → 转 Service」。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import AuthorizationError, NotFoundError
from app.domain.enums import ProjectRole
from app.domain.project import ensure_role_allowed
from app.models.project import Project, ProjectMember
from app.models.user import User
from app.repositories.project_repository import ProjectMemberRepository, ProjectRepository

__all__ = ["ProjectAccessGuard"]


class ProjectAccessGuard:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._projects = ProjectRepository(session)
        self._members = ProjectMemberRepository(session)

    async def load_project(self, project_id: UUID) -> Project:
        project = await self._projects.get(project_id)
        if project is None:
            raise NotFoundError.for_resource("PROJECT", "Project does not exist")
        return project

    async def require(
        self,
        project: Project,
        actor: User,
        allowed: frozenset[ProjectRole],
        *,
        action: str,
    ) -> ProjectMember:
        """确认 ``actor`` 是 ``project`` 的成员，且角色在 ``allowed`` 内。

        两种越权刻意给不同的 code：

        - 不是成员 → ``PROJECT_ACCESS_DENIED``
        - 是成员但角色不够 → ``PROJECT_ROLE_REQUIRED``（details 里带上实际角色和所需角色）

        对客户端来说都是 403，但日志里能立刻分辨「外部人在敲门」还是
        「成员手伸太长」，这两件事的处置方式完全不同。

        返回成员行本身：调用方常常还要用它的 role，避免再查一次。
        """
        member = await self._members.get_member(project.id, actor.id)
        if member is None:
            raise AuthorizationError(
                "You are not a member of this project",
                code="PROJECT_ACCESS_DENIED",
                details={"project_id": str(project.id)},
            )

        try:
            project_role = ProjectRole(member.role)
        except ValueError:
            # 库里存了未知角色（脏数据 / 手工改过库）。宁可拒绝也不能放行：
            # 放行的默认值就等于把权限判给了所有人。
            raise AuthorizationError(
                "Your project role is not recognized",
                code="PROJECT_ROLE_UNKNOWN",
                details={"project_id": str(project.id), "role": member.role},
            ) from None

        ensure_role_allowed(project_role, allowed, action=action, project_id=project.id)
        return member
