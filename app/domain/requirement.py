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
    "CLOSED_STATUSES",
    "can_transition",
    "ensure_transition",
    "ensure_editable",
    "ensure_runnable",
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

# 「已经关掉」的需求：内容不许再改（否则已生成的 PRD / 架构会悄悄失效，
# version 字段也失去意义），工作流也不许再起一条。
#
# WORKFLOW 失败（FAILED）刻意不在其中 —— 失败的需求修完之后应该能重跑。
CLOSED_STATUSES: frozenset[RequirementStatus] = frozenset(
    {RequirementStatus.COMPLETED, RequirementStatus.CANCELLED}
)


def ensure_editable(status: RequirementStatus) -> None:
    """需求内容是否还允许修改。"""
    if status in CLOSED_STATUSES:
        raise ConflictError(
            f"Requirement in status {status} can no longer be modified",
            code="REQUIREMENT_NOT_EDITABLE",
            details={"current_status": str(status)},
        )


def ensure_runnable(status: RequirementStatus) -> None:
    """是否还能为这份需求新建一条工作流。

    和 ``ensure_editable`` 共用同一组状态，但**错误码分开**：
    「需求不能改」和「需求不能再跑」对调用方是两件事，合成一个码会让
    前端只能靠 message 字符串判断。
    """
    if status in CLOSED_STATUSES:
        raise ConflictError(
            f"Requirement in status {status} cannot start a new workflow run",
            code="REQUIREMENT_NOT_RUNNABLE",
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
