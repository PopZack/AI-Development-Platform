"""补丁工具与审批消费的闭环测试。

完整走一遍文档流程 B 的 Developer 环节：

    generate_patch (L2) 只出 diff 不写盘
      → apply_patch (L4) 第一次调用被拦，自动发起审批
      → OWNER 批准
      → 带上 approval_id 重试 → 真正写盘
      → 再试一次 → 被拒（一条审批只换一次成功执行）
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.approval_service import ApprovalService
from app.common.exceptions import ApprovalRequiredError, ToolDeniedError, ValidationError
from app.domain.tool_levels import ToolLevel
from app.infrastructure.tools import ToolContext, ToolDefinition, ToolGateway
from app.models.project import Project, ProjectMember
from app.models.requirement import Requirement
from app.models.user import User


@pytest.fixture
def workspace(tmp_path: Any) -> Any:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("value = 1\n", encoding="utf-8")
    return tmp_path


@pytest.fixture
async def requirement_id(db_session: AsyncSession, reviewer: User) -> UUID:
    suffix = uuid4().hex[:8]
    user = User(
        email=f"patch-{suffix}@example.com",
        password_hash="x",
        display_name="Patch Test User",
        status="ACTIVE",
    )
    db_session.add(user)
    await db_session.flush()
    project = Project(name="Patch Test", slug=f"patch-{suffix}", owner_id=user.id, status="ACTIVE")
    db_session.add(project)
    await db_session.flush()
    db_session.add(ProjectMember(project_id=project.id, user_id=user.id, role="OWNER"))
    # reviewer 也要是成员（且是 OWNER），否则它去批准时会 403
    db_session.add(ProjectMember(project_id=project.id, user_id=reviewer.id, role="OWNER"))
    requirement = Requirement(
        project_id=project.id,
        title="补丁测试",
        description="d",
        status="DRAFT",
        priority="P0",
        created_by=user.id,
        version=1,
    )
    db_session.add(requirement)
    await db_session.commit()
    return requirement.id


@pytest.fixture
async def requester(db_session: AsyncSession) -> User:
    """触发工具的用户。``requested_by`` 是真实外键，不能用随机 UUID。"""
    user = User(
        email=f"requester-{uuid4().hex[:8]}@example.com",
        password_hash="x",
        display_name="Requester",
        status="ACTIVE",
    )
    db_session.add(user)
    await db_session.commit()
    return user


@pytest.fixture
async def reviewer(db_session: AsyncSession) -> User:
    user = User(
        email=f"reviewer-{uuid4().hex[:8]}@example.com",
        password_hash="x",
        display_name="Reviewer",
        status="ACTIVE",
    )
    db_session.add(user)
    await db_session.commit()
    return user


CHANGES = [{"path": "app/main.py", "new_content": "value = 2\n"}]


async def _make_requirement(session: AsyncSession, owner: User) -> UUID:
    """再造一份需求（外键不允许用假 id 冒充「另一份需求」）。"""
    suffix = uuid4().hex[:8]
    project = Project(name=f"P-{suffix}", slug=f"p-{suffix}", owner_id=owner.id, status="ACTIVE")
    session.add(project)
    await session.flush()
    session.add(ProjectMember(project_id=project.id, user_id=owner.id, role="OWNER"))
    requirement = Requirement(
        project_id=project.id,
        title="另一份需求",
        description="d",
        status="DRAFT",
        priority="P0",
        created_by=owner.id,
        version=1,
    )
    session.add(requirement)
    await session.commit()
    return requirement.id


def _gateway(session: AsyncSession, workspace: Path, requested_by: UUID | None = None) -> ToolGateway:
    return ToolGateway(session, workspace_root=workspace, requested_by=requested_by)


def _ctx(requirement_id: UUID) -> ToolContext:
    return ToolContext(requirement_id=requirement_id)


# ------------------------------------------------------------ generate_patch


async def test_generate_patch_previews_without_writing(
    db_session: AsyncSession, workspace: Path, requirement_id: UUID, requester: User
) -> None:
    gateway = _gateway(db_session, workspace)

    result = await gateway.execute("generate_patch", {"changes": CHANGES}, context=_ctx(requirement_id))

    assert "-value = 1" in result["diff"]
    assert "+value = 2" in result["diff"]
    assert result["files"][0]["is_new_file"] is False
    # L2 只出预览，不写盘 —— 这是它和 L4 的本质区别
    assert (workspace / "app" / "main.py").read_text(encoding="utf-8") == "value = 1\n"


async def test_generate_patch_reports_new_files(
    db_session: AsyncSession, workspace: Path, requirement_id: UUID, requester: User
) -> None:
    gateway = _gateway(db_session, workspace)

    result = await gateway.execute(
        "generate_patch",
        {"changes": [{"path": "app/extra.py", "new_content": "x = 1\n"}]},
        context=_ctx(requirement_id),
    )

    assert result["files"][0]["is_new_file"] is True


async def test_generate_patch_rejects_bad_changes(
    db_session: AsyncSession, workspace: Path, requirement_id: UUID, requester: User
) -> None:
    gateway = _gateway(db_session, workspace)

    for bad in ({}, {"changes": []}, {"changes": [{"path": ""}]}):
        with pytest.raises(ValidationError) as excinfo:
            await gateway.execute("generate_patch", bad, context=_ctx(requirement_id))
        assert excinfo.value.code == "PATCH_CHANGES_INVALID"


# ---------------------------------------------------------------- 审批闭环


async def test_apply_patch_full_approval_loop(
    db_session: AsyncSession, workspace: Path, requirement_id: UUID, reviewer: User, requester: User
) -> None:
    """第一次调用被拦并自动发起审批 → 批准 → 重试成功 → 文件真的变了。"""
    gateway = _gateway(db_session, workspace, requested_by=requester.id)

    # 1) 第一次：被拦
    with pytest.raises(ApprovalRequiredError) as excinfo:
        await gateway.execute("apply_patch", {"changes": CHANGES}, context=_ctx(requirement_id))
    approval_id = UUID(excinfo.value.details["approval_id"])

    # 文件还没变
    assert (workspace / "app" / "main.py").read_text(encoding="utf-8") == "value = 1\n"

    # 2) 未批准就重试 → 拒绝
    with pytest.raises(ApprovalRequiredError) as excinfo:
        await gateway.execute(
            "apply_patch",
            {"changes": CHANGES, "approval_id": str(approval_id)},
            context=_ctx(requirement_id),
        )
    assert excinfo.value.code == "APPROVAL_NOT_APPROVED"

    # 3) OWNER 批准
    service = ApprovalService(db_session)
    await service.approve(approval_id, actor=reviewer, note="ok")

    # 4) 重试 → 成功，文件真的变了
    result = await gateway.execute(
        "apply_patch",
        {"changes": CHANGES, "approval_id": str(approval_id)},
        context=_ctx(requirement_id),
    )
    assert result["written"][0]["path"] == "app/main.py"
    assert (workspace / "app" / "main.py").read_text(encoding="utf-8") == "value = 2\n"


async def test_an_approval_can_only_be_consumed_once(
    db_session: AsyncSession, workspace: Path, requirement_id: UUID, reviewer: User, requester: User
) -> None:
    """没有这条，批准一次就能反复写盘 —— 审批就失去了意义。"""
    gateway = _gateway(db_session, workspace, requested_by=requester.id)

    with pytest.raises(ApprovalRequiredError) as excinfo:
        await gateway.execute("apply_patch", {"changes": CHANGES}, context=_ctx(requirement_id))
    approval_id = UUID(excinfo.value.details["approval_id"])

    await ApprovalService(db_session).approve(approval_id, actor=reviewer)
    await gateway.execute(
        "apply_patch",
        {"changes": CHANGES, "approval_id": str(approval_id)},
        context=_ctx(requirement_id),
    )

    with pytest.raises(ToolDeniedError) as excinfo:
        await gateway.execute(
            "apply_patch",
            {
                "changes": [{"path": "app/main.py", "new_content": "value = 999\n"}],
                "approval_id": str(approval_id),
            },
            context=_ctx(requirement_id),
        )
    assert excinfo.value.code == "APPROVAL_ALREADY_USED"
    # 第二次写盘没有发生
    assert (workspace / "app" / "main.py").read_text(encoding="utf-8") == "value = 2\n"


async def test_approval_must_match_the_tool(
    db_session: AsyncSession, workspace: Path, requirement_id: UUID, reviewer: User, requester: User
) -> None:
    """给 read_file 批的审批不能拿来 apply_patch —— 错误码要能说明原因。"""
    gateway = _gateway(db_session, workspace, requested_by=requester.id)

    async def _read_only(request) -> dict:  # pragma: no cover - 不会真的执行
        return {}

    gateway.register(
        ToolDefinition(
            name="something_else", level=ToolLevel.L4, description="别的 L4 工具", handler=_read_only
        )
    )

    with pytest.raises(ApprovalRequiredError) as excinfo:
        await gateway.execute("apply_patch", {"changes": CHANGES}, context=_ctx(requirement_id))
    approval_id = UUID(excinfo.value.details["approval_id"])

    await ApprovalService(db_session).approve(approval_id, actor=reviewer)

    with pytest.raises(ToolDeniedError) as excinfo:
        await gateway.execute(
            "something_else", {"approval_id": str(approval_id)}, context=_ctx(requirement_id)
        )
    assert excinfo.value.code == "APPROVAL_TOOL_MISMATCH"


async def test_approval_must_match_the_requirement(
    db_session: AsyncSession, workspace: Path, requirement_id: UUID, reviewer: User, requester: User
) -> None:
    """给需求 A 批的审批不能用在需求 B 上 —— 否则审批的边界就没了。"""
    gateway = _gateway(db_session, workspace, requested_by=requester.id)

    with pytest.raises(ApprovalRequiredError) as excinfo:
        await gateway.execute("apply_patch", {"changes": CHANGES}, context=_ctx(requirement_id))
    approval_id = UUID(excinfo.value.details["approval_id"])

    await ApprovalService(db_session).approve(approval_id, actor=reviewer)

    other_requirement = await _make_requirement(db_session, reviewer)
    with pytest.raises(ToolDeniedError) as excinfo:
        await gateway.execute(
            "apply_patch",
            {"changes": CHANGES, "approval_id": str(approval_id)},
            context=ToolContext(requirement_id=other_requirement),  # 另一份需求
        )
    assert excinfo.value.code == "APPROVAL_REQUIREMENT_MISMATCH"


async def test_apply_patch_rejects_malformed_approval_id(
    db_session: AsyncSession, workspace: Path, requirement_id: UUID, requester: User
) -> None:
    """带了 approval_id 但格式不对 → 明确拒绝，而不是悄悄当成「没带」重新发起审批。"""
    gateway = _gateway(db_session, workspace, requested_by=requester.id)

    with pytest.raises(ToolDeniedError) as excinfo:
        await gateway.execute(
            "apply_patch",
            {"changes": CHANGES, "approval_id": "not-a-uuid"},
            context=_ctx(requirement_id),
        )

    assert excinfo.value.code == "APPROVAL_ID_INVALID"


async def test_apply_patch_rejects_paths_outside_workspace(
    db_session: AsyncSession, workspace: Path, requirement_id: UUID, reviewer: User, requester: User
) -> None:
    """审批不是万能通行证：路径校验在批准之后依然生效。"""
    gateway = _gateway(db_session, workspace, requested_by=requester.id)

    outside_changes = [{"path": "../../escaped.txt", "new_content": "boom\n"}]
    with pytest.raises(ApprovalRequiredError) as excinfo:
        await gateway.execute("apply_patch", {"changes": outside_changes}, context=_ctx(requirement_id))
    approval_id = UUID(excinfo.value.details["approval_id"])

    await ApprovalService(db_session).approve(approval_id, actor=reviewer)

    with pytest.raises(ToolDeniedError):
        await gateway.execute(
            "apply_patch",
            {"changes": outside_changes, "approval_id": str(approval_id)},
            context=_ctx(requirement_id),
        )
    assert not (workspace.parent / "escaped.txt").exists()


async def test_expired_approval_blocks_execution(
    db_session: AsyncSession, workspace: Path, requirement_id: UUID, requester: User
) -> None:
    """批准之后过期了，同样不能用 —— 过期判定的入口不止审批接口一处。"""
    from app.models.approval import Approval

    gateway = _gateway(db_session, workspace, requested_by=requester.id)

    with pytest.raises(ApprovalRequiredError) as excinfo:
        await gateway.execute("apply_patch", {"changes": CHANGES}, context=_ctx(requirement_id))
    approval_id = UUID(excinfo.value.details["approval_id"])

    approval = await db_session.get(Approval, approval_id)
    approval.status = "APPROVED"
    approval.expires_at = datetime.now(UTC) - timedelta(minutes=1)
    await db_session.commit()

    with pytest.raises(ApprovalRequiredError) as excinfo:
        await gateway.execute(
            "apply_patch",
            {"changes": CHANGES, "approval_id": str(approval_id)},
            context=_ctx(requirement_id),
        )
    assert excinfo.value.code == "APPROVAL_NOT_APPROVED"
