"""Agent 用例：Product Agent 生成 PRD、Architect Agent 生成技术设计。

Layer: Application（Service）。

## 事务边界（文档 §5.3：绝不能在持有事务时等待 LLM 返回）

文档的原话是「**先提交状态，再调外部服务，最后开新事务保存结果**」。
这不是洁癖，是必须的：一次模型调用实测要 **20~30 秒**，
握着这个事务的代价是：

- SQLite 下写事务会把库锁住，期间**其他所有请求都在排队**
- 换 MySQL 也会长时间占着连接与行锁

所以下面每个方法的形状都是：**校验 → 提交状态（释放事务）→ 调模型 → 新事务落库**。
``await self._session.commit()`` 那几行看起来多余，实际是整个方法里最要紧的地方。

## 状态机

    DRAFT --analyze--> ANALYZING（PRD 已就绪）
    ANALYZING --plan--> DESIGNED（技术设计已就绪）
    DESIGNED --analyze--> ANALYZING（需求改了要重跑）
    任意非终态 --失败--> FAILED

两个接口因此构成一条有顺序的流程：**没做过 analyze 就不能 plan**
（``ensure_transition`` 会直接拒掉 DRAFT → DESIGNED）。

⚠️ 状态名 ``ANALYZING`` 读起来像「正在分析」，实际语义是「分析已完成、PRD 已就绪」。
这是我在 Stage 1 起的名字，等设计文档补齐 ``requirements.status`` 的取值集合时
应该一并修正（更贴切的是 ``ANALYZED``）。已记入文档缺口清单。
"""

from __future__ import annotations

import logging
from pathlib import Path
from uuid import UUID

from sqlalchemy.ext.asyncio import AsyncSession

from app.agent.roles import (
    ARCHITECT_SPEC,
    PRODUCT_SPEC,
    build_architecture_user_prompt,
    build_prd_user_prompt,
)
from app.agent.runtime import AgentContext, AgentRuntime
from app.application.project_access import ProjectAccessGuard
from app.common.exceptions import AppError, ConflictError, NotFoundError
from app.domain.enums import ArtifactType, RequirementStatus
from app.domain.project import READ_ROLES, WRITE_ROLES
from app.domain.requirement import ensure_transition
from app.infrastructure.llm.base import LLMProvider
from app.models.agent import Artifact
from app.models.requirement import Requirement
from app.models.user import User
from app.repositories.agent_repository import ArtifactRepository
from app.repositories.requirement_repository import RequirementRepository
from app.repositories.workflow_repository import WorkflowRunRepository

__all__ = ["AgentService"]

logger = logging.getLogger(__name__)


class AgentService:
    def __init__(
        self,
        session: AsyncSession,
        provider: LLMProvider,
        *,
        max_output_attempts: int = 2,
        slow_call_seconds: float = 60.0,
    ) -> None:
        self._session = session
        self._requirements = RequirementRepository(session)
        self._artifacts = ArtifactRepository(session)
        self._workflows = WorkflowRunRepository(session)
        self._access = ProjectAccessGuard(session)
        self._runtime = AgentRuntime(
            session, provider, max_output_attempts=max_output_attempts, slow_call_seconds=slow_call_seconds
        )

    # ------------------------------------------------------------------ 分析

    async def analyze_requirement(self, requirement_id: UUID, *, actor: User) -> Artifact:
        """Product Agent：需求 → PRD。"""
        requirement = await self._load_for_write(requirement_id, actor, "run the Product Agent on")
        target = RequirementStatus.ANALYZING
        ensure_transition(RequirementStatus(requirement.status), target)

        # ⬇️ 提交状态，释放事务 —— 下面那次模型调用要几十秒，不能握着事务不放
        requirement.status = target.value
        await self._session.commit()

        user_prompt = build_prd_user_prompt(
            title=requirement.title,
            description=requirement.description,
            priority=requirement.priority,
            acceptance_criteria=list(requirement.acceptance_criteria_json or []),
        )

        outcome = await self._run_or_mark_failed(
            PRODUCT_SPEC,
            requirement_id=requirement_id,
            user_prompt=user_prompt,
        )

        # ⬇️ 新事务：写交付物 + 更新需求上的当前版本快照
        artifact = await self._save_artifact(
            requirement_id=requirement_id,
            artifact_type=ArtifactType.PRD,
            content=outcome.parsed,
            agent_run_id=outcome.agent_run_id,
        )
        requirement.prd_json = outcome.parsed
        await self._session.commit()
        logger.info(
            "prd generated | requirement=%s version=%s attempts=%s",
            requirement_id,
            artifact.version,
            outcome.attempts,
        )
        return artifact

    # ------------------------------------------------------------------ 设计

    async def plan_requirement(self, requirement_id: UUID, *, actor: User) -> Artifact:
        """Architect Agent：PRD → 技术设计。"""
        requirement = await self._load_for_write(requirement_id, actor, "run the Architect Agent on")
        target = RequirementStatus.DESIGNED
        ensure_transition(RequirementStatus(requirement.status), target)

        # 状态机已经保证了「先 analyze 过」，但 prd_json 为空仍然可能（手工改库之类），
        # 这时候给它一句明确的提示，比让模型对着 null 编一份设计强
        if not requirement.prd_json:
            raise ConflictError(
                "Requirement has no PRD yet; call /analyze first",
                code="PRD_REQUIRED",
                details={"requirement_id": str(requirement_id)},
            )

        prd = dict(requirement.prd_json)
        title = requirement.title

        # ⬇️ 提交状态，释放事务
        requirement.status = target.value
        await self._session.commit()

        outcome = await self._run_or_mark_failed(
            ARCHITECT_SPEC,
            requirement_id=requirement_id,
            user_prompt=build_architecture_user_prompt(prd=prd, title=title),
        )

        # 技术设计没有对应的需求列（只有 prd_json），所以只落交付物。
        # 「当前版本」靠 artifacts 里 version 最大的那条表达。
        artifact = await self._save_artifact(
            requirement_id=requirement_id,
            artifact_type=ArtifactType.ARCHITECTURE,
            content=outcome.parsed,
            agent_run_id=outcome.agent_run_id,
        )
        # ⚠️ 这行不能省。analyze 那边因为还要写 prd_json 所以顺带 commit 了，
        # 这边没有别的事要做，漏掉的话交付物就只存在于内存里：
        # 响应返回 201，请求一结束会话关闭，行被回滚 —— 数据悄悄丢掉。
        # 这个 bug 是「先 analyze + plan、再另发一个请求读列表」的用例抓出来的。
        await self._session.commit()
        logger.info(
            "architecture generated | requirement=%s version=%s attempts=%s",
            requirement_id,
            artifact.version,
            outcome.attempts,
        )
        return artifact

    # ------------------------------------------------------------------ 读取

    async def list_artifacts(
        self,
        requirement_id: UUID,
        *,
        actor: User,
        artifact_type: ArtifactType | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[Artifact], int]:
        """列出该需求的交付物。读权限即可（含 VIEWER）。"""
        requirement = await self._requirements.get(requirement_id)
        if requirement is None:
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")

        project = await self._access.load_project(requirement.project_id)
        await self._access.require(project, actor, READ_ROLES, action="read the artifacts of")

        items = await self._artifacts.list_by_requirement(
            requirement_id, artifact_type=artifact_type, limit=limit, offset=offset
        )
        total = await self._artifacts.count_by_requirement(requirement_id, artifact_type=artifact_type)
        return items, total

    async def deliverables_summary(
        self, requirement_id: UUID, *, actor: User, workspace_root: Path | None = None
    ) -> dict:
        """需求的完整交付物汇总（文档 §13 Stage 4 验收项）。

        按类型取最新版本；历史版本仍走 ``list_artifacts`` 查。
        工作区文件清单只读 ``settings.workspace_root/req-<id>`` —— 只列名字，
        不读内容：这是给人看的归档视图，不是给模型的上下文。
        """
        requirement = await self._requirements.get(requirement_id)
        if requirement is None:
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")

        project = await self._access.load_project(requirement.project_id)
        await self._access.require(project, actor, READ_ROLES, action="read the deliverables of")

        deliverables = [
            artifact
            for artifact_type in ArtifactType
            if (artifact := await self._artifacts.latest(requirement_id, artifact_type)) is not None
        ]
        runs = await self._workflows.list_by_requirement(requirement_id)
        latest_run = runs[-1] if runs else None

        workspace_files: list[str] = []
        if workspace_root is not None:
            root = workspace_root / f"req-{requirement_id}"
            if root.is_dir():
                workspace_files = sorted(
                    # as_posix：API 对外返回 portable 路径（Windows 上否则是反斜杠）
                    p.relative_to(root).as_posix()
                    for p in root.rglob("*")
                    if p.is_file()
                )

        return {
            "requirement": requirement,
            "latest_run": latest_run,
            "deliverables": deliverables,
            "workspace_files": workspace_files,
        }

    # ------------------------------------------------------------------ 内部

    async def _load_for_write(self, requirement_id: UUID, actor: User, action: str) -> Requirement:
        requirement = await self._requirements.get(requirement_id)
        if requirement is None:
            raise NotFoundError.for_resource("REQUIREMENT", "Requirement does not exist")

        project = await self._access.load_project(requirement.project_id)
        # 触发 Agent 会花掉 token（真金白银）并改动需求状态，所以要求写权限：
        # VIEWER 能看结果，但不能触发
        await self._access.require(project, actor, WRITE_ROLES, action=action)
        return requirement

    async def _run_or_mark_failed(self, spec, *, requirement_id: UUID, user_prompt: str):
        """调模型；失败时把需求标为 FAILED 再抛出。

        失败必须落到状态上，否则需求会永远停在 ANALYZING，而调用方只看到一次报错，
        重试又会被状态机拒掉（ANALYZING → ANALYZING 不是合法迁移），
        整条需求就废了。
        """
        try:
            return await self._runtime.run(
                spec,
                user_prompt=user_prompt,
                context=AgentContext(requirement_id=requirement_id),
            )
        except AppError as exc:
            # 失败原因不去 requirement 上另开一列存：agent_runs.error_message 里已经有了，
            # 那才是权威来源。这里只需要把状态推过去。
            logger.warning(
                "agent failed, marking requirement FAILED | role=%s requirement=%s code=%s",
                spec.role,
                requirement_id,
                exc.code,
            )
            await self._mark_failed(requirement_id)
            raise

    async def _mark_failed(self, requirement_id: UUID) -> None:
        """新开一个事务把需求标为失败。

        Runtime 在每次尝试后都 commit 过，所以这里是一个干净的新事务。

        失败必须落到状态上：否则需求会永远停在 ANALYZING，而调用方只看到一次报错，
        重试又会被状态机拒掉（ANALYZING → ANALYZING 不是合法迁移），整条需求就废了。
        对应地，``FAILED → ANALYZING`` 是合法迁移，失败之后可以重跑。
        """
        requirement = await self._requirements.get(requirement_id)
        if requirement is None:  # pragma: no cover - 极端竞态
            return
        requirement.status = RequirementStatus.FAILED.value
        await self._session.commit()

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
            # 版本递增而不是覆盖：需求改了要重跑，旧版必须留着，
            # 否则「这份 PRD 对应哪一版需求」就说不清了
            version=await self._artifacts.next_version(requirement_id, artifact_type),
            content_json=content,
        )
        await self._artifacts.add(artifact)
        return artifact
