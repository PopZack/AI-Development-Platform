"""审批接口测试：L4 工具被拦 → 自动建审批 → OWNER 批准 / 驳回。

核心验证两件事：
1. 审批记录是 **Gateway 自动创建** 的（人不需要替系统记这件事）
2. 职责分离 —— Developer 能触发请求，但**不能批自己的请求**
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import ApprovalRequiredError
from app.domain.enums import ApprovalStatus
from app.domain.tool_levels import ToolLevel
from app.infrastructure.tools import ToolContext, ToolDefinition, ToolGateway
from tests.conftest import API


@pytest.fixture
def workspace(tmp_path: Any) -> Any:
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    return tmp_path


async def _register_l4_and_trigger(
    db_session: AsyncSession,
    workspace: Any,
    requirement_id: UUID,
    requested_by: UUID,
) -> UUID:
    """注册一个 L4 工具并触发它 —— 应该被拦并自动生成审批。"""

    async def _apply_patch(request) -> dict:  # pragma: no cover - 不会真的执行
        return {}

    gateway = ToolGateway(db_session, workspace_root=workspace, requested_by=requested_by)
    gateway.register(
        ToolDefinition(
            name="apply_patch",
            level=ToolLevel.L4,
            description="把补丁写进工作区（需人工审批）",
            handler=_apply_patch,
        )
    )
    with pytest.raises(ApprovalRequiredError) as excinfo:
        await gateway.execute(
            "apply_patch",
            {"patch": "--- a/main.py\n+++ b/main.py"},
            # agent_run_id 不传随机值：外键是生效的，假 id 会被数据库拒绝
            context=ToolContext(requirement_id=requirement_id),
        )
    return UUID(excinfo.value.details["approval_id"])


@pytest.fixture
async def pending_approval(
    db_session: AsyncSession,
    workspace: Any,
    make_project_with_member: object,
    make_requirement: object,
) -> dict[str, Any]:
    owner, project, developer = await make_project_with_member(role="DEVELOPER")
    requirement = await make_requirement(project["id"], owner["headers"])
    approval_id = await _register_l4_and_trigger(
        db_session, workspace, UUID(requirement["id"]), UUID(developer["user"]["id"])
    )
    return {
        "owner": owner,
        "developer": developer,
        "project": project,
        "requirement": requirement,
        "approval_id": str(approval_id),
    }


# ------------------------------------------------------- 自动创建


async def test_l4_tool_creates_a_pending_approval(
    client: AsyncClient, pending_approval: dict[str, Any]
) -> None:
    """审批记录由 Gateway 自动创建，reason 里带着「是什么工具、要改什么」。"""
    owner = pending_approval["owner"]
    requirement = pending_approval["requirement"]

    listing = (
        await client.get(
            f"{API}/requirements/{requirement['id']}/approvals",
            params={"status": "PENDING"},
            headers=owner["headers"],
        )
    ).json()

    assert listing["total"] == 1
    item = listing["items"][0]
    assert item["id"] == pending_approval["approval_id"]
    assert item["tool_name"] == "apply_patch"
    assert item["status"] == "PENDING"
    assert "apply_patch" in item["reason"]
    assert item["requested_by"] == pending_approval["developer"]["user"]["id"]
    assert item["reviewed_by"] is None


# ------------------------------------------------------- 批准 / 驳回


async def test_owner_can_approve(client: AsyncClient, pending_approval: dict[str, Any]) -> None:
    owner = pending_approval["owner"]

    response = await client.post(
        f"{API}/approvals/{pending_approval['approval_id']}/approve",
        json={"note": "改动范围可控，批准"},
        headers=owner["headers"],
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["status"] == "APPROVED"
    assert body["reviewed_by"] == owner["user"]["id"]
    assert body["review_note"] == "改动范围可控，批准"


async def test_owner_can_reject(client: AsyncClient, pending_approval: dict[str, Any]) -> None:
    owner = pending_approval["owner"]

    response = await client.post(
        f"{API}/approvals/{pending_approval['approval_id']}/reject",
        json={"note": "改得太多了，先拆小"},
        headers=owner["headers"],
    )

    assert response.status_code == 200
    assert response.json()["status"] == "REJECTED"


async def test_developer_cannot_approve_their_own_request(
    client: AsyncClient, pending_approval: dict[str, Any]
) -> None:
    """职责分离：Developer 触发的请求不能自己批 —— 否则审批形同虚设。"""
    developer = pending_approval["developer"]

    response = await client.post(
        f"{API}/approvals/{pending_approval['approval_id']}/approve",
        json={"note": "我自己觉得没问题"},
        headers=developer["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ROLE_REQUIRED"
    assert response.json()["error"]["details"]["required_roles"] == ["OWNER"]


async def test_already_decided_approval_cannot_be_decided_again(
    client: AsyncClient, pending_approval: dict[str, Any]
) -> None:
    """审批是一次性决定。改主意要重新发起，历史必须原样保留。"""
    owner = pending_approval["owner"]
    approval_id = pending_approval["approval_id"]

    first = await client.post(
        f"{API}/approvals/{approval_id}/approve",
        json={"note": "改动范围可控，批准"},
        headers=owner["headers"],
    )
    second = await client.post(
        f"{API}/approvals/{approval_id}/reject", json={"note": "反悔了"}, headers=owner["headers"]
    )

    assert first.status_code == 200
    assert second.status_code == 409
    assert second.json()["error"]["code"] == "APPROVAL_NOT_PENDING"

    # 历史没有被覆盖
    detail = (await client.get(f"{API}/approvals/{approval_id}", headers=owner["headers"])).json()
    assert detail["status"] == "APPROVED"
    assert detail["review_note"] == "改动范围可控，批准"


# ------------------------------------------------------- 过期（惰性判定）


async def test_expired_approval_is_marked_on_read(
    client: AsyncClient,
    db_session: AsyncSession,
    pending_approval: dict[str, Any],
) -> None:
    """过期的判定是惰性的：读取时发现 PENDING 已到期，就顺手落成 EXPIRED。"""
    from app.models.approval import Approval

    owner = pending_approval["owner"]
    approval_id = UUID(pending_approval["approval_id"])

    approval = await db_session.get(Approval, approval_id)
    approval.expires_at = datetime.now(UTC) - timedelta(hours=1)
    await db_session.commit()

    detail = (await client.get(f"{API}/approvals/{approval_id}", headers=owner["headers"])).json()
    assert detail["status"] == "EXPIRED"

    # Session.get 走身份映射缓存，会返回本会话刚才改过的旧对象 —— 必须 expire 再读
    db_session.expire_all()
    stored = await db_session.get(Approval, approval_id)
    assert stored.status == ApprovalStatus.EXPIRED.value


async def test_expired_approval_cannot_be_approved(
    client: AsyncClient,
    db_session: AsyncSession,
    pending_approval: dict[str, Any],
) -> None:
    from app.models.approval import Approval

    owner = pending_approval["owner"]
    approval_id = UUID(pending_approval["approval_id"])

    approval = await db_session.get(Approval, approval_id)
    approval.expires_at = datetime.now(UTC) - timedelta(minutes=5)
    await db_session.commit()

    response = await client.post(f"{API}/approvals/{approval_id}/approve", json={}, headers=owner["headers"])

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "APPROVAL_NOT_PENDING"


# ------------------------------------------------------------- 权限与边界


async def test_viewer_can_read_but_not_decide(
    client: AsyncClient,
    db_session: AsyncSession,
    workspace: Any,
    pending_approval: dict[str, Any],
    make_project_with_member: object,
    make_requirement: object,
) -> None:
    owner, project, viewer = await make_project_with_member(role="VIEWER")
    requirement = await make_requirement(project["id"], owner["headers"])
    approval_id = await _register_l4_and_trigger(
        db_session, workspace, UUID(requirement["id"]), UUID(owner["user"]["id"])
    )

    listed = await client.get(f"{API}/requirements/{requirement['id']}/approvals", headers=viewer["headers"])
    assert listed.status_code == 200
    assert listed.json()["total"] == 1

    denied = await client.post(
        f"{API}/approvals/{str(approval_id)}/approve", json={}, headers=viewer["headers"]
    )
    assert denied.status_code == 403


async def test_non_member_sees_nothing(client: AsyncClient, pending_approval: dict[str, Any]) -> None:
    outsider = await client.post(
        f"{API}/auth/register",
        json={
            "email": "outsider-approval@example.com",
            "password": "StrongPassword123!",
            "display_name": "o",
        },
    )
    assert outsider.status_code == 201
    login = await client.post(
        f"{API}/auth/login",
        json={"email": "outsider-approval@example.com", "password": "StrongPassword123!"},
    )
    headers = {"Authorization": f"Bearer {login.json()['access_token']}"}
    requirement = pending_approval["requirement"]

    assert (
        await client.get(f"{API}/requirements/{requirement['id']}/approvals", headers=headers)
    ).status_code == 403
    assert (
        await client.get(f"{API}/approvals/{pending_approval['approval_id']}", headers=headers)
    ).status_code == 403


async def test_requires_authentication(client: AsyncClient, pending_approval: dict[str, Any]) -> None:
    approval_id = pending_approval["approval_id"]

    assert (await client.get(f"{API}/approvals/{approval_id}")).status_code == 401
    assert (await client.post(f"{API}/approvals/{approval_id}/approve", json={})).status_code == 401


async def test_unknown_approval_returns_404(client: AsyncClient, make_actor: object) -> None:
    actor = await make_actor()

    response = await client.get(f"{API}/approvals/{uuid4()}", headers=actor["headers"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "APPROVAL_NOT_FOUND"
