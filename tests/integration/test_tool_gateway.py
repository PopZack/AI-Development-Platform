"""Tool Gateway 测试：路径校验、权限等级、L1 工具、审计记录。

路径校验是文档 §14.1 的硬线 —— **不能用字符串前缀判断**。
这里专门放了几个「字符串前缀会误判」的例子，确保我们的实现是对的。
"""

from __future__ import annotations

from pathlib import Path
from uuid import UUID, uuid4

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ApprovalRequiredError, NotFoundError, ToolDeniedError
from app.domain.enums import ToolCallStatus
from app.domain.tool_levels import (
    ToolAccessDecision,
    ToolLevel,
    decision_for,
    ensure_tool_allowed,
)
from app.infrastructure.tools import ToolContext, ToolDefinition, ToolGateway
from app.infrastructure.tools.paths import WorkspacePathValidator
from app.models.tool import ToolCall


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """造一个带真实内容的工作区。"""
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("def main():\n    return 'hello'\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# demo\n", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("找这里：todo_service\n", encoding="utf-8")
    (tmp_path / "app" / "__pycache__").mkdir()
    (tmp_path / "app" / "__pycache__" / "main.cpython-313.pyc").write_bytes(b"\x00\x01")
    (tmp_path / "logo.png").write_bytes(b"\x89PNG\r\n")
    return tmp_path


def _gateway(session: AsyncSession, workspace: Path) -> ToolGateway:
    return ToolGateway(session, workspace_root=workspace)


@pytest.fixture
async def requirement_id(db_session: AsyncSession) -> UUID:
    """造一条**真实**的需求链路。

    ``tool_calls.requirement_id`` 是 NOT NULL 外键且外键开关已打开，
    用随机 UUID 会被数据库直接拒绝 —— 测试里不能用假 id 糊过去。
    """
    from app.models.project import Project, ProjectMember
    from app.models.requirement import Requirement
    from app.models.user import User

    suffix = uuid4().hex[:8]
    user = User(
        email=f"tool-{suffix}@example.com",
        password_hash="x",
        display_name="Tool Test User",
        status="ACTIVE",
    )
    db_session.add(user)
    await db_session.flush()

    project = Project(name="Tool Test Project", slug=f"tool-test-{suffix}", owner_id=user.id, status="ACTIVE")
    db_session.add(project)
    await db_session.flush()
    db_session.add(ProjectMember(project_id=project.id, user_id=user.id, role="OWNER"))

    requirement = Requirement(
        project_id=project.id,
        title="工具审计测试",
        description="d",
        status="DRAFT",
        priority="P0",
        created_by=user.id,
        version=1,
    )
    db_session.add(requirement)
    await db_session.commit()
    return requirement.id


# ------------------------------------------------------------- 路径校验


def test_valid_relative_path_resolves_inside_root(workspace: Path) -> None:
    validator = WorkspacePathValidator(workspace)
    resolved = validator.validate("app/main.py")
    assert resolved == (workspace / "app" / "main.py").resolve()


def test_traversal_is_rejected(workspace: Path) -> None:
    """`../..` 在字符串前缀上完全合法 —— 这就是必须 resolve 的原因。"""
    validator = WorkspacePathValidator(workspace)
    with pytest.raises(ToolDeniedError) as excinfo:
        validator.validate("../../etc/passwd")
    assert excinfo.value.code == "TOOL_DENIED"


def test_absolute_path_outside_root_is_rejected(workspace: Path) -> None:
    validator = WorkspacePathValidator(workspace)
    outside = workspace.parent / "elsewhere.txt"
    with pytest.raises(ToolDeniedError):
        validator.validate(str(outside))


def test_prefix_lookalike_directory_is_rejected(workspace: Path) -> None:
    """`workspace-evil` 以 `workspace` 为前缀 —— 字符串前缀判断会放行。

    造出来的 `-evil` 目录不做清理：它在 pytest 的临时目录里，随用例生命周期回收。
    （本机沙箱会拦截程序内的目录删除，finally 里 rmdir 反而让用例报错。）
    """
    evil = workspace.parent / f"{workspace.name}-evil"
    evil.mkdir(exist_ok=True)
    validator = WorkspacePathValidator(workspace)
    with pytest.raises(ToolDeniedError):
        validator.validate(str(evil / "secret.txt"))


def test_empty_and_control_character_paths_are_rejected(workspace: Path) -> None:
    validator = WorkspacePathValidator(workspace)
    with pytest.raises(ToolDeniedError):
        validator.validate("")
    with pytest.raises(ToolDeniedError):
        validator.validate("a\nb")


# ------------------------------------------------------------- 权限等级


def test_level_decisions_match_the_document() -> None:
    """L0~L3 允许，L4 要审批，L5 禁止 —— 这张表就是文档 §14.1。"""
    assert decision_for(ToolLevel.L0) is ToolAccessDecision.ALLOWED
    assert decision_for(ToolLevel.L1) is ToolAccessDecision.ALLOWED
    assert decision_for(ToolLevel.L2) is ToolAccessDecision.ALLOWED
    assert decision_for(ToolLevel.L3) is ToolAccessDecision.ALLOWED
    assert decision_for(ToolLevel.L4) is ToolAccessDecision.APPROVAL_REQUIRED
    assert decision_for(ToolLevel.L5) is ToolAccessDecision.DENIED


def test_l4_raises_approval_not_denial() -> None:
    """L4 是「需要人点头」，不是「被拒绝」—— 两者后续动作完全不同。"""
    with pytest.raises(ApprovalRequiredError) as excinfo:
        ensure_tool_allowed(ToolLevel.L4)

    assert excinfo.value.code == "APPROVAL_REQUIRED"


def test_l5_raises_denial() -> None:
    with pytest.raises(ToolDeniedError):
        ensure_tool_allowed(ToolLevel.L5)


# ------------------------------------------------------------- L1 工具


async def test_list_files_skips_cache_and_binaries(db_session: AsyncSession, workspace: Path) -> None:
    gateway = _gateway(db_session, workspace)

    result = await gateway.execute("list_files", {}, context=ToolContext())

    paths = [entry["path"] for entry in result["entries"]]
    assert "app/main.py" in paths
    assert "README.md" in paths
    # __pycache__ 与二进制文件不该出现
    assert not any("__pycache__" in p for p in paths)
    assert not any("logo.png" in p for p in paths)


async def test_read_file_returns_content(db_session: AsyncSession, workspace: Path) -> None:
    gateway = _gateway(db_session, workspace)

    result = await gateway.execute("read_file", {"path": "app/main.py"})

    assert "def main():" in result["content"]
    assert result["truncated"] is False
    assert result["path"] == "app/main.py"


async def test_read_file_rejects_missing_and_directory(db_session: AsyncSession, workspace: Path) -> None:
    gateway = _gateway(db_session, workspace)

    with pytest.raises(NotFoundError):
        await gateway.execute("read_file", {"path": "no/such.py"})
    with pytest.raises(NotFoundError):
        await gateway.execute("read_file", {"path": "app"})


async def test_read_file_cannot_escape_workspace(db_session: AsyncSession, workspace: Path) -> None:
    """工具层也要拦越界 —— 不只靠 Gateway 的前置校验，双保险。"""
    gateway = _gateway(db_session, workspace)

    with pytest.raises(ToolDeniedError):
        await gateway.execute("read_file", {"path": "../../secrets.txt"})


async def test_search_code_finds_matches(db_session: AsyncSession, workspace: Path) -> None:
    gateway = _gateway(db_session, workspace)

    result = await gateway.execute("search_code", {"query": "todo_service"})

    assert result["count"] >= 1
    assert any(m["path"] == "notes.txt" for m in result["matches"])


async def test_search_code_requires_a_query(db_session: AsyncSession, workspace: Path) -> None:
    gateway = _gateway(db_session, workspace)

    with pytest.raises(NotFoundError) as excinfo:
        await gateway.execute("search_code", {"query": "  "})

    assert excinfo.value.code == "SEARCH_QUERY_EMPTY"


# ------------------------------------------------------------------- 审计


async def test_successful_call_is_recorded(
    db_session: AsyncSession, workspace: Path, requirement_id: UUID
) -> None:
    gateway = _gateway(db_session, workspace)

    await gateway.execute(
        "read_file",
        {"path": "README.md"},
        context=ToolContext(requirement_id=requirement_id),
    )

    call = (await db_session.execute(select(ToolCall))).scalar_one()
    assert call.tool_name == "read_file"
    assert call.status == ToolCallStatus.SUCCEEDED.value
    assert call.level == "L1"
    assert call.requirement_id == requirement_id
    assert call.result_summary is not None
    assert call.error_code is None


async def test_unknown_tool_is_denied_and_recorded(db_session: AsyncSession, workspace: Path) -> None:
    gateway = _gateway(db_session, workspace)

    with pytest.raises(NotFoundError) as excinfo:
        await gateway.execute("deploy_to_production", {})

    assert excinfo.value.code == "TOOL_UNKNOWN"
    call = (await db_session.execute(select(ToolCall))).scalar_one()
    assert call.status == ToolCallStatus.DENIED.value
    assert call.error_code == "TOOL_UNKNOWN"
    assert call.level == "UNKNOWN"


async def test_l4_tool_is_recorded_as_approval_required(db_session: AsyncSession, workspace: Path) -> None:
    """L4 要审批，且审计里能和「直接被拒」区分开 —— 审批流程要靠这条筛。"""

    async def _apply_patch(request) -> dict:  # pragma: no cover - 不会真的执行
        return {}

    gateway = _gateway(db_session, workspace)
    gateway.register(
        ToolDefinition(
            name="apply_patch",
            level=ToolLevel.L4,
            description="把补丁写进工作区（需人工审批）",
            handler=_apply_patch,
        )
    )

    with pytest.raises(ApprovalRequiredError):
        await gateway.execute("apply_patch", {"patch": "..."})

    call = (await db_session.execute(select(ToolCall))).scalar_one()
    assert call.status == ToolCallStatus.APPROVAL_REQUIRED.value
    assert call.level == "L4"


async def test_l5_tool_is_denied_and_recorded(db_session: AsyncSession, workspace: Path) -> None:
    """L5 首期完全禁止 —— 不是「要审批」，是根本不提供。"""

    async def _drop_db(request) -> dict:  # pragma: no cover - 不会真的执行
        return {}

    gateway = _gateway(db_session, workspace)
    gateway.register(
        ToolDefinition(name="drop_database", level=ToolLevel.L5, description="删库", handler=_drop_db)
    )

    with pytest.raises(ToolDeniedError):
        await gateway.execute("drop_database", {})

    call = (await db_session.execute(select(ToolCall))).scalar_one()
    assert call.status == ToolCallStatus.DENIED.value


async def test_failed_execution_is_recorded(db_session: AsyncSession, workspace: Path) -> None:
    """失败也要留痕：Agent 读不到它要的文件，是排查「为什么它做错了」的关键线索。"""
    gateway = _gateway(db_session, workspace)

    with pytest.raises(NotFoundError):
        await gateway.execute("read_file", {"path": "ghost.py"})

    call = (await db_session.execute(select(ToolCall))).scalar_one()
    assert call.status == ToolCallStatus.FAILED.value
    assert call.error_code == "WORKSPACE_FILE_NOT_FOUND"


async def test_audit_survives_an_outer_rollback(db_session: AsyncSession, workspace: Path) -> None:
    """审计记录在 Gateway 自己的事务里提交 —— 外层业务回滚时它必须留下来。

    出问题的时候，你恰恰需要知道 Agent 做过什么。
    """
    gateway = _gateway(db_session, workspace)
    await gateway.execute("list_files", {}, context=ToolContext())

    await db_session.rollback()

    total = (await db_session.execute(select(func.count()).select_from(ToolCall))).scalar_one()
    assert total == 1


# ------------------------------------------------------------- 外键真的生效


async def test_tool_call_rejects_unknown_requirement(db_session: AsyncSession) -> None:
    """SQLite 的外键开关打开之后，指向不存在需求的审计记录会被拒掉。"""
    db_session.add(
        ToolCall(
            requirement_id=uuid4(),  # 不存在
            tool_name="read_file",
            level="L1",
            status=ToolCallStatus.SUCCEEDED.value,
        )
    )
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()


# ------------------------------------------------------------- Prompt 素材


def test_describe_tools_returns_level_and_parameters(db_session: AsyncSession, workspace: Path) -> None:
    """给 Prompt 拼「你能用哪些工具」用 —— 没有参数说明，模型就只能瞎猜。"""
    gateway = _gateway(db_session, workspace)

    tools = gateway.describe_tools()

    names = {t["name"] for t in tools}
    assert {"list_files", "read_file", "search_code"} <= names
    for tool in tools:
        assert tool["level"].startswith("L")
        assert tool["description"]
        assert isinstance(tool["parameters"], dict)
