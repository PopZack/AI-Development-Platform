"""项目接口测试：CRUD + 资源级权限。

权限部分是这个提交的重点。核心验收来自文档 §11 Stage 2：
**「用户不能访问不属于自己的项目」**。
"""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient

from tests.conftest import API


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------- 创建与归属


async def test_create_project_makes_caller_the_owner(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    """文档 §3.2 流程 A：创建项目后当前用户成为 Owner。"""
    actor = await make_actor()
    project = await make_project(actor["headers"], name="Todo API Demo", slug="todo-api-demo")

    assert project["slug"] == "todo-api-demo"
    assert project["owner_id"] == actor["user"]["id"]
    assert project["status"] == "ACTIVE"

    members = (await client.get(f"{API}/projects/{project['id']}/members", headers=actor["headers"])).json()
    assert members["total"] == 1
    assert members["items"][0]["user_id"] == actor["user"]["id"]
    assert members["items"][0]["role"] == "OWNER"


async def test_owner_in_request_body_is_ignored(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    """请求体里塞 owner_id 不会生效 —— 归属权只由令牌决定。

    这不是「服务端刚好也读了令牌」，而是**字段本身已经被删掉**：
    保留它等于给任何登录用户一个「创建归属别人的项目」的入口。
    """
    actor = await make_actor()
    victim = await make_actor()

    response = await client.post(
        f"{API}/projects",
        json={"name": "Hijack Attempt", "owner_id": victim["user"]["id"]},
        headers=actor["headers"],
    )

    assert response.status_code == 201
    assert response.json()["owner_id"] == actor["user"]["id"]
    assert response.json()["owner_id"] != victim["user"]["id"]


async def test_create_project_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(f"{API}/projects", json={"name": "Anonymous"})

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_slug_is_generated_from_name_when_omitted(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    actor = await make_actor()
    project = await make_project(actor["headers"], name="Payment Gateway Refactor")

    assert project["slug"] == "payment-gateway-refactor"


async def test_non_ascii_name_falls_back_to_placeholder_slug(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    """中文项目名 slugify 后为空，必须回退成占位 slug，不能产生空 slug 或直接崩。"""
    actor = await make_actor()
    project = await make_project(actor["headers"], name="智全的线索平台")

    assert project["slug"] == "project"


async def test_placeholder_slug_gets_random_suffix_on_collision(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    actor = await make_actor()
    first = await make_project(actor["headers"], name="智全的线索平台")
    second = await make_project(actor["headers"], name="另一个中文项目")

    assert first["slug"] == "project"
    assert second["slug"].startswith("project-")
    assert second["slug"] != first["slug"]


async def test_duplicate_explicit_slug_returns_409(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    actor = await make_actor()
    await make_project(actor["headers"], slug="taken-slug")

    response = await client.post(
        f"{API}/projects",
        json={"name": "Other", "slug": "taken-slug"},
        headers=actor["headers"],
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "PROJECT_SLUG_TAKEN"
    assert body["error"]["details"]["slug"] == "taken-slug"


async def test_non_kebab_slug_returns_422(client: AsyncClient, make_actor: object) -> None:
    actor = await make_actor()

    response = await client.post(
        f"{API}/projects",
        json={"name": "Bad Slug", "slug": "Not_Kebab_Case"},
        headers=actor["headers"],
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_revoked_token_cannot_create_project(client: AsyncClient, make_actor: object) -> None:
    """登出后手上那张令牌连项目都建不了。

    账号「是否可用」是在认证层拦的（``authenticate_token`` 检查 status），
    所以业务层不需要再判一次 —— 能走到 Service 的 actor 一定是有效账号。
    """
    actor = await make_actor()
    await client.post(f"{API}/auth/logout", headers=actor["headers"])

    response = await client.post(f"{API}/projects", json={"name": "After logout"}, headers=actor["headers"])

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_REVOKED"


# ----------------------------------------------------------------- 列表范围


async def test_project_list_only_contains_my_projects(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    """文档 §11 Stage 2 验收：用户不能访问不属于自己的项目（列表侧）。"""
    alice = await make_actor()
    bob = await make_actor()
    await make_project(alice["headers"], name="Alice Project")
    await make_project(bob["headers"], name="Bob Project")

    alice_list = (await client.get(f"{API}/projects", headers=alice["headers"])).json()
    bob_list = (await client.get(f"{API}/projects", headers=bob["headers"])).json()

    assert alice_list["total"] == 1
    assert alice_list["items"][0]["name"] == "Alice Project"
    assert bob_list["total"] == 1
    assert bob_list["items"][0]["name"] == "Bob Project"


async def test_project_list_for_new_user_is_empty(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    owner = await make_actor()
    await make_project(owner["headers"])
    newcomer = await make_actor()

    response = await client.get(f"{API}/projects", headers=newcomer["headers"])

    assert response.status_code == 200
    assert response.json() == {"items": [], "total": 0}


async def test_project_list_ignores_owner_id_query_parameter(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    """Stage 1 的 ``?owner_id=`` 开关已删除，传了也没有任何效果。"""
    alice = await make_actor()
    bob = await make_actor()
    await make_project(alice["headers"], name="Alice Project")
    await make_project(bob["headers"], name="Bob Project")

    response = await client.get(
        f"{API}/projects", params={"owner_id": bob["user"]["id"]}, headers=alice["headers"]
    )

    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["name"] == "Alice Project"


async def test_project_list_requires_authentication(client: AsyncClient) -> None:
    assert (await client.get(f"{API}/projects")).status_code == 401


# ------------------------------------------------------------------- 越权


async def test_non_member_cannot_read_project(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    owner = await make_actor()
    project = await make_project(owner["headers"])
    outsider = await make_actor()

    response = await client.get(f"{API}/projects/{project['id']}", headers=outsider["headers"])

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "PROJECT_ACCESS_DENIED"
    assert body["error"]["details"]["project_id"] == project["id"]


async def test_non_member_cannot_read_members(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    owner = await make_actor()
    project = await make_project(owner["headers"])
    outsider = await make_actor()

    response = await client.get(f"{API}/projects/{project['id']}/members", headers=outsider["headers"])

    assert response.status_code == 403


async def test_unknown_project_returns_404(client: AsyncClient, make_actor: object) -> None:
    """不存在 → 404，不是 403。两者要能分清，否则排查时没法区分「ID 写错了」和「没权限」。"""
    actor = await make_actor()

    response = await client.get(f"{API}/projects/{uuid4()}", headers=actor["headers"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "PROJECT_NOT_FOUND"


async def test_member_can_read_project(client: AsyncClient, make_project_with_member: object) -> None:
    _, project, member = await make_project_with_member(role="DEVELOPER")

    response = await client.get(f"{API}/projects/{project['id']}", headers=member["headers"])

    assert response.status_code == 200
    assert response.json()["id"] == project["id"]


# --------------------------------------------------------------- 角色检查


async def test_developer_cannot_update_project(client: AsyncClient, make_project_with_member: object) -> None:
    _, project, developer = await make_project_with_member(role="DEVELOPER")

    response = await client.patch(
        f"{API}/projects/{project['id']}", json={"name": "Renamed"}, headers=developer["headers"]
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "PROJECT_ROLE_REQUIRED"
    assert body["error"]["details"]["actual_role"] == "DEVELOPER"
    assert body["error"]["details"]["required_roles"] == ["OWNER"]


async def test_viewer_cannot_update_project(client: AsyncClient, make_project_with_member: object) -> None:
    _, project, viewer = await make_project_with_member(role="VIEWER")

    response = await client.patch(
        f"{API}/projects/{project['id']}", json={"name": "Renamed"}, headers=viewer["headers"]
    )

    assert response.status_code == 403


async def test_owner_can_update_project(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    owner = await make_actor()
    project = await make_project(owner["headers"])

    response = await client.patch(
        f"{API}/projects/{project['id']}", json={"name": "Renamed"}, headers=owner["headers"]
    )

    assert response.status_code == 200
    assert response.json()["name"] == "Renamed"
    # slug 是对外标识，不允许被顺带改掉
    assert response.json()["slug"] == project["slug"]


async def test_developer_cannot_add_members(
    client: AsyncClient, make_project_with_member: object, make_actor: object
) -> None:
    _, project, developer = await make_project_with_member(role="DEVELOPER")
    newcomer = await make_actor()

    response = await client.post(
        f"{API}/projects/{project['id']}/members",
        json={"user_id": newcomer["user"]["id"], "role": "DEVELOPER"},
        headers=developer["headers"],
    )

    assert response.status_code == 403
    assert response.json()["error"]["code"] == "PROJECT_ROLE_REQUIRED"


async def test_owner_can_add_member_and_duplicate_is_rejected(
    client: AsyncClient, make_project_with_member: object
) -> None:
    owner, project, member = await make_project_with_member(role="DEVELOPER")

    duplicate = await client.post(
        f"{API}/projects/{project['id']}/members",
        json={"user_id": member["user"]["id"], "role": "DEVELOPER"},
        headers=owner["headers"],
    )

    assert duplicate.status_code == 409
    assert duplicate.json()["error"]["code"] == "PROJECT_MEMBER_EXISTS"


async def test_owner_role_cannot_be_granted_to_someone_else(
    client: AsyncClient, make_actor: object, make_project: object
) -> None:
    owner = await make_actor()
    project = await make_project(owner["headers"])
    other = await make_actor()

    response = await client.post(
        f"{API}/projects/{project['id']}/members",
        json={"user_id": other["user"]["id"], "role": "OWNER"},
        headers=owner["headers"],
    )

    assert response.status_code == 409
    assert response.json()["error"]["code"] == "OWNER_ROLE_MISMATCH"
