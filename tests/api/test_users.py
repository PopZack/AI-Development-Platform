"""用户查询与修改接口测试。

注册相关用例已挪到 ``test_auth.py`` —— 它们验的是认证链路，
不是用户资源的读写。

本模块的接口目前**尚未强制认证**（给业务路由加认证与资源级权限是 Stage 2 的
下一个提交），所以这里暂时不带 Authorization 头。下一个提交会把它们补上。
"""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient

from tests.conftest import API


async def test_unknown_user_returns_404_with_specific_code(client: AsyncClient, make_user: object) -> None:
    response = await client.get(f"{API}/users/{uuid4()}")

    assert response.status_code == 404
    assert response.json()["error"]["code"] == "USER_NOT_FOUND"


async def test_malformed_uuid_returns_422(client: AsyncClient) -> None:
    response = await client.get(f"{API}/users/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


async def test_update_user_display_name(client: AsyncClient, make_user: object) -> None:
    user = await make_user()

    response = await client.patch(f"{API}/users/{user['id']}", json={"display_name": "Renamed"})

    assert response.status_code == 200
    assert response.json()["display_name"] == "Renamed"


async def test_list_users_reports_total(client: AsyncClient, make_user: object) -> None:
    await make_user()
    await make_user()

    response = await client.get(f"{API}/users")

    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert len(body["items"]) == 2
