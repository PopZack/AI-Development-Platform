"""工作流执行编排：把 §3.3 那台状态机真正跑起来。

Layer: Application（Service）。

## 「暂停」的表达：status + current_step 二元组

文档 §6.3 给 workflow_runs 同时定义了 status 与 current_step（都是 NOT NULL 但
没说区别）。本项目的划分：status = 对外阶段，current_step = 阶段内环节。
这个划分在这里第一次真正发挥作用 —— **两次暂停都不是新状态，而是一个合法
状态 + 一个特定步骤**：

    补丁审批暂停   → (IMPLEMENTING, TOOL_GATEWAY)    审批记录已由 Gateway 自动创建
    最终审批暂停   → (WAITING_APPROVAL, APPROVAL)     文档状态机的原生停点

为什么不给「等补丁审批」也用 WAITING_APPROVAL？因为 §3.3 的迁移表只允许
REVIEWING → WAITING_APPROVAL。与其为了方便给状态机开后门，
不如用 current_step 表达「阶段内卡在哪」—— 这本来就是它存在的意义。

## 主流程

    start:  CREATED → RUNNING → ANALYZING → PLANNING → IMPLEMENTING
            Developer 出变更 → L2 生成 diff（PATCH artifact）→ 尝试 L4 写盘
            → 被审批拦下 → 暂停在 (IMPLEMENTING, TOOL_GATEWAY)
    resume: OWNER 批准后带 approval_id → 写盘 → TESTING → REVIEWING
            → 通过 → (WAITING_APPROVAL, APPROVAL) 暂停
            → 驳回 → REJECTED
    approve/reject: 最终人工决定 → COMPLETED / 回到实现

REJECTED → REVISION_REQUIRED → IMPLEMENTING 的修订回路也在这里闭环。

## 事务边界

延续 AgentService 的纪律（文档 §5.3）：每次状态提交都 commit 释放事务，
模型调用发生时不持有写事务。审批的创建与消费在 Tool Gateway 自己的事务里。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.roles import (
    DEVELOPER_SPEC,
    REVIEWER_SPEC,
    TESTER_SPEC,
    build_developer_user_prompt,
    build_reviewer_user_prompt,
    build_tester_user_prompt,
)
from app.agent.runtime import AgentContext, AgentRuntime
from app.application.agent_service import AgentService
from app.application.project_access import ProjectAccessGuard
from app.common.exceptions import AppError, ConflictError, NotFoundError
from app.domain.enums import (
    AgentRole,
    ArtifactType,
    ProjectRole,
    RequirementStatus,
    WorkflowStatus,
    WorkflowStep,
)
from app.domain.project import READ_ROLES, WRITE_ROLES
from app.domain.workflow import can_transition, ensure_transition
from app.infrastructure.llm.base import LLMProvider
from app.infrastructure.tools import ToolContext, ToolGateway
from app.models.agent import Artifact
from app.models.requirement import Requirement
from app.models.user import User
from app.models.workflow import WorkflowRun
from app.repositories.agent_repository import ArtifactRepository
from app.repositories.requirement_repository import RequirementRepository
from app.repositories.workflow_repository import WorkflowRunRepository

__all__ = ["RunOutcome", "WorkflowOrchestrator"]

logger = logging.getLogger(__name__)

# 状态机没有环能超过这个长度（最长的合法路径约 8 步）。
# 这个护栏不为防设计，为防 bug：步骤推进写错时快速失败，而不是原地打转。
_MAX_ADVANCE_ITERATIONS = 12


@dataclass
class RunOutcome:
    """一次 start/resume 的结果。

    ``paused=True`` 时 ``pause_reason`` 说明在等什么：
    ``tool_approval`` 等补丁写盘审批（approval_id 直接给出）；
    ``final_approval`` 等最终人工决定（走 /approve 或 /reject）。
    """

    run: WorkflowRun
    paused: bool = False
    pause_reason: str | None = None
    approval_id: UUID | None = None


class WorkflowOrchestrator:
    def __init__(
        self,
        session: AsyncSession,
        provider: LLMProvider,
        *,
        workspace_root: Path,
        max_output_attempts: int = 2,
    ) -> None:
        self._session = session
        self._agents = AgentService(session, provider, max_output_attempts=max_output_attempts)
        self._runtime = AgentRuntime(session, provider, max_output_attempts=max_output_attempts)
        self._workflows = WorkflowRunRepository(session)
        self._requirements = RequirementRepository(session)
        self._artifacts = ArtifactRepository(session)
        self._access = ProjectAccessGuard(session)
        self._provider = provider
        self._workspace_root = workspace_root

    # ------------------------------------------------------------------ 启动

    async def start(self, run_id: UUID, *, actor: User) -> RunOutcome:
        run, requirement = await self._load(run_id, actor, WRITE_ROLES, "start")

        if WorkflowStatus(run.status) is not WorkflowStatus.CREATED:
            raise ConflictError(
                "Workflow run has already been started",
                code="WORKFLOW_ALREADY_STARTED",
                details={"current_status": run.status, "current_step": run.current_step},
            )
        # 全流程从 DRAFT 起跑：analyze/plan 是流程的前两步，DESIGNED 的需求
        # 说明它们已经做过 —— 走 /analyze /plan 单独接口或重开一份需求
        if RequirementStatus(requirement.status) is not RequirementStatus.DRAFT:
            raise ConflictError(
                "Workflow runs execute the full flow; the requirement must still be DRAFT",
                code="REQUIREMENT_NOT_DRAFT",
                details={"requirement_status": requirement.status},
            )

        self._transition(run, WorkflowStatus.RUNNING)
        run.started_at = datetime.now(UTC)
        run.current_step = WorkflowStep.PRODUCT_AGENT.value
        await self._session.commit()
        logger.info("workflow run started | run=%s requirement=%s", run.id, run.requirement_id)

        return await self._advance(run, actor)

    # ------------------------------------------------------------------ 恢复

    async def resume(self, run_id: UUID, *, actor: User, approval_id: UUID | None = None) -> RunOutcome:
        run, _requirement = await self._load(run_id, actor, WRITE_ROLES, "resume")
        status = WorkflowStatus(run.status)
        step = WorkflowStep(run.current_step)

        if (status, step) == (WorkflowStatus.WAITING_APPROVAL, WorkflowStep.APPROVAL):
            raise ConflictError(
                "This run is waiting for the final decision; use /approve or /reject",
                code="WORKFLOW_FINAL_APPROVAL_REQUIRED",
            )
        if (status, step) == (WorkflowStatus.IMPLEMENTING, WorkflowStep.TOOL_GATEWAY):
            # 补丁审批暂停点：必须带上 Gateway 返回的 approval_id
            if approval_id is None:
                raise ConflictError(
                    "approval_id is required to resume a run paused for patch approval",
                    code="APPROVAL_ID_REQUIRED",
                    details={"hint": "GET /requirements/{id}/approvals?status=PENDING"},
                )
            outcome = await self._apply_and_continue(run, actor, approval_id)
            if outcome is not None:
                return outcome
            return await self._advance(run, actor)

        if status in (WorkflowStatus.REJECTED, WorkflowStatus.REVISION_REQUIRED):
            # 被最终驳回 / 审查要求返工：回到实现阶段重新出补丁
            return await self._advance(run, actor)

        raise ConflictError(
            "Workflow run is not resumable in its current state",
            code="WORKFLOW_NOT_RESUMABLE",
            details={"current_status": run.status, "current_step": run.current_step},
        )

    # ------------------------------------------------------------------ 决定

    async def approve(self, run_id: UUID, *, actor: User) -> WorkflowRun:
        """最终人工批准。只有项目 OWNER —— 理由同工具审批：不能自己批自己。"""
        run, requirement = await self._load(run_id, actor, frozenset({ProjectRole.OWNER}), "approve")

        self._expect(run, WorkflowStatus.WAITING_APPROVAL, WorkflowStep.APPROVAL)
        self._transition(run, WorkflowStatus.APPROVED)
        self._transition(run, WorkflowStatus.COMPLETED)
        run.current_step = WorkflowStep.DONE.value
        run.finished_at = datetime.now(UTC)
        self._set_requirement_status(requirement, RequirementStatus.COMPLETED)
        await self._session.commit()
        logger.info("workflow run completed | run=%s", run.id)
        return run

    async def reject(self, run_id: UUID, *, actor: User) -> WorkflowRun:
        """最终驳回。走 REJECTED →（resume）→ REVISION_REQUIRED → 重新实现。"""
        run, _requirement = await self._load(run_id, actor, frozenset({ProjectRole.OWNER}), "reject")

        self._expect(run, WorkflowStatus.WAITING_APPROVAL, WorkflowStep.APPROVAL)
        self._transition(run, WorkflowStatus.REJECTED)
        run.current_step = WorkflowStep.DEVELOPER_AGENT.value
        await self._session.commit()
        logger.info("workflow run rejected | run=%s", run.id)
        return run

    async def cancel(self, run_id: UUID, *, actor: User) -> WorkflowRun:
        run, requirement = await self._load(run_id, actor, WRITE_ROLES, "cancel")

        # 合法性交给状态机：APPROVED / 终态都会在这里被拒
        self._transition(run, WorkflowStatus.CANCELLED)
        run.finished_at = datetime.now(UTC)
        # 需求侧能不能跟着取消，先查再动 —— 不要靠 rollback 收场：
        # rollback 会无条件过期会话里所有 ORM 对象，之后任何属性访问都会炸
        if can_transition(RequirementStatus(requirement.status), RequirementStatus.CANCELLED):
            requirement.status = RequirementStatus.CANCELLED.value
        await self._session.commit()
        logger.info("workflow run cancelled | run=%s", run.id)
        return run

    # ------------------------------------------------------------------ 读取

    async def list_artifacts(self, run_id: UUID, *, actor: User) -> tuple[list[Artifact], int]:
        """这条工作流产出的交付物 = 其需求名下的全部交付物。"""
        run, _requirement = await self._load(run_id, actor, READ_ROLES, "read the artifacts of")
        items = await self._artifacts.list_by_requirement(run.requirement_id)
        total = await self._artifacts.count_by_requirement(run.requirement_id)
        return items, total

    # ------------------------------------------------------------------ 主循环

    async def _advance(self, run: WorkflowRun, actor: User) -> RunOutcome:
        for _iteration in range(_MAX_ADVANCE_ITERATIONS):
            status = WorkflowStatus(run.status)
            step = WorkflowStep(run.current_step)

            if (status, step) == (WorkflowStatus.IMPLEMENTING, WorkflowStep.TOOL_GATEWAY):
                return RunOutcome(run, paused=True, pause_reason="tool_approval")

            if (status, step) == (WorkflowStatus.WAITING_APPROVAL, WorkflowStep.APPROVAL):
                return RunOutcome(run, paused=True, pause_reason="final_approval")

            if (status, step) == (WorkflowStatus.RUNNING, WorkflowStep.PRODUCT_AGENT):
                self._transition(run, WorkflowStatus.ANALYZING)
                await self._session.commit()
                await self._run_agent_step(
                    lambda: self._agents.analyze_requirement(run.requirement_id, actor=actor),
                    run=run,
                    phase="analyze",
                )
                self._transition(run, WorkflowStatus.PLANNING)
                run.current_step = WorkflowStep.ARCHITECT_AGENT.value
                await self._session.commit()
                continue

            if (status, step) == (WorkflowStatus.PLANNING, WorkflowStep.ARCHITECT_AGENT):
                await self._run_agent_step(
                    lambda: self._agents.plan_requirement(run.requirement_id, actor=actor),
                    run=run,
                    phase="plan",
                )
                self._transition(run, WorkflowStatus.IMPLEMENTING)
                run.current_step = WorkflowStep.DEVELOPER_AGENT.value
                await self._session.commit()
                continue

            if step is WorkflowStep.DEVELOPER_AGENT and status in (
                WorkflowStatus.IMPLEMENTING,
                WorkflowStatus.REVISION_REQUIRED,
                WorkflowStatus.REJECTED,
            ):
                if status is WorkflowStatus.REJECTED:
                    self._transition(run, WorkflowStatus.REVISION_REQUIRED)
                    await self._session.commit()
                if WorkflowStatus(run.status) is WorkflowStatus.REVISION_REQUIRED:
                    self._transition(run, WorkflowStatus.IMPLEMENTING)
                    await self._session.commit()
                paused = await self._develop(run, actor)
                if paused is not None:
                    return paused
                continue

            if (status, step) == (WorkflowStatus.TESTING, WorkflowStep.TESTER_AGENT):
                await self._test(run, actor)
                continue

            if (status, step) == (WorkflowStatus.REVIEWING, WorkflowStep.REVIEWER_AGENT):
                approved = await self._review(run, actor)
                if not approved:
                    continue
                return RunOutcome(run, paused=True, pause_reason="final_approval")

            raise AppError(
                f"No handler for workflow state ({status}, {step})",
                code="WORKFLOW_STATE_UNHANDLED",
                status_code=500,
                details={"status": str(status), "step": str(step)},
            )

        raise AppError(
            "Workflow exceeded the maximum number of advance iterations",
            code="WORKFLOW_LOOP_GUARD",
            status_code=500,
        )

    # ------------------------------------------------------------------ 阶段

    async def _develop(self, run: WorkflowRun, actor: User) -> RunOutcome | None:
        """Developer 阶段：出变更 → L2 生成 diff → 尝试 L4 写盘。

        L4 被审批拦下是**预期停点**，返回暂停结果而不是异常。
        """
        requirement = await self._requirements.get(run.requirement_id)
        assert requirement is not None
        gateway = self._gateway_for(run, actor)
        ctx = ToolContext(requirement_id=run.requirement_id, workflow_run_id=run.id)

        listing = await self._run_tool_step(
            lambda: gateway.execute("list_files", {}, context=ctx), run=run, phase="list_files"
        )
        workspace_files = [
            str(entry.get("path")) for entry in listing.get("entries", []) if entry.get("type") == "file"
        ]
        architecture = await self._artifacts.latest(run.requirement_id, ArtifactType.ARCHITECTURE)
        prd = dict(requirement.prd_json or {})

        outcome = await self._run_agent_step(
            lambda: self._runtime.run(
                DEVELOPER_SPEC,
                user_prompt=build_developer_user_prompt(
                    prd=prd,
                    architecture=dict(architecture.content_json) if architecture else {},
                    workspace_files=workspace_files,
                ),
                context=AgentContext(requirement_id=run.requirement_id),
            ),
            run=run,
            phase="develop",
        )
        patch = outcome.output
        changes = [{"path": c.path, "new_content": c.new_content} for c in patch.changes]

        generated = await self._run_tool_step(
            lambda: gateway.execute("generate_patch", {"changes": changes}, context=ctx),
            run=run,
            phase="generate_patch",
        )
        await self._save_artifact(
            requirement_id=run.requirement_id,
            artifact_type=ArtifactType.PATCH,
            content={
                "summary": patch.summary,
                "notes": patch.notes,
                "follows_architecture": patch.follows_architecture,
                "changes": changes,
                "diff": generated.get("diff", ""),
                "files": generated.get("files", []),
            },
            agent_run_id=outcome.agent_run_id,
        )

        run.current_step = WorkflowStep.TOOL_GATEWAY.value
        await self._session.commit()

        try:
            await gateway.execute("apply_patch", {"changes": changes}, context=ctx)
        except AppError as exc:
            if exc.code == "APPROVAL_REQUIRED":
                # 预期停点：Gateway 已经自动创建了审批（在它自己的事务里提交）
                approval_id = exc.details.get("approval_id")
                logger.info(
                    "workflow paused for patch approval | run=%s approval=%s",
                    run.id,
                    approval_id,
                )
                return RunOutcome(
                    run,
                    paused=True,
                    pause_reason="tool_approval",
                    approval_id=UUID(str(approval_id)) if approval_id else None,
                )
            await self._mark_failed(run, exc)
            raise

        # 少见路径：审批已存在且可用（重放/补偿场景）—— 写盘成功，直接进测试
        return await self._after_apply(run, actor, gateway, ctx, changes)

    async def _apply_and_continue(
        self, run: WorkflowRun, actor: User, approval_id: UUID
    ) -> RunOutcome | None:
        """消费已批准的审批写盘；成功后推进到 TESTING 并继续主循环。"""
        requirement = await self._requirements.get(run.requirement_id)
        assert requirement is not None
        gateway = self._gateway_for(run, actor)
        ctx = ToolContext(requirement_id=run.requirement_id, workflow_run_id=run.id)

        patch_artifact = await self._artifacts.latest(run.requirement_id, ArtifactType.PATCH)
        if patch_artifact is None:
            raise ConflictError(
                "No patch artifact to apply",
                code="PATCH_ARTIFACT_MISSING",
            )
        content = dict(patch_artifact.content_json)
        changes = [{"path": c["path"], "new_content": c["new_content"]} for c in content.get("changes", [])]

        try:
            applied = await gateway.execute(
                "apply_patch", {**({"changes": changes}), "approval_id": str(approval_id)}, context=ctx
            )
        except AppError as exc:
            await self._mark_failed(run, exc)
            raise

        logger.info("patch applied | run=%s files=%s", run.id, len(applied.get("written", [])))
        return await self._after_apply(run, actor, gateway, ctx, changes)

    async def _after_apply(
        self,
        run: WorkflowRun,
        actor: User,
        gateway: ToolGateway,
        ctx: ToolContext,
        changes: list[dict[str, str]],
    ) -> RunOutcome | None:
        """写盘成功后的公共尾部：进测试 → 审查。返回 None 让调用方继续主循环。"""
        _ = gateway, ctx, changes
        self._transition(run, WorkflowStatus.TESTING)
        run.current_step = WorkflowStep.TESTER_AGENT.value
        await self._session.commit()
        return None

    async def _test(self, run: WorkflowRun, actor: User) -> None:
        requirement = await self._requirements.get(run.requirement_id)
        assert requirement is not None
        architecture = await self._artifacts.latest(run.requirement_id, ArtifactType.ARCHITECTURE)
        patch_artifact = await self._artifacts.latest(run.requirement_id, ArtifactType.PATCH)
        patch = dict(patch_artifact.content_json) if patch_artifact else {}
        written = [c.get("path", "") for c in patch.get("changes", [])]

        outcome = await self._run_agent_step(
            lambda: self._runtime.run(
                TESTER_SPEC,
                user_prompt=build_tester_user_prompt(
                    prd=dict(requirement.prd_json or {}),
                    architecture=dict(architecture.content_json) if architecture else {},
                    written_files=written,
                ),
                context=AgentContext(requirement_id=run.requirement_id),
            ),
            run=run,
            phase="test",
        )
        await self._save_artifact(
            requirement_id=run.requirement_id,
            artifact_type=ArtifactType.TEST_REPORT,
            content=outcome.parsed,
            agent_run_id=outcome.agent_run_id,
        )
        self._transition(run, WorkflowStatus.REVIEWING)
        run.current_step = WorkflowStep.REVIEWER_AGENT.value
        await self._session.commit()

    async def _review(self, run: WorkflowRun, actor: User) -> bool:
        """审查阶段。返回 True 表示通过（停在最终审批），False 表示要求返工。"""
        requirement = await self._requirements.get(run.requirement_id)
        assert requirement is not None
        architecture = await self._artifacts.latest(run.requirement_id, ArtifactType.ARCHITECTURE)
        patch_artifact = await self._artifacts.latest(run.requirement_id, ArtifactType.PATCH)
        patch = dict(patch_artifact.content_json) if patch_artifact else {}
        test_artifact = await self._artifacts.latest(run.requirement_id, ArtifactType.TEST_REPORT)

        outcome = await self._run_agent_step(
            lambda: self._runtime.run(
                REVIEWER_SPEC,
                user_prompt=build_reviewer_user_prompt(
                    prd=dict(requirement.prd_json or {}),
                    architecture=dict(architecture.content_json) if architecture else {},
                    patch_summary=str(patch.get("summary", "")),
                    diff=str(patch.get("diff", "")),
                    test_report=dict(test_artifact.content_json) if test_artifact else {},
                ),
                context=AgentContext(requirement_id=run.requirement_id),
            ),
            run=run,
            phase="review",
        )
        await self._save_artifact(
            requirement_id=run.requirement_id,
            artifact_type=ArtifactType.REVIEW,
            content=outcome.parsed,
            agent_run_id=outcome.agent_run_id,
        )

        review = outcome.output
        if review.verdict == "approved":
            self._transition(run, WorkflowStatus.WAITING_APPROVAL)
            run.current_step = WorkflowStep.APPROVAL.value
            await self._session.commit()
            logger.info("workflow passed review, waiting for final approval | run=%s", run.id)
            return True

        self._transition(run, WorkflowStatus.REVISION_REQUIRED)
        run.current_step = WorkflowStep.DEVELOPER_AGENT.value
        await self._session.commit()
        logger.info("review requires revision | run=%s", run.id)
        return False

    # ------------------------------------------------------------------ 内部

    async def _load(
        self, run_id: UUID, actor: User, roles: frozenset[ProjectRole], action: str
    ) -> tuple[WorkflowRun, Requirement]:
        run = await self._workflows.get(run_id)
        if run is None:
            raise NotFoundError.for_resource("WORKFLOW_RUN", "Workflow run does not exist")
        requirement = await self._requirements.get(run.requirement_id)
        if requirement is None:  # pragma: no cover - 外键保证不会发生
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")
        project = await self._access.load_project(requirement.project_id)
        await self._access.require(project, actor, roles, action=action)
        return run, requirement

    def _gateway_for(self, run: WorkflowRun, actor: User) -> ToolGateway:
        root = self._workspace_root / f"req-{run.requirement_id}"
        return ToolGateway(self._session, workspace_root=root, requested_by=actor.id)

    async def _run_agent_step(self, call, *, run: WorkflowRun, phase: str):
        """跑一次 Agent；任何 AppError 都把工作流标为 FAILED 再抛出。"""
        try:
            return await call()
        except AppError as exc:
            await self._mark_failed(run, exc)
            raise
        except Exception:
            logger.exception("unexpected error in workflow phase %s | run=%s", phase, run.id)
            raise

    async def _run_tool_step(self, call, *, run: WorkflowRun, phase: str) -> dict[str, Any]:
        try:
            return await call()
        except AppError as exc:
            await self._mark_failed(run, exc)
            raise

    def _transition(self, run: WorkflowRun, target: WorkflowStatus) -> None:
        current = WorkflowStatus(run.status)
        ensure_transition(current, target)
        run.status = target.value

    def _expect(self, run: WorkflowRun, status: WorkflowStatus, step: WorkflowStep) -> None:
        if WorkflowStatus(run.status) is not status or WorkflowStep(run.current_step) is not step:
            raise ConflictError(
                "Workflow run is not in the expected state",
                code="WORKFLOW_STATE_CONFLICT",
                details={"current_status": run.status, "current_step": run.current_step},
            )

    def _set_requirement_status(self, requirement: Requirement, target: RequirementStatus) -> None:
        from app.domain.requirement import ensure_transition as ensure_requirement_transition

        ensure_requirement_transition(RequirementStatus(requirement.status), target)
        requirement.status = target.value

    async def _mark_failed(self, run: WorkflowRun, exc: AppError) -> None:
        """把工作流标为 FAILED。失败原因存 run 上（agent_runs 里有细节）。"""
        if can_transition(WorkflowStatus(run.status), WorkflowStatus.FAILED):
            run.status = WorkflowStatus.FAILED.value
        run.error_code = exc.code
        run.error_message = exc.message
        run.finished_at = datetime.now(UTC)
        await self._session.commit()
        logger.warning(
            "workflow run failed | run=%s phase_step=%s code=%s", run.id, run.current_step, exc.code
        )

    async def _save_artifact(
        self,
        *,
        requirement_id: UUID,
        artifact_type: ArtifactType,
        content: dict,
        agent_run_id: UUID | None,
    ) -> Artifact:
        artifact = Artifact(
            requirement_id=requirement_id,
            agent_run_id=agent_run_id,
            type=artifact_type.value,
            version=await self._artifacts.next_version(requirement_id, artifact_type),
            content_json=content,
        )
        await self._artifacts.add(artifact)
        return artifact

    @staticmethod
    def _role_name(role: AgentRole) -> str:
        return role.value
