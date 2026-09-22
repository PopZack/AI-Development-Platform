"""工作流用例。

Layer: Application（Service）。

这个提交只做**创建工作流 + 幂等键**。状态机的实际推进（start/pause/resume/cancel）
属于 Stage 4 —— 那时候才有 Agent 可以跑，现在把它们加进来只会是一堆空转接口。

幂等键的语义（文档 §12.2）：

    同一个 ``Idempotency-Key`` + 同一份需求  → 返回**已存在的那条**工作流（不新建）
    同一个 ``Idempotency-Key`` + 另一份需求  → 409，这是客户端 bug，不是重试
    没有用过的键                            → 新建

关键点：**应用层的「先查再插」在并发下是不成立的**。两个请求可能同时查到「不存在」，
然后同时插入。真正兜底的是 ``idempotency_key`` 上的 UNIQUE 约束 —— 所以下面会捕获
``IntegrityError`` 并从库里把已存在的那条读回来，而不是让它变成 500。
"""

from __future__ import annotations

import logging
from uuid import UUID

from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.project_access import ProjectAccessGuard
from app.common.exceptions import ConflictError, NotFoundError
from app.domain.enums import RequirementStatus, WorkflowStatus, WorkflowStep
from app.domain.project import READ_ROLES, WRITE_ROLES
from app.domain.requirement import ensure_runnable
from app.models.user import User
from app.models.workflow import WorkflowRun
from app.repositories.requirement_repository import RequirementRepository
from app.repositories.workflow_repository import WorkflowRunRepository

__all__ = ["WorkflowService"]

logger = logging.getLogger(__name__)


class WorkflowService:
    def __init__(self, session: AsyncSession) -> None:
        self._session = session
        self._workflows = WorkflowRunRepository(session)
        self._requirements = RequirementRepository(session)
        self._access = ProjectAccessGuard(session)

    # ------------------------------------------------------------------ 创建

    async def create_run(
        self, requirement_id: UUID, idempotency_key: str, *, actor: User
    ) -> tuple[WorkflowRun, bool]:
        """返回 ``(run, created)``。``created=False`` 表示命中了幂等重放。

        Router 据此区分 201 与 200，并在重放时回一个 ``Idempotent-Replay: true``
        响应头 —— 否则调用方无法判断自己拿到的是新建结果还是历史结果。
        """
        requirement = await self._requirements.get(requirement_id)
        if requirement is None:
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")

        # 提前把 id 取成普通值。下面的 rollback() 之后 ORM 对象会被**无条件过期**
        # （expire_on_commit=False 只管 commit，不管 rollback），此后再碰
        # requirement.id 就会触发同步懒加载，在 async 上下文里直接炸成
        # MissingGreenlet —— 而且只在回滚这条罕见路径上炸，正常流程永远看不到。
        req_id = requirement.id

        project = await self._access.load_project(requirement.project_id)
        await self._access.require(project, actor, WRITE_ROLES, action="start a workflow run on")

        # 幂等检查刻意排在「需求状态校验」之前：重试一次**已经成功过**的调用，
        # 应该拿回当初的结果，哪怕这段时间里需求已经被改成 COMPLETED。
        # 反过来的话，客户端会因为「重试变得不可幂等」而收到莫名其妙的 409。
        existing = await self._workflows.get_by_idempotency_key(idempotency_key)
        if existing is not None:
            return self._resolve_replay(existing, req_id), False

        ensure_runnable(RequirementStatus(requirement.status))

        run = WorkflowRun(
            requirement_id=req_id,
            # 只创建、不执行，所以停在 CREATED / PENDING。真正推进到 RUNNING
            # 是 Stage 4 的 Workflow Service 的事
            status=WorkflowStatus.CREATED.value,
            current_step=WorkflowStep.PENDING.value,
            idempotency_key=idempotency_key,
        )

        try:
            await self._workflows.add(run)
            await self._session.commit()
        except IntegrityError:
            # 并发兜底：另一个请求抢在查重之后、插入之前写进了同一个键。
            # 回滚后把已存在的那条读出来按重放返回，而不是把 500 抛给客户端。
            await self._session.rollback()
            winner = await self._workflows.get_by_idempotency_key(idempotency_key)
            if winner is None:
                # 不是幂等键冲突（比如外键违规），原样抛出，别把真正的问题藏起来
                raise
            logger.info("idempotent replay resolved by unique constraint | key=%s", idempotency_key)
            return self._resolve_replay(winner, req_id), False

        logger.info("workflow run created | id=%s requirement=%s key=%s", run.id, req_id, idempotency_key)
        return run, True

    @staticmethod
    def _resolve_replay(existing: WorkflowRun, requirement_id: UUID) -> WorkflowRun:
        """命中已存在的幂等键：判断它是不是同一份需求的。

        刻意收 ``requirement_id`` 而不是 ORM 对象 —— 调用点之一在 rollback 之后，
        那里任何对 ORM 属性的访问都会触发懒加载。
        """
        if existing.requirement_id != requirement_id:
            raise ConflictError(
                "This Idempotency-Key has already been used for a different requirement",
                code="IDEMPOTENCY_KEY_CONFLICT",
                details={
                    "idempotency_key": existing.idempotency_key,
                    "used_by_requirement_id": str(existing.requirement_id),
                },
            )
        return existing

    # ------------------------------------------------------------------ 读取

    async def get_run(self, run_id: UUID, *, actor: User) -> WorkflowRun:
        run = await self._workflows.get(run_id)
        if run is None:
            raise NotFoundError.for_resource("WORKFLOW_RUN", "Workflow run does not exist")

        # 工作流没有 project_id，权限要顺着 requirement 往上找 —— 和读需求是同一条路径
        requirement = await self._requirements.get(run.requirement_id)
        if requirement is None:
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")

        project = await self._access.load_project(requirement.project_id)
        await self._access.require(project, actor, READ_ROLES, action="read this workflow run")
        return run
