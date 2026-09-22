"""集成测试：验证数据真的落了库，且失败路径不会留下半成品。

这里刻意绕过 API 直接查表。只用响应体断言的话，「响应正确但没写库」
这类问题会被完全漏掉 —— 而那恰恰是分层架构里最容易出错的地方。
"""

from __future__ import annotations

from uuid import UUID

import pytest
from httpx import AsyncClient
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.project import Project, ProjectMember
from app.models.requirement import Requirement
from app.models.user import User
from tests.conftest import API


async def test_created_objects_are_persisted_and_rereadable(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: object,
    make_project: object,
    make_requirement: object,
) -> None:
    """文档 §11 Stage 1 验收：数据可以保存到数据库并重新读取。"""
    owner = await make_user(email="persist@example.com")
    project = await make_project(owner["id"], name="Persisted Project")
    requirement = await make_requirement(project["id"], owner["id"], title="Persisted Req")

    project_row = await db_session.get(Project, UUID(project["id"]))
    assert project_row is not None
    assert project_row.name == "Persisted Project"
    assert project_row.slug == "persisted-project"
    assert project_row.created_at is not None

    requirement_row = await db_session.get(Requirement, UUID(requirement["id"]))
    assert requirement_row is not None
    assert requirement_row.title == "Persisted Req"
    assert requirement_row.status == "DRAFT"
    assert requirement_row.version == 1
    assert requirement_row.acceptance_criteria_json == ["可以创建 Todo", "可以查询 Todo 列表"]
    # Stage 3 之前 PRD 必须保持为空，绝不能有半成品写进去
    assert requirement_row.prd_json is None

    via_api = (await client.get(f"{API}/requirements/{requirement['id']}")).json()
    assert via_api["title"] == "Persisted Req"
    assert via_api["acceptance_criteria"] == ["可以创建 Todo", "可以查询 Todo 列表"]


async def test_password_is_never_stored_in_plaintext(
    client: AsyncClient, db_session: AsyncSession, make_user: object
) -> None:
    user = await make_user(email="hash@example.com", password="SuperSecret123!")

    row = await db_session.get(User, UUID(user["id"]))

    assert row is not None
    assert row.password_hash != "SuperSecret123!"
    assert row.password_hash.startswith("$argon2")


async def test_duplicate_email_does_not_write_a_partial_row(
    client: AsyncClient, db_session: AsyncSession, make_user: object
) -> None:
    await make_user(email="only-once@example.com")
    before = (await db_session.execute(select(func.count()).select_from(User))).scalar_one()

    response = await client.post(
        f"{API}/users",
        json={"email": "only-once@example.com", "password": "StrongPass123!", "display_name": "Dup"},
    )

    assert response.status_code == 409
    after = (await db_session.execute(select(func.count()).select_from(User))).scalar_one()
    assert after == before


async def test_failed_project_creation_leaves_no_orphan_member(
    client: AsyncClient,
    db_session: AsyncSession,
    make_user: object,
    make_project: object,
) -> None:
    """slug 冲突时整体失败：项目不能建出来，成员行也不能偷偷留下。

    这是「事务边界归 Service」最容易出错的地方 —— 两次 add 只 commit 一次，
    顺序反了就会出现没有 Owner 的孤儿项目。
    """
    owner = await make_user()
    await make_project(owner["id"], slug="atomic-slug")

    members_before = (await db_session.execute(select(func.count()).select_from(ProjectMember))).scalar_one()

    response = await client.post(
        f"{API}/projects",
        json={"name": "Conflict", "slug": "atomic-slug", "owner_id": owner["id"]},
    )
    assert response.status_code == 409

    projects = (await db_session.execute(select(func.count()).select_from(Project))).scalar_one()
    members_after = (await db_session.execute(select(func.count()).select_from(ProjectMember))).scalar_one()

    assert projects == 1
    assert members_after == members_before


async def test_project_member_unique_constraint_is_enforced_by_database(
    client: AsyncClient, db_session: AsyncSession, make_user: object, make_project: object
) -> None:
    """绕过 Service 直接插重复成员，数据库的唯一约束必须挡住。

    Service 层的重复检查是「友好提示」，不是「正确性保证」—— 并发下靠的是
    UNIQUE(project_id, user_id)。
    """
    owner = await make_user()
    project = await make_project(owner["id"])
    duplicate = ProjectMember(project_id=UUID(project["id"]), user_id=UUID(owner["id"]), role="OWNER")

    db_session.add(duplicate)
    with pytest.raises(IntegrityError):
        await db_session.flush()
    await db_session.rollback()
