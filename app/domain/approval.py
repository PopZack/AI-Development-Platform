"""人工审批的生命周期规则。

Layer: Domain —— 只回答「这个审批能不能从当前状态变到目标状态」。

文档 §6.x 定义 ``approvals.status`` 的取值集合：
``PENDING / APPROVED / REJECTED / EXPIRED``，但**没给状态迁移表**。
这里的收敛：

    PENDING ──approve--> APPROVED（终态）
    PENDING ──reject---> REJECTED（终态）
    PENDING ──过期-----> EXPIRED（终态）

三个终态都不再有出边 —— 审批是一次性决定，不允许「批了又反悔」。
要改主意就重新发起一次审批，历史必须原样保留。

**过期是惰性判定**，不是后台任务：读取或审批时如果发现
``PENDING`` 且 ``expires_at`` 已过，就先落成 ``EXPIRED``。
这避免为一个低频事件跑常驻定时任务。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.common.exceptions import ConflictError
from app.domain.enums import ApprovalStatus

__all__ = [
    "APPROVAL_TRANSITIONS",
    "DEFAULT_APPROVAL_TTL_HOURS",
    "can_transition",
    "ensure_transition",
    "effective_status",
]

#: 审批默认有效期。给太长（比如 30 天）会让「待审批」堆积成无人看的列表
DEFAULT_APPROVAL_TTL_HOURS = 24

APPROVAL_TRANSITIONS: dict[ApprovalStatus, frozenset[ApprovalStatus]] = {
    ApprovalStatus.PENDING: frozenset(
        {ApprovalStatus.APPROVED, ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED}
    ),
    ApprovalStatus.APPROVED: frozenset(),
    ApprovalStatus.REJECTED: frozenset(),
    ApprovalStatus.EXPIRED: frozenset(),
}


def can_transition(current: ApprovalStatus, target: ApprovalStatus) -> bool:
    return target in APPROVAL_TRANSITIONS.get(current, frozenset())


def ensure_transition(current: ApprovalStatus, target: ApprovalStatus) -> None:
    if not can_transition(current, target):
        raise ConflictError(
            f"Illegal approval transition: {current} -> {target}",
            code="APPROVAL_STATUS_CONFLICT",
            details={"current_status": str(current), "target_status": str(target)},
        )


def effective_status(
    status: ApprovalStatus, expires_at: datetime | None, *, now: datetime | None = None
) -> ApprovalStatus:
    """把「已过期但还没被标记」的记录判成 ``EXPIRED``。

    ``expires_at`` 的含义随状态变化：

    - ``PENDING``：必须在何时前做出决定
    - ``APPROVED``：批准后必须在何时前**被消费** —— 否则一个批准永远有效，
      「人工审批」就退化成了一次性的放行券

    ``REJECTED`` 不受影响（那是一次已经做出的决定，不该被时间改写）。
    ``expires_at`` 为 ``None`` 表示永不过期。
    """
    if status in (ApprovalStatus.REJECTED, ApprovalStatus.EXPIRED):
        return status
    if expires_at is None:
        return status
    now = now or datetime.now(UTC)
    expiry = expires_at if expires_at.tzinfo else expires_at.replace(tzinfo=UTC)
    return ApprovalStatus.EXPIRED if now >= expiry else status


def ensure_pending(
    approval_status: ApprovalStatus, expires_at: datetime | None, *, approval_id: UUID
) -> ApprovalStatus:
    """审批动作的前置校验：必须是「仍有效的 PENDING」。

    返回判定后的实际状态（可能已被惰性标成 EXPIRED），调用方据此落库。
    """
    actual = effective_status(approval_status, expires_at)
    if actual is not ApprovalStatus.PENDING:
        raise ConflictError(
            f"Approval {approval_id} is no longer pending (status: {actual})",
            code="APPROVAL_NOT_PENDING",
            details={"approval_id": str(approval_id), "current_status": str(actual)},
        )
    return actual
