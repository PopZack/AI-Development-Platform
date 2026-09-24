"""工作流后台执行：任务注册表、跨 worker 锁、进程重启后的孤儿回收。

Layer: Application。

为什么存在这个模块：start/resume 从「同步长请求」改成「202 + 后台执行」后，
出现了三个同步时代不存在的问题，都在这里解决：

1. **双击 / 两个 worker 同时触发**：start 用数据库原子认领（见
   ``WorkflowRunRepository.claim_start``）；resume 没有等价的状态跳变可认领
   （暂停点状态合法地停留几分钟），所以用「进程内注册表 + Redis 运行锁」双保险。
   没 Redis 时退化为仅进程内 —— 多 worker 不配 Redis 本来就是不支持的部署
   （限流与事件同样退化），这里保持一致。
2. **执行体要自己的数据库会话**：请求结束后请求级会话就关闭了，
   后台任务必须从会话工厂自开一个，跑完整个多步执行再还。
3. **进程重启的孤儿**：执行到一半进程没了，运行会永久卡在执行中状态。
   启动时扫描「执行中」的运行：持有运行锁的是别的活着的 worker（不动），
   其余标记 ``FAILED / INTERRUPTED`` —— 与 Agent 失败的终态语义一致，
   让「卡死」变成「看得见的失败」。
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from app.common.exceptions import ConflictError
from app.infrastructure.redis import RedisLike

logger = logging.getLogger(__name__)

__all__ = ["WorkflowTaskManager", "recover_orphaned_runs"]

LOCK_TTL_SECONDS = 90
HEARTBEAT_INTERVAL_SECONDS = 30


class WorkflowTaskManager:
    """每个进程一份：记录本进程正在执行的工作流，并持有跨 worker 的运行锁。"""

    def __init__(self, *, workspace_root: Path, redis: RedisLike | None = None) -> None:
        # 刻意**不**在这里捕获 provider：测试会事后把 app.state.llm_provider
        # 换成 Mock，捕获引用会拿到创建时的旧实例（真 Ark provider）——
        # 表现是测试里凭空出现「凭据无效」。provider 由 launch 调用方现取现传
        self._workspace_root = workspace_root
        self._redis = redis
        self._tasks: dict[UUID, asyncio.Task] = {}

    def active_run_ids(self) -> set[UUID]:
        """本进程正在执行的运行（启动孤儿回收时要跳过它们）。"""
        return {run_id for run_id, task in self._tasks.items() if not task.done()}

    def is_running(self, run_id: UUID) -> bool:
        task = self._tasks.get(run_id)
        return task is not None and not task.done()

    async def launch(
        self,
        run_id: UUID,
        actor_id: UUID,
        *,
        approval_id: UUID | None = None,
        provider: Any,
    ) -> None:
        """认领并发起后台执行。已被占用时抛 ``WORKFLOW_RUN_BUSY``。"""
        if self.is_running(run_id):
            raise ConflictError(
                "This workflow run is already executing in the background",
                code="WORKFLOW_RUN_BUSY",
                details={"hint": "轮询 GET /runs/{id} 或订阅事件流观察进展"},
            )
        token = uuid4().hex
        if self._redis is not None:
            # NX = 只在不存在时写入：别的 worker 正在执行同一运行时这里拿不到锁
            acquired = await self._redis.set(f"runlock:{run_id}", token, nx=True, ex=LOCK_TTL_SECONDS)
            if not acquired:
                raise ConflictError(
                    "This workflow run is already executing on another worker",
                    code="WORKFLOW_RUN_BUSY",
                    details={"hint": "多 worker 部署下执行实例由运行锁仲裁"},
                )
        task = asyncio.create_task(self._execute(run_id, actor_id, approval_id, token, provider))
        self._tasks[run_id] = task
        task.add_done_callback(lambda _task: self._tasks.pop(run_id, None))
        logger.info("workflow run accepted for background execution | run=%s actor=%s", run_id, actor_id)

    # ------------------------------------------------------------------ 内部

    async def _execute(
        self, run_id: UUID, actor_id: UUID, approval_id: UUID | None, token: str, provider: Any
    ) -> None:
        from app.application.workflow_orchestrator import WorkflowOrchestrator

        heartbeat = asyncio.create_task(self._beat(run_id)) if self._redis is not None else None
        try:
            await WorkflowOrchestrator.execute_pending(
                run_id,
                actor_id,
                provider=provider,
                workspace_root=self._workspace_root,
                approval_id=approval_id,
            )
        except Exception:
            # execute_pending 内部已兜底标记 FAILED；这里只防「兜底自己也炸」
            logger.exception("background workflow task crashed hard | run=%s", run_id)
        finally:
            if heartbeat is not None:
                heartbeat.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await heartbeat
            if self._redis is not None:
                await self._release_lock(run_id, token)
            self._tasks.pop(run_id, None)

    async def _beat(self, run_id: UUID) -> None:
        """给运行锁续期（expire 保留原 token）。执行步骤可达数分钟，TTL 必须
        一直续而不是设大：设大了崩溃后要等锁过期才能恢复。"""
        try:
            while True:
                await asyncio.sleep(HEARTBEAT_INTERVAL_SECONDS)
                await self._redis.expire(f"runlock:{run_id}", LOCK_TTL_SECONDS)
        except asyncio.CancelledError:
            pass

    async def _release_lock(self, run_id: UUID, token: str) -> None:
        try:
            key = f"runlock:{run_id}"
            current = await self._redis.get(key)
            if current == token:  # 只释放自己的锁：过期被别人抢走时不能误删
                await self._redis.delete(key)
        except Exception:  # noqa: BLE001 - 释放失败留给 TTL 自然过期
            logger.warning("failed to release run lock | run=%s", run_id, exc_info=True)


async def recover_orphaned_runs(*, skip_running: set[UUID], redis: RedisLike | None = None) -> int:
    """进程重启后把「卡在执行中」的运行标记为 ``FAILED / INTERRUPTED``。

    跳过两类：本进程注册表里还活着的（启动早期不该有，防御性）；
    Redis 运行锁仍然有效的（**别的 worker** 正在执行，重启的不是它）。
    返回标记数量，给启动日志一个可见的证据。
    """
    from app.domain.enums import WorkflowStatus, WorkflowStep
    from app.infrastructure.db.session import get_session_factory
    from app.models.user import User  # noqa: F401 —— 确保模型已加载
    from app.repositories.workflow_repository import WorkflowRunRepository

    factory = get_session_factory()
    async with factory() as session:
        repo = WorkflowRunRepository(session)
        executing = await repo.list_by_statuses(
            [
                WorkflowStatus.RUNNING.value,
                WorkflowStatus.ANALYZING.value,
                WorkflowStatus.PLANNING.value,
                WorkflowStatus.IMPLEMENTING.value,
                WorkflowStatus.TESTING.value,
                WorkflowStatus.REVIEWING.value,
            ]
        )
        marked = 0
        for run in executing:
            status = WorkflowStatus(run.status)
            step = WorkflowStep(run.current_step)
            if (status, step) == (WorkflowStatus.IMPLEMENTING, WorkflowStep.TOOL_GATEWAY):
                continue  # 合法停点：等人工审批，不是孤儿
            if run.id in skip_running:
                continue
            if redis is not None and await redis.get(f"runlock:{run.id}"):
                continue  # 别的 worker 活着且正在执行
            run.status = WorkflowStatus.FAILED.value
            run.error_code = "INTERRUPTED"
            run.error_message = "进程重启时执行被中断；请重新创建工作流"
            run.finished_at = datetime.now(UTC)
            marked += 1
        if marked:
            await session.commit()
            logger.warning("marked %s orphaned workflow run(s) as FAILED/INTERRUPTED", marked)
        return marked
