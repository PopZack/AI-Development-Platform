"""工作流状态迁移规则。

Layer: Domain —— 只回答「这个状态能不能变到那个状态」，
不碰数据库、不碰 HTTP、不碰 LLM。

设计文档 §3.3 的硬性要求：
    「不允许客户端通过一个请求直接把状态任意修改为 COMPLETED。
      所有状态迁移由 Domain 规则和 Workflow Service 验证。」
本模块就是那条规则的落点。

Stage 1 只定义规则并被单元测试覆盖；真正驱动状态迁移的
Workflow Service 在 Stage 4 落地。
"""

from __future__ import annotations

from app.common.exceptions import ConflictError
from app.domain.enums import WORKFLOW_TERMINAL_STATUSES, WorkflowStatus

__all__ = [
    "WORKFLOW_TRANSITIONS",
    "can_transition",
    "ensure_transition",
    "is_terminal",
]

# 关于 REVIEWING 的出边：文档的状态机图只画了 WAITING_APPROVAL 和 REJECTED
# 两条路，但 §12.7 的 Reviewer Agent 示例输出是 "needs_revision"，
# 按原图这个结果没有合法去处。因此这里补上 REVIEWING → REVISION_REQUIRED，
# 让「审查发现必须改的问题 → 回到实现」成为一条闭合回路。
WORKFLOW_TRANSITIONS: dict[WorkflowStatus, frozenset[WorkflowStatus]] = {
    WorkflowStatus.CREATED: frozenset(
        {WorkflowStatus.RUNNING, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.RUNNING: frozenset(
        {
            WorkflowStatus.ANALYZING,
            WorkflowStatus.BLOCKED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }
    ),
    WorkflowStatus.ANALYZING: frozenset(
        {WorkflowStatus.PLANNING, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.PLANNING: frozenset(
        {WorkflowStatus.IMPLEMENTING, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.IMPLEMENTING: frozenset(
        {WorkflowStatus.TESTING, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.TESTING: frozenset(
        {WorkflowStatus.REVIEWING, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.REVIEWING: frozenset(
        {
            WorkflowStatus.WAITING_APPROVAL,
            WorkflowStatus.REVISION_REQUIRED,
            WorkflowStatus.FAILED,
            WorkflowStatus.CANCELLED,
        }
    ),
    WorkflowStatus.REVISION_REQUIRED: frozenset(
        {WorkflowStatus.IMPLEMENTING, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.WAITING_APPROVAL: frozenset(
        {
            WorkflowStatus.APPROVED,
            WorkflowStatus.REJECTED,
            WorkflowStatus.BLOCKED,
            WorkflowStatus.CANCELLED,
        }
    ),
    WorkflowStatus.REJECTED: frozenset({WorkflowStatus.REVISION_REQUIRED, WorkflowStatus.CANCELLED}),
    WorkflowStatus.APPROVED: frozenset({WorkflowStatus.COMPLETED, WorkflowStatus.FAILED}),
    WorkflowStatus.BLOCKED: frozenset(
        {WorkflowStatus.IMPLEMENTING, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
    ),
    WorkflowStatus.COMPLETED: frozenset(),
    WorkflowStatus.FAILED: frozenset(),
    WorkflowStatus.CANCELLED: frozenset(),
}


def can_transition(current: WorkflowStatus, target: WorkflowStatus) -> bool:
    """当前状态是否允许迁移到目标状态。同状态视为非法（幂等判断交给上层）。"""
    return target in WORKFLOW_TRANSITIONS.get(current, frozenset())


def is_terminal(status: WorkflowStatus) -> bool:
    return status in WORKFLOW_TERMINAL_STATUSES


def ensure_transition(current: WorkflowStatus, target: WorkflowStatus) -> None:
    """非法迁移直接抛 ConflictError（HTTP 409），由统一错误处理器输出。"""
    if not can_transition(current, target):
        raise ConflictError(
            f"Illegal workflow transition: {current} -> {target}",
            code="WORKFLOW_STATUS_CONFLICT",
            details={
                "current_status": str(current),
                "target_status": str(target),
                "allowed": sorted(str(s) for s in WORKFLOW_TRANSITIONS.get(current, frozenset())),
            },
        )
