"""需求生命周期规则测试。"""

from __future__ import annotations

import pytest

from app.common.exceptions import ConflictError
from app.domain.enums import RequirementStatus
from app.domain.requirement import (
    ensure_editable,
    ensure_runnable,
    ensure_transition,
    is_terminal,
)


def test_draft_can_start_analysis() -> None:
    assert ensure_transition(RequirementStatus.DRAFT, RequirementStatus.ANALYZING) is None


def test_designed_can_be_reanalyzed() -> None:
    """需求改过之后要能重跑分析，而不是卡死在 DESIGNED。"""
    assert ensure_transition(RequirementStatus.DESIGNED, RequirementStatus.ANALYZING) is None


def test_terminal_requirement_cannot_change_status() -> None:
    """COMPLETED / CANCELLED 是终态。FAILED 刻意不是 —— 见下一个用例。"""
    for status in (RequirementStatus.COMPLETED, RequirementStatus.CANCELLED):
        assert is_terminal(status)
        with pytest.raises(ConflictError) as excinfo:
            ensure_transition(status, RequirementStatus.ANALYZING)
        assert excinfo.value.code == "REQUIREMENT_STATUS_CONFLICT"


def test_failed_requirement_is_recoverable() -> None:
    """失败必须可恢复：一次模型调用抖动就把需求永久锁死、只能手工改库才能救回来，
    那是设计缺陷不是安全边界。"""
    assert not is_terminal(RequirementStatus.FAILED)
    assert ensure_transition(RequirementStatus.FAILED, RequirementStatus.ANALYZING) is None
    assert ensure_transition(RequirementStatus.FAILED, RequirementStatus.CANCELLED) is None


def test_illegal_transition_reports_allowed_targets() -> None:
    with pytest.raises(ConflictError) as excinfo:
        ensure_transition(RequirementStatus.DRAFT, RequirementStatus.COMPLETED)

    assert excinfo.value.details["allowed"] == ["ANALYZING", "CANCELLED"]


def test_draft_is_editable() -> None:
    ensure_editable(RequirementStatus.DRAFT)
    ensure_editable(RequirementStatus.DESIGNED)


@pytest.mark.parametrize("status", [RequirementStatus.COMPLETED, RequirementStatus.CANCELLED])
def test_finished_requirement_is_not_editable(status: RequirementStatus) -> None:
    """终态需求还能改内容的话，已生成的 PRD / 架构就悄悄失效了。"""
    with pytest.raises(ConflictError) as excinfo:
        ensure_editable(status)

    assert excinfo.value.code == "REQUIREMENT_NOT_EDITABLE"
    assert excinfo.value.status_code == 409


def test_open_requirement_is_runnable() -> None:
    ensure_runnable(RequirementStatus.DRAFT)
    ensure_runnable(RequirementStatus.DESIGNED)
    # FAILED 不在「已关闭」里 —— 失败的需求修完之后应该能重跑
    ensure_runnable(RequirementStatus.FAILED)


@pytest.mark.parametrize("status", [RequirementStatus.COMPLETED, RequirementStatus.CANCELLED])
def test_closed_requirement_cannot_start_a_new_run(status: RequirementStatus) -> None:
    """与「不可编辑」共用同一组状态，但错误码分开。

    如果两者共用一个码，前端就只能靠 message 字符串判断是「不能改」还是「不能再跑」。
    """
    with pytest.raises(ConflictError) as excinfo:
        ensure_runnable(status)

    assert excinfo.value.code == "REQUIREMENT_NOT_RUNNABLE"
    assert excinfo.value.details["current_status"] == str(status)
