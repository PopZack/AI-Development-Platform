"""审批用例。

Layer: Application（Service）。

## 谁能批？

**只有项目 OWNER 能批。** 这是刻意的职责分离：

- Developer 可以触发 Agent（也就可能触发 L4 工具），但**不能批自己的请求** ——
  「我自己申请、我自己批准」等于没有审批
- VIEWER 连触发都不行

申请与批准由两个不同角色完成，审批才有意义。

## 过期是惰性判定

``PENDING`` 且 ``expires_at`` 已过的审批，在**被读取或被审批时**才落成
``EXPIRED``（见 :mod:`app.domain.approval`）。这避免为一个低频事件跑
常驻定时任务 —— 代价是「过期」这个事实要等到有人去看才会被写进库，
我们认为这个代价可以接受。
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.application.project_access import ProjectAccessGuard
from app.common.exceptions import NotFoundError
from app.domain.approval import effective_status, ensure_pending
from app.domain.enums import ApprovalStatus, ProjectRole
from app.models.approval import Approval
from app.models.user import User
from app.repositories.approval_repository import ApprovalRepository
from app.repositories.requirement_repository import RequirementRepository

__all__ = ["ApprovalService"]

logger = logging.getLogger(__name__)


class ApprovalService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._approvals = ApprovalRepository(session)
        self._requirements = RequirementRepository(session)
        self._access = ProjectAccessGuard(session)

    # ------------------------------------------------------------------ 读取

    async def get(self, approval_id: UUID, *, actor: User) -> Approval:
        approval = await self._load(approval_id)
        await self._require_read(approval, actor)
        await self._refresh_expiry(approval)
        return approval

    async def list_for_requirement(
        self,
        requirement_id: UUID,
        *,
        actor: User,
        status: ApprovalStatus | None = None,
    ) -> tuple[list[Approval], int]:
        requirement = await self._requirements.get(requirement_id)
        if requirement is None:
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")

        project = await self._access.load_project(requirement.project_id)
        await self._access.require(
            project,
            actor,
            {ProjectRole.OWNER, ProjectRole.DEVELOPER, ProjectRole.VIEWER},
            action="read the approvals of",
        )

        items = await self._approvals.list_by_requirement(requirement_id, status=status)
        # 逐条做惰性过期检查（量小：一个需求的审批通常个位数）
        for item in items:
            await self._refresh_expiry(item)
        return items, len(items)

    # ------------------------------------------------------------------ 审批

    async def approve(self, approval_id: UUID, *, actor: User, note: str | None = None) -> Approval:
        return await self._decide(approval_id, actor=actor, note=note, target=ApprovalStatus.APPROVED)

    async def reject(self, approval_id: UUID, *, actor: User, note: str | None = None) -> Approval:
        return await self._decide(approval_id, actor=actor, note=note, target=ApprovalStatus.REJECTED)

    async def _decide(
        self,
        approval_id: UUID,
        *,
        actor: User,
        note: str | None,
        target: ApprovalStatus,
    ) -> Approval:
        approval = await self._load(approval_id)
        await self._require_owner(approval, actor, action=f"{target.value.lower()} this approval")

        # 过期检查：先落成 EXPIRED 再拒绝审批，库里反映的是真实状态
        actual = ensure_pending(ApprovalStatus(approval.status), approval.expires_at, approval_id=approval.id)
        if actual is not ApprovalStatus.PENDING:
            approval.status = actual.value
            await self._session.commit()

        approval.status = target.value
        approval.reviewed_by = actor.id
        approval.review_note = note
        await self._session.commit()

        logger.info(
            "approval decided | approval=%s tool=%s decision=%s by=%s",
            approval.id,
            approval.tool_name,
            target.value,
            actor.id,
        )
        return approval

    # ------------------------------------------------------------------ 内部

    async def _load(self, approval_id: UUID) -> Approval:
        approval = await self._approvals.get(approval_id)
        if approval is None:
            raise NotFoundError.for_resource("APPROVAL", "Approval does not exist")
        return approval

    async def _refresh_expiry(self, approval: Approval) -> None:
        actual = effective_status(ApprovalStatus(approval.status), approval.expires_at)
        if actual is not ApprovalStatus(approval.status):
            approval.status = actual.value
            await self._session.commit()

    async def _require_read(self, approval: Approval, actor: User) -> None:
        requirement = await self._requirements.get(approval.requirement_id)
        if requirement is None:  # pragma: no cover - 级联删除下不会发生
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")
        project = await self._access.load_project(requirement.project_id)
        await self._access.require(
            project,
            actor,
            {ProjectRole.OWNER, ProjectRole.DEVELOPER, ProjectRole.VIEWER},
            action="read this approval",
        )

    async def _require_owner(self, approval: Approval, actor: User, *, action: str) -> None:
        requirement = await self._requirements.get(approval.requirement_id)
        if requirement is None:  # pragma: no cover - 级联删除下不会发生
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")
        project = await self._access.load_project(requirement.project_id)
        # 刻意只给 OWNER：Developer 不能批自己触发的请求 —— 职责分离
        await self._access.require(project, actor, {ProjectRole.OWNER}, action=action)


def _utcnow() -> datetime:
    return datetime.now(UTC)
