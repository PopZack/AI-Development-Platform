"""工作流状态机规则测试。

这些用例的意义不只是「代码没写错」，而是把设计文档 §3.3 里那两处缺口
钉成可执行的断言 —— 文档正文和状态机图再次打架时，测试会先炸，
而不是等到写 Workflow Service 的时候才发现无路可走。
"""

from __future__ import annotations

import pytest

from app.common.exceptions import ConflictError
from app.domain.enums import WORKFLOW_TERMINAL_STATUSES, WorkflowStatus
from app.domain.workflow import (
    WORKFLOW_TRANSITIONS,
    can_transition,
    ensure_transition,
    is_terminal,
)

HAPPY_PATH = [
    WorkflowStatus.CREATED,
    WorkflowStatus.RUNNING,
    WorkflowStatus.ANALYZING,
    WorkflowStatus.PLANNING,
    WorkflowStatus.IMPLEMENTING,
    WorkflowStatus.TESTING,
    WorkflowStatus.REVIEWING,
    WorkflowStatus.WAITING_APPROVAL,
    WorkflowStatus.APPROVED,
    WorkflowStatus.COMPLETED,
]


def test_happy_path_is_fully_connected() -> None:
    for current, target in zip(HAPPY_PATH, HAPPY_PATH[1:], strict=False):
        assert can_transition(current, target), f"{current} -> {target} 应该是合法迁移"


def test_missing_states_are_present_in_the_enum() -> None:
    """文档缺口：状态机图里有 REJECTED / REVISION_REQUIRED，状态表里没有。"""
    assert {"REJECTED", "REVISION_REQUIRED"} <= {status.value for status in WorkflowStatus}


def test_reviewing_can_reach_revision_required() -> None:
    """文档缺口：§12.7 的 Reviewer 输出是 needs_revision，
    没有 REVIEWING -> REVISION_REQUIRED 这条路，这个结果就没有合法去处。"""
    assert can_transition(WorkflowStatus.REVIEWING, WorkflowStatus.REVISION_REQUIRED)
    assert can_transition(WorkflowStatus.REVISION_REQUIRED, WorkflowStatus.IMPLEMENTING)


def test_client_cannot_jump_straight_to_completed() -> None:
    """文档 §3.3 的核心约束：只有 APPROVED 能走到 COMPLETED。"""
    for status in WorkflowStatus:
        if status is WorkflowStatus.APPROVED:
            continue
        assert not can_transition(status, WorkflowStatus.COMPLETED), f"{status} 不该能直接跳到 COMPLETED"


def test_waiting_approval_branches_both_ways() -> None:
    assert can_transition(WorkflowStatus.WAITING_APPROVAL, WorkflowStatus.APPROVED)
    assert can_transition(WorkflowStatus.WAITING_APPROVAL, WorkflowStatus.REJECTED)
    assert can_transition(WorkflowStatus.REJECTED, WorkflowStatus.REVISION_REQUIRED)


def test_same_status_is_not_a_transition() -> None:
    """同状态不算迁移，幂等判断交给上层，不要在这里悄悄放行。"""
    assert not can_transition(WorkflowStatus.RUNNING, WorkflowStatus.RUNNING)


def test_every_status_has_an_entry_in_the_transition_table() -> None:
    """新增了状态却忘了写出边，比状态机画错更难排查。"""
    assert not set(WorkflowStatus) - set(WORKFLOW_TRANSITIONS)


@pytest.mark.parametrize("terminal", sorted(WORKFLOW_TERMINAL_STATUSES))
def test_terminal_states_have_no_outgoing_edges(terminal: WorkflowStatus) -> None:
    assert WORKFLOW_TRANSITIONS[terminal] == frozenset()
    assert is_terminal(terminal)


def test_ensure_transition_raises_conflict_with_allowed_targets() -> None:
    with pytest.raises(ConflictError) as excinfo:
        ensure_transition(WorkflowStatus.CREATED, WorkflowStatus.COMPLETED)

    error = excinfo.value
    assert error.status_code == 409
    assert error.code == "WORKFLOW_STATUS_CONFLICT"
    assert error.details["current_status"] == "CREATED"
    # 报错信息必须告诉调用方「你只能往哪走」，否则前端只能靠猜
    assert error.details["allowed"] == ["CANCELLED", "FAILED", "RUNNING"]
    assert "COMPLETED" not in error.details["allowed"]


def test_ensure_transition_allows_legal_move() -> None:
    ensure_transition(WorkflowStatus.CREATED, WorkflowStatus.RUNNING)
