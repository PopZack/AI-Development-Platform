"""需求生命周期规则测试。"""

from __future__ import annotations

import pytest

from app.common.exceptions import ConflictError
from app.domain.enums import RequirementStatus
from app.domain.requirement import ensure_editable, ensure_transition, is_terminal


def test_draft_can_start_analysis() -> None:
    assert ensure_transition(RequirementStatus.DRAFT, RequirementStatus.ANALYZING) is None


def test_designed_can_be_reanalyzed() -> None:
    """需求改过之后要能重跑分析，而不是卡死在 DESIGNED。"""
    assert ensure_transition(RequirementStatus.DESIGNED, RequirementStatus.ANALYZING) is None


def test_terminal_requirement_cannot_change_status() -> None:
    for status in (RequirementStatus.COMPLETED, RequirementStatus.CANCELLED, RequirementStatus.FAILED):
        assert is_terminal(status)
        with pytest.raises(ConflictError) as excinfo:
            ensure_transition(status, RequirementStatus.ANALYZING)
        assert excinfo.value.code == "REQUIREMENT_STATUS_CONFLICT"


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
