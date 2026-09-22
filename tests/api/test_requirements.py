"""需求接口测试 —— 覆盖文档 §11 Stage 1 的验收条款。"""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient

from tests.conftest import API


async def test_create_requirement_starts_as_draft(
    client: AsyncClient, make_user: object, make_project: object, make_requirement: object
) -> None:
    """文档 §12.1：需求创建后 status = DRAFT。"""
    owner = await make_user()
    project = await make_project(owner["id"])
    requirement = await make_requirement(project["id"], owner["id"])

    assert requirement["status"] == "DRAFT"
    assert requirement["version"] == 1
    assert requirement["project_id"] == project["id"]
    assert requirement["priority"] == "P0"


async def test_acceptance_criteria_round_trips_through_db_column(
    client: AsyncClient, make_user: object, make_project: object, make_requirement: object
) -> None:
    """请求体里叫 acceptance_criteria，库里叫 acceptance_criteria_json，两端都要对。"""
    owner = await make_user()
    project = await make_project(owner["id"])
    criteria = ["可以创建 Todo", "可以删除 Todo"]

    requirement = await make_requirement(project["id"], owner["id"], acceptance_criteria=criteria)

    assert requirement["acceptance_criteria"] == criteria
    # 库里那个字段名不该泄漏到 API 上
    assert "acceptance_criteria_json" not in requirement
    assert requirement["prd"] is None
    assert "prd_json" not in requirement

    detail = (await client.get(f"{API}/requirements/{requirement['id']}")).json()
    assert detail["acceptance_criteria"] == criteria


async def test_requirement_detail_is_queryable_by_id(
    client: AsyncClient, make_user: object, make_project: object, make_requirement: object
) -> None:
    owner = await make_user()
    project = await make_project(owner["id"])
    requirement = await make_requirement(project["id"], owner["id"], title="实现 Todo API")

    response = await client.get(f"{API}/requirements/{requirement['id']}")

    assert response.status_code == 200
    assert response.json()["title"] == "实现 Todo API"


async def test_list_requirements_scoped_to_project(
    client: AsyncClient, make_user: object, make_project: object, make_requirement: object
) -> None:
    owner = await make_user()
    project_a = await make_project(owner["id"], name="Project A")
    project_b = await make_project(owner["id"], name="Project B")
    await make_requirement(project_a["id"], owner["id"], title="A-1")
    await make_requirement(project_a["id"], owner["id"], title="A-2")
    await make_requirement(project_b["id"], owner["id"], title="B-1")

    response = await client.get(f"{API}/projects/{project_a['id']}/requirements")

    body = response.json()
    assert body["total"] == 2
    assert {item["title"] for item in body["items"]} == {"A-1", "A-2"}


async def test_creating_requirement_on_missing_project_returns_404(
    client: AsyncClient, make_user: object
) -> None:
    user = await make_user()

    response = await client.post(
        f"{API}/projects/{uuid4()}/requirements",
        json={
            "title": "Orphan",
            "description": "no project",
            "priority": "P1",
            "created_by": user["id"],
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


async def test_unknown_creator_returns_404(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    owner = await make_user()
    project = await make_project(owner["id"])

    response = await client.post(
        f"{API}/projects/{project['id']}/requirements",
        json={
            "title": "Ghost",
            "description": "creator does not exist",
            "priority": "P1",
            "created_by": str(uuid4()),
        },
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


async def test_empty_title_returns_validation_error(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    owner = await make_user()
    project = await make_project(owner["id"])

    response = await client.post(
        f"{API}/projects/{project['id']}/requirements",
        json={"title": "", "description": "x", "priority": "P1", "created_by": owner["id"]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_invalid_priority_returns_validation_error(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    owner = await make_user()
    project = await make_project(owner["id"])

    response = await client.post(
        f"{API}/projects/{project['id']}/requirements",
        json={
            "title": "Bad priority",
            "description": "x",
            "priority": "URGENT",
            "created_by": owner["id"],
        },
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_content_update_bumps_version(
    client: AsyncClient, make_user: object, make_project: object, make_requirement: object
) -> None:
    owner = await make_user()
    project = await make_project(owner["id"])
    requirement = await make_requirement(project["id"], owner["id"])
    assert requirement["version"] == 1

    response = await client.patch(
        f"{API}/requirements/{requirement['id']}", json={"description": "改过的描述"}
    )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["description"] == "改过的描述"


async def test_priority_only_change_also_bumps_version(
    client: AsyncClient, make_user: object, make_project: object, make_requirement: object
) -> None:
    owner = await make_user()
    project = await make_project(owner["id"])
    requirement = await make_requirement(project["id"], owner["id"])

    response = await client.patch(f"{API}/requirements/{requirement['id']}", json={"priority": "P2"})

    assert response.json()["priority"] == "P2"
    assert response.json()["version"] == 2


async def test_status_cannot_be_set_through_patch(
    client: AsyncClient, make_user: object, make_project: object, make_requirement: object
) -> None:
    """文档 §3.3：不允许客户端通过一个请求直接把状态改成 COMPLETED。

    ``RequirementUpdate`` 里根本没有 status 字段，Pydantic 默认忽略未知字段，
    所以请求会成功但状态原封不动 —— 这正是我们要的行为。
    """
    owner = await make_user()
    project = await make_project(owner["id"])
    requirement = await make_requirement(project["id"], owner["id"])

    response = await client.patch(
        f"{API}/requirements/{requirement['id']}",
        json={"status": "COMPLETED", "title": "Renamed"},
    )

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "DRAFT"
    assert body["title"] == "Renamed"


async def test_empty_patch_does_not_bump_version(
    client: AsyncClient, make_user: object, make_project: object, make_requirement: object
) -> None:
    owner = await make_user()
    project = await make_project(owner["id"])
    requirement = await make_requirement(project["id"], owner["id"])

    response = await client.patch(f"{API}/requirements/{requirement['id']}", json={})

    assert response.status_code == 200
    assert response.json()["version"] == 1
