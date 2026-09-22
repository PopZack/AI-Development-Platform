"""需求接口测试：CRUD + 状态/版本规则 + 资源级权限。"""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient

from tests.conftest import API

# --------------------------------------------------------------- 创建与归属


async def test_create_requirement_starts_as_draft(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """文档 §12.1：需求创建后 status = DRAFT。"""
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])

    assert requirement["status"] == "DRAFT"
    assert requirement["version"] == 1
    assert requirement["project_id"] == project["id"]
    assert requirement["priority"] == "P0"


async def test_created_by_comes_from_token_not_request_body(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    """请求体里塞 created_by 不会生效 —— 这个字段已经被删掉，否则等于允许冒名。"""
    actor = await make_actor()
    victim = await make_actor()
    project = await make_project(actor["headers"])

    response = await client.post(
        f"{API}/projects/{project['id']}/requirements",
        json={
            "title": "冒名需求",
            "description": "试图把创建人伪造成别人",
            "created_by": victim["user"]["id"],
        },
        headers=actor["headers"],
    )

    assert response.status_code == 201
    assert response.json()["created_by"] == actor["user"]["id"]
    assert response.json()["created_by"] != victim["user"]["id"]


async def test_requirement_without_acceptance_criteria_reads_back_as_empty_list(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    """回归测试：库里 ``acceptance_criteria_json`` 为 NULL 时，响应必须是 ``[]``。

    这是 Stage 1 就埋下的 bug：建一条不带验收标准的需求，再读它就会 500 ——
    响应模型要求 ``list[str]``，而数据库给的是 ``None``。
    之所以一直没暴露，是因为 Stage 1 的用例**全都显式传了验收标准**，
    恰好绕开了这条路径。
    """
    actor = await make_actor()
    project = await make_project(actor["headers"])

    created = await client.post(
        f"{API}/projects/{project['id']}/requirements",
        json={"title": "没有验收标准", "description": "只给标题和描述"},
        headers=actor["headers"],
    )
    assert created.status_code == 201
    assert created.json()["acceptance_criteria"] == []

    detail = await client.get(f"{API}/requirements/{created.json()['id']}", headers=actor["headers"])
    assert detail.status_code == 200
    assert detail.json()["acceptance_criteria"] == []


async def test_create_requirement_requires_authentication(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    owner = await make_actor()
    project = await make_project(owner["headers"])

    response = await client.post(
        f"{API}/projects/{project['id']}/requirements",
        json={"title": "Anonymous", "description": "no token"},
    )

    assert response.status_code == 401


async def test_acceptance_criteria_round_trips_through_db_column(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """请求体里叫 acceptance_criteria，库里叫 acceptance_criteria_json，两端都要对。"""
    actor = await make_actor()
    project = await make_project(actor["headers"])
    criteria = ["可以创建 Todo", "可以删除 Todo"]

    requirement = await make_requirement(project["id"], actor["headers"], acceptance_criteria=criteria)

    assert requirement["acceptance_criteria"] == criteria
    assert "acceptance_criteria_json" not in requirement
    assert requirement["prd"] is None
    assert "prd_json" not in requirement

    detail = (await client.get(f"{API}/requirements/{requirement['id']}", headers=actor["headers"])).json()
    assert detail["acceptance_criteria"] == criteria


async def test_creating_requirement_on_missing_project_returns_404(
    client: AsyncClient, make_actor: object
) -> None:
    actor = await make_actor()

    response = await client.post(
        f"{API}/projects/{uuid4()}/requirements",
        json={"title": "Orphan", "description": "no project", "priority": "P1"},
        headers=actor["headers"],
    )

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


async def test_empty_title_returns_validation_error(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    actor = await make_actor()
    project = await make_project(actor["headers"])

    response = await client.post(
        f"{API}/projects/{project['id']}/requirements",
        json={"title": "", "description": "x", "priority": "P1"},
        headers=actor["headers"],
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_invalid_priority_returns_validation_error(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    actor = await make_actor()
    project = await make_project(actor["headers"])

    response = await client.post(
        f"{API}/projects/{project['id']}/requirements",
        json={"title": "Bad priority", "description": "x", "priority": "URGENT"},
        headers=actor["headers"],
    )

    assert response.status_code == 422


# ------------------------------------------------------------- 权限（写）


async def test_non_member_cannot_create_requirement(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    owner = await make_actor()
    project = await make_project(owner["headers"])
    outsider = await make_actor()

    response = await client.post(
        f"{API}/projects/{project['id']}/requirements",
        json={"title": "Sneaky", "description": "not my project"},
        headers=outsider["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"


async def test_viewer_can_read_but_cannot_create_requirement(
    client: AsyncClient, make_project_with_member: object
) -> None:
    """VIEWER 是只读角色：能看到项目内容，但不能往里写。"""
    _, project, viewer = await make_project_with_member(role="VIEWER")

    assert (await client.get(f"{API}/projects/{project['id']}", headers=viewer["headers"])).status_code == 200
    assert (
        await client.get(f"{API}/projects/{project['id']}/requirements", headers=viewer["headers"])
    ).status_code == 200

    response = await client.post(
        f"{API}/projects/{project['id']}/requirements",
        json={"title": "Viewer attempt", "description": "should be denied"},
        headers=viewer["headers"],
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "PROJECT_ROLE_REQUIRED"
    assert body["error"]["details"]["actual_role"] == "VIEWER"
    assert body["error"]["details"]["required_roles"] == ["DEVELOPER", "OWNER"]


async def test_viewer_cannot_update_requirement(
    client: AsyncClient, make_actor: object, make_project_with_member: object, make_requirement: object
) -> None:
    owner, project, viewer = await make_project_with_member(role="VIEWER")
    requirement = await make_requirement(project["id"], owner["headers"])

    response = await client.patch(
        f"{API}/requirements/{requirement['id']}",
        json={"title": "Viewer edit"},
        headers=viewer["headers"],
    )

    assert response.status_code == 403


async def test_developer_can_create_and_update_requirement(
    client: AsyncClient, make_project_with_member: object, make_requirement: object
) -> None:
    _, project, developer = await make_project_with_member(role="DEVELOPER")

    requirement = await make_requirement(project["id"], developer["headers"])
    assert requirement["created_by"] == developer["user"]["id"]

    response = await client.patch(
        f"{API}/requirements/{requirement['id']}",
        json={"priority": "P1"},
        headers=developer["headers"],
    )

    assert response.status_code == 200
    assert response.json()["priority"] == "P1"


async def test_non_member_cannot_read_requirement(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """路径里只有 requirement_id，权限要顺着它所属的项目判 —— 这正是
    「权限检查放 Service 而不是依赖」的原因。"""
    owner = await make_actor()
    project = await make_project(owner["headers"])
    requirement = await make_requirement(project["id"], owner["headers"])
    outsider = await make_actor()

    response = await client.get(f"{API}/requirements/{requirement['id']}", headers=outsider["headers"])

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ACCESS_DENIED"


async def test_non_member_cannot_list_project_requirements(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    owner = await make_actor()
    project = await make_project(owner["headers"])
    outsider = await make_actor()

    response = await client.get(f"{API}/projects/{project['id']}/requirements", headers=outsider["headers"])

    assert response.status_code == 403


async def test_unknown_requirement_returns_404_not_403(client: AsyncClient, make_actor: object) -> None:
    actor = await make_actor()

    response = await client.get(f"{API}/requirements/{uuid4()}", headers=actor["headers"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "REQUIREMENT_NOT_FOUND"


# --------------------------------------------------------- 版本与状态规则


async def test_list_requirements_scoped_to_project(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    actor = await make_actor()
    project_a = await make_project(actor["headers"], name="Project A")
    project_b = await make_project(actor["headers"], name="Project B")
    await make_requirement(project_a["id"], actor["headers"], title="A-1")
    await make_requirement(project_a["id"], actor["headers"], title="A-2")
    await make_requirement(project_b["id"], actor["headers"], title="B-1")

    response = await client.get(f"{API}/projects/{project_a['id']}/requirements", headers=actor["headers"])

    body = response.json()
    assert body["total"] == 2
    assert {item["title"] for item in body["items"]} == {"A-1", "A-2"}


async def test_content_update_bumps_version(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])
    assert requirement["version"] == 1

    response = await client.patch(
        f"{API}/requirements/{requirement['id']}",
        json={"description": "改过的描述"},
        headers=actor["headers"],
    )

    assert response.status_code == 200
    assert response.json()["version"] == 2
    assert response.json()["description"] == "改过的描述"


async def test_status_cannot_be_set_through_patch(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    """文档 §3.3：不允许客户端通过一个请求直接把状态改成 COMPLETED。

    ``RequirementUpdate`` 里根本没有 status 字段，Pydantic 默认忽略未知字段，
    所以请求会成功但状态原封不动 —— 这正是我们要的行为。
    """
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])

    response = await client.patch(
        f"{API}/requirements/{requirement['id']}",
        json={"status": "COMPLETED", "title": "Renamed"},
        headers=actor["headers"],
    )

    assert response.status_code == 200
    assert response.json()["status"] == "DRAFT"
    assert response.json()["title"] == "Renamed"


async def test_empty_patch_does_not_bump_version(
    client: AsyncClient, make_actor: object, make_project: object, make_requirement: object
) -> None:
    actor = await make_actor()
    project = await make_project(actor["headers"])
    requirement = await make_requirement(project["id"], actor["headers"])

    response = await client.patch(
        f"{API}/requirements/{requirement['id']}", json={}, headers=actor["headers"]
    )

    assert response.status_code == 200
    assert response.json()["version"] == 1
