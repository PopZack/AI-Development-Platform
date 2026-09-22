"""用户查询与修改接口测试。

注册相关用例在 ``test_auth.py``。

权限规则（本模块是 Stage 2 收尾补的鉴权）：
- 三个接口都要求登录
- ``PATCH`` 只允许改自己，且只能改 ``display_name``
"""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient

from tests.conftest import API


async def test_user_endpoints_require_authentication(client: AsyncClient) -> None:
    assert (await client.get(f"{API}/users")).status_code == 401
    assert (await client.get(f"{API}/users/{uuid4()}")).status_code == 401
    assert (await client.patch(f"{API}/users/{uuid4()}", json={"display_name": "x"})).status_code == 401


async def test_list_users_reports_total(client: AsyncClient, make_actor: object, make_user: object) -> None:
    actor = await make_actor()
    await make_user()

    response = await client.get(f"{API}/users", headers=actor["headers"])

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2


async def test_get_own_detail(client: AsyncClient, make_actor: object) -> None:
    actor = await make_actor()

    response = await client.get(f"{API}/users/{actor['user']['id']}", headers=actor["headers"])

    assert response.status_code == 200
    assert response.json()["id"] == actor["user"]["id"]


async def test_get_other_user_detail_is_allowed(client: AsyncClient, make_actor: object) -> None:
    """别人的详情可以看 —— 「添加项目成员」需要先查到对方 id。

    这是一个**已知取舍**：任何登录用户都能看到全部用户的邮箱。在本地协作平台里
    可接受；要对外则应收紧成「只能看到与你有共同项目的人」或对其他人的 email 脱敏。
    """
    viewer = await make_actor()
    other = await make_actor()

    response = await client.get(f"{API}/users/{other['user']['id']}", headers=viewer["headers"])

    assert response.status_code == 200
    assert response.json()["id"] == other["user"]["id"]


async def test_unknown_user_returns_404_with_specific_code(client: AsyncClient, make_actor: object) -> None:
    actor = await make_actor()

    response = await client.get(f"{API}/users/{uuid4()}", headers=actor["headers"])

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


async def test_malformed_uuid_returns_422(client: AsyncClient, make_actor: object) -> None:
    actor = await make_actor()

    response = await client.get(f"{API}/users/not-a-uuid", headers=actor["headers"])

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_update_own_display_name(client: AsyncClient, make_actor: object) -> None:
    actor = await make_actor()

    response = await client.patch(
        f"{API}/users/{actor['user']['id']}",
        json={"display_name": "Renamed"},
        headers=actor["headers"],
    )

    assert response.status_code == 200
    assert response.json()["display_name"] == "Renamed"


async def test_cannot_update_someone_else(client: AsyncClient, make_actor: object) -> None:
    """改别人的资料必须被后端拒掉，而不是靠前端不显示按钮。"""
    attacker = await make_actor()
    victim = await make_actor()

    response = await client.patch(
        f"{API}/users/{victim['user']['id']}",
        json={"display_name": "Hijacked"},
        headers=attacker["headers"],
    )

    assert response.status_code == 403
    body = response.json()
    assert body["error"]["code"] == "USER_SELF_ONLY"
    assert body["error"]["details"]["target_user_id"] == victim["user"]["id"]

    # 受害者的名字必须没被动过
    unchanged = await client.get(f"{API}/users/{victim['user']['id']}", headers=attacker["headers"])
    assert unchanged.json()["display_name"] == victim["user"]["display_name"]


async def test_status_cannot_be_changed_through_patch(client: AsyncClient, make_actor: object) -> None:
    """``UserUpdate`` 里没有 status 字段，所以停了不了任何账号（包括自己）。

    停用账号没有 API 入口是刻意的：文档只定义了项目级角色，没有全局管理员。
    """
    actor = await make_actor()

    response = await client.patch(
        f"{API}/users/{actor['user']['id']}",
        json={"status": "DISABLED", "display_name": "Still Active"},
        headers=actor["headers"],
    )

    assert response.status_code == 200
    assert response.json()["status"] == "ACTIVE"
    assert response.json()["display_name"] == "Still Active"
