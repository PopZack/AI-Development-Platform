"""需求用例。

Layer: Application（Service）。

权限边界（承自项目成员角色，见 app/domain/project.py）：

- 读需求：``READ_ROLES``（含只读的 VIEWER）
- 建/改需求：``WRITE_ROLES``（VIEWER 会被拒）

``created_by`` 取自 JWT 主体，请求体里没有这个字段 —— 允许客户端指定创建人
等于允许冒名。

status 一如既往不可被客户端修改：``RequirementUpdate`` 里没有这个字段，且即使
有人把它加进去，这里也不会读它。状态只能由 Domain 规则推进（文档 §3.3）。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.project_access import ProjectAccessGuard
from app.common.exceptions import ConflictError, NotFoundError
from app.domain.enums import ProjectStatus, RequirementStatus
from app.domain.project import READ_ROLES, WRITE_ROLES
from app.domain.requirement import ensure_editable
from app.models.requirement import Requirement
from app.models.user import User
from app.repositories.requirement_repository import RequirementRepository
from app.schemas.requirement import RequirementCreate, RequirementUpdate

__all__ = ["RequirementService"]

logger = logging.getLogger(__name__)


class RequirementService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requirements = RequirementRepository(session)
        self._access = ProjectAccessGuard(session)

    # ------------------------------------------------------------------ 创建

    async def create_requirement(
        self, project_id: UUID, payload: RequirementCreate, *, actor: User
    ) -> Requirement:
        project = await self._access.load_project(project_id)
        await self._access.require(project, actor, WRITE_ROLES, action="create requirements")

        if project.status != ProjectStatus.ACTIVE:
            raise ConflictError(
                "Cannot add requirements to an inactive project",
                code="PROJECT_NOT_ACTIVE",
                details={"project_status": project.status},
            )

        requirement = Requirement(
            project_id=project.id,
            title=payload.title.strip(),
            description=payload.description.strip(),
            status=RequirementStatus.DRAFT.value,
            priority=payload.priority.value,
            acceptance_criteria_json=list(payload.acceptance_criteria) or None,
            created_by=actor.id,
            version=1,
        )
        await self._requirements.add(requirement)
        await self._session.commit()
        logger.info("requirement created | id=%s project=%s by=%s", requirement.id, project.id, actor.id)
        return requirement

    # ------------------------------------------------------------------ 读取

    async def get_requirement(self, requirement_id: UUID, *, actor: User) -> Requirement:
        """先取需求，再用它所属的项目做权限判断。

        这里就是「权限检查不适合写成依赖」的具体原因：路径里只有 requirement_id，
        依赖想校验成员关系就必须先把需求查出来，那等于把业务查询塞进依赖层。
        """
        requirement = await self._requirements.get(requirement_id)
        if requirement is None:
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")

        project = await self._access.load_project(requirement.project_id)
        await self._access.require(project, actor, READ_ROLES, action="read this requirement")
        return requirement

    async def list_requirements(
        self, project_id: UUID, *, actor: User, limit: int = 50, offset: int = 0
    ) -> tuple[list[Requirement], int]:
        project = await self._access.load_project(project_id)
        await self._access.require(project, actor, READ_ROLES, action="read requirements")

        items = await self._requirements.list_by_project(project_id, limit=limit, offset=offset)
        total = await self._requirements.count_by_project(project_id)
        return items, total

    # ------------------------------------------------------------------ 修改

    async def update_requirement(
        self, requirement_id: UUID, payload: RequirementUpdate, *, actor: User
    ) -> Requirement:
        requirement = await self._requirements.get(requirement_id)
        if requirement is None:
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")

        project = await self._access.load_project(requirement.project_id)
        await self._access.require(project, actor, WRITE_ROLES, action="update this requirement")

        ensure_editable(RequirementStatus(requirement.status))

        changes = payload.model_dump(exclude_unset=True)
        touched_versioned_field = False

        if changes.get("title") is not None:
            requirement.title = changes["title"].strip()
            touched_versioned_field = True
        if changes.get("description") is not None:
            requirement.description = changes["description"].strip()
            touched_versioned_field = True
        if changes.get("priority") is not None:
            requirement.priority = str(changes["priority"])
            touched_versioned_field = True
        if "acceptance_criteria" in changes and changes["acceptance_criteria"] is not None:
            requirement.acceptance_criteria_json = list(changes["acceptance_criteria"]) or None
            touched_versioned_field = True

        # 内容变了就让 version 自增：已生成的 PRD / 架构据此判断是否已过期
        if touched_versioned_field:
            requirement.version += 1

        await self._session.commit()
        return requirement
