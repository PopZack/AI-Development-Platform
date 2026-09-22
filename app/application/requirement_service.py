"""需求用例。

Layer: Application（Service）。

status 只能被创建为 DRAFT，之后一律由 Domain 规则 + Workflow Service 推进。
``update_requirement`` 不允许触碰 status，即使未来有人把 status 加进
``RequirementUpdate``，这里也不读它 —— 权限与状态边界要挡在后端，
而不是靠前端不传（文档 §14.1）。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ConflictError, NotFoundError
from app.domain.enums import ProjectStatus, RequirementStatus
from app.domain.requirement import ensure_editable
from app.models.requirement import Requirement
from app.repositories.project_repository import ProjectRepository
from app.repositories.requirement_repository import RequirementRepository
from app.repositories.user_repository import UserRepository
from app.schemas.requirement import RequirementCreate, RequirementUpdate

__all__ = ["RequirementService"]

logger = logging.getLogger(__name__)


class RequirementService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._requirements = RequirementRepository(session)
        self._projects = ProjectRepository(session)
        self._users = UserRepository(session)

    async def create_requirement(self, project_id: UUID, payload: RequirementCreate) -> Requirement:
        project = await self._projects.get(project_id)
        if project is None:
            raise NotFoundError.for_resource("PROJECT", "Project does not exist")
        if project.status != ProjectStatus.ACTIVE:
            raise ConflictError(
                "Cannot add requirements to an inactive project",
                code="PROJECT_NOT_ACTIVE",
                details={"project_status": project.status},
            )

        creator = await self._users.get(payload.created_by)
        if creator is None:
            raise NotFoundError.for_resource("USER", "Requirement creator does not exist")

        requirement = Requirement(
            project_id=project.id,
            title=payload.title.strip(),
            description=payload.description.strip(),
            status=RequirementStatus.DRAFT.value,
            priority=payload.priority.value,
            acceptance_criteria_json=list(payload.acceptance_criteria) or None,
            created_by=creator.id,
            version=1,
        )
        await self._requirements.add(requirement)
        await self._session.commit()
        logger.info("requirement created | id=%s project=%s", requirement.id, project.id)
        return requirement

    async def get_requirement(self, requirement_id: UUID) -> Requirement:
        requirement = await self._requirements.get(requirement_id)
        if requirement is None:
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")
        return requirement

    async def list_requirements(
        self, project_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> tuple[list[Requirement], int]:
        # 先确认项目存在，否则拼错 project_id 会静默返回空列表，排查起来很费劲
        if await self._projects.get(project_id) is None:
            raise NotFoundError.for_resource("PROJECT", "Project does not exist")
        items = await self._requirements.list_by_project(project_id, limit=limit, offset=offset)
        total = await self._requirements.count_by_project(project_id)
        return items, total

    async def update_requirement(self, requirement_id: UUID, payload: RequirementUpdate) -> Requirement:
        requirement = await self.get_requirement(requirement_id)
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

        if touched_versioned_field:
            requirement.version += 1

        await self._session.commit()
        return requirement
