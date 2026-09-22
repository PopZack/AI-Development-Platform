"""项目接口测试。"""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient

from tests.conftest import API


async def test_create_project_makes_creator_the_owner(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    """文档 §3.2 流程 A：创建项目后当前用户成为 Owner。"""
    owner = await make_user()
    project = await make_project(owner["id"], name="Todo API Demo", slug="todo-api-demo")

    assert project["slug"] == "todo-api-demo"
    assert project["owner_id"] == owner["id"]
    assert project["status"] == "ACTIVE"

    members = (await client.get(f"{API}/projects/{project['id']}/members")).json()
    assert members["total"] == 1
    assert members["items"][0]["user_id"] == owner["id"]
    assert members["items"][0]["role"] == "OWNER"


async def test_slug_is_generated_from_name_when_omitted(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    owner = await make_user()
    project = await make_project(owner["id"], name="Payment Gateway Refactor")

    assert project["slug"] == "payment-gateway-refactor"


async def test_non_ascii_name_falls_back_to_placeholder_slug(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    """中文项目名 slugify 后为空，必须回退成占位 slug，不能产生空 slug 或直接崩。"""
    owner = await make_user()
    project = await make_project(owner["id"], name="智全的线索平台")

    assert project["slug"] == "project"


async def test_placeholder_slug_gets_random_suffix_on_collision(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    """第二个同样取不到 slug 的项目必须能建成功，而不是报 slug 重复。"""
    owner = await make_user()
    first = await make_project(owner["id"], name="智全的线索平台")
    second = await make_project(owner["id"], name="另一个中文项目")

    assert first["slug"] == "project"
    assert second["slug"].startswith("project-")
    assert second["slug"] != first["slug"]


async def test_two_projects_with_same_name_get_distinct_slugs(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    owner = await make_user()
    first = await make_project(owner["id"], name="Same Name")
    second = await make_project(owner["id"], name="Same Name")

    assert first["slug"] != second["slug"]


async def test_duplicate_explicit_slug_returns_409(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    owner = await make_user()
    await make_project(owner["id"], slug="taken-slug")

    response = await client.post(
        f"{API}/projects", json={"name": "Other", "slug": "taken-slug", "owner_id": owner["id"]}
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "PROJECT_SLUG_TAKEN"
    assert body["error"]["details"]["slug"] == "taken-slug"


async def test_non_kebab_slug_returns_422(client: AsyncClient, make_user: object) -> None:
    owner = await make_user()

    response = await client.post(
        f"{API}/projects",
        json={"name": "Bad Slug", "slug": "Not_Kebab_Case", "owner_id": owner["id"]},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_unknown_owner_returns_404(client: AsyncClient) -> None:
    response = await client.post(f"{API}/projects", json={"name": "Orphan", "owner_id": str(uuid4())})

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


async def test_disabled_owner_is_rejected(client: AsyncClient, make_user: object) -> None:
    owner = await make_user()
    await client.patch(f"{API}/users/{owner['id']}", json={"status": "DISABLED"})

    response = await client.post(f"{API}/projects", json={"name": "Blocked", "owner_id": owner["id"]})

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OWNER_NOT_ACTIVE"


async def test_list_projects_can_filter_by_member(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    owner = await make_user()
    outsider = await make_user()
    await make_project(owner["id"], name="Mine")

    mine = (await client.get(f"{API}/projects", params={"owner_id": owner["id"]})).json()
    theirs = (await client.get(f"{API}/projects", params={"owner_id": outsider["id"]})).json()

    assert mine["total"] == 1
    assert theirs["total"] == 0


async def test_add_member_and_reject_duplicate(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    owner = await make_user()
    collaborator = await make_user()
    project = await make_project(owner["id"])

    created = await client.post(
        f"{API}/projects/{project['id']}/members",
        json={"user_id": collaborator["id"], "role": "DEVELOPER"},
    )
    assert created.status_code == 201
    assert created.json()["role"] == "DEVELOPER"

    duplicate = await client.post(
        f"{API}/projects/{project['id']}/members",
        json={"user_id": collaborator["id"], "role": "DEVELOPER"},
    )
    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "PROJECT_MEMBER_EXISTS"


async def test_owner_role_cannot_be_granted_to_someone_else(
    client: AsyncClient, make_user: object, make_project: object
) -> None:
    owner = await make_user()
    other = await make_user()
    project = await make_project(owner["id"])

    response = await client.post(
        f"{API}/projects/{project['id']}/members",
        json={"user_id": other["id"], "role": "OWNER"},
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OWNER_ROLE_MISMATCH"


async def test_update_project_name(client: AsyncClient, make_user: object, make_project: object) -> None:
    owner = await make_user()
    project = await make_project(owner["id"])

    response = await client.patch(f"{API}/projects/{project['id']}", json={"name": "Renamed"})

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    # slug 是对外标识，不允许被顺带改掉
    assert response.json()["slug"] == project["slug"]
