"""需求生命周期规则。

Layer: Domain。

和 workflow.py 的区别：需求的状态描述「这份需求被设计到什么程度」，
工作流的状态描述「这次执行跑到哪一步」。一份需求可以先后关联多次
工作流（改需求 → 重新跑），所以两者的状态集合刻意不同。
"""

from __future__ import annotations

from app.common.exceptions import ConflictError
from app.domain.enums import RequirementStatus

__all__ = [
    "REQUIREMENT_TRANSITIONS",
    "NON_EDITABLE_STATUSES",
    "can_transition",
    "ensure_transition",
    "ensure_editable",
    "is_terminal",
]

REQUIREMENT_TRANSITIONS: dict[RequirementStatus, frozenset[RequirementStatus]] = {
    RequirementStatus.DRAFT: frozenset({RequirementStatus.ANALYZING, RequirementStatus.CANCELLED}),
    RequirementStatus.ANALYZING: frozenset(
        {RequirementStatus.DESIGNED, RequirementStatus.FAILED, RequirementStatus.CANCELLED}
    ),
    # 允许重新分析：需求被修改后应该能再跑一遍，而不是卡死在 DESIGNED
    RequirementStatus.DESIGNED: frozenset(
        {RequirementStatus.ANALYZING, RequirementStatus.COMPLETED, RequirementStatus.CANCELLED}
    ),
    RequirementStatus.COMPLETED: frozenset(),
    RequirementStatus.FAILED: frozenset(),
    RequirementStatus.CANCELLED: frozenset(),
}

# 终态需求不允许再改内容 —— 否则已经生成的 PRD / 架构会悄悄失效，
# 而 version 字段也就失去了意义
NON_EDITABLE_STATUSES: frozenset[RequirementStatus] = frozenset(
    {RequirementStatus.COMPLETED, RequirementStatus.CANCELLED}
)


def ensure_editable(status: RequirementStatus) -> None:
    """需求内容是否还允许修改。"""
    if status in NON_EDITABLE_STATUSES:
        raise ConflictError(
            f"Requirement in status {status} can no longer be modified",
            code="REQUIREMENT_NOT_EDITABLE",
            details={"current_status": str(status)},
        )


def can_transition(current: RequirementStatus, target: RequirementStatus) -> bool:
    return target in REQUIREMENT_TRANSITIONS.get(current, frozenset())


def is_terminal(status: RequirementStatus) -> bool:
    return not REQUIREMENT_TRANSITIONS.get(status, frozenset())


def ensure_transition(current: RequirementStatus, target: RequirementStatus) -> None:
    if not can_transition(current, target):
        raise ConflictError(
            f"Illegal requirement transition: {current} -> {target}",
            code="REQUIREMENT_STATUS_CONFLICT",
            details={
                "current_status": str(current),
                "target_status": str(target),
                "allowed": sorted(str(s) for s in REQUIREMENT_TRANSITIONS.get(current, frozenset())),
            },
        )
