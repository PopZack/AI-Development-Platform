"""用户接口测试。"""

from __future__ import annotations

from uuid import uuid4

from httpx import AsyncClient

from tests.conftest import API, DEFAULT_PASSWORD


async def test_create_user_returns_201_without_password_hash(client: AsyncClient, make_user: object) -> None:
    user = await make_user(email="alice@example.com", display_name="Alice")

    assert user["email"] == "alice@example.com"
    assert user["display_name"] == "Alice"
    assert user["status"] == "ACTIVE"
    # 哈希绝不能出现在响应里
    assert "password" not in user
    assert "password_hash" not in user


async def test_create_user_lowercases_email(client: AsyncClient, make_user: object) -> None:
    user = await make_user(email="MixedCase@Example.COM")
    assert user["email"] == "mixedcase@example.com"


async def test_duplicate_email_is_rejected_case_insensitively(client: AsyncClient, make_user: object) -> None:
    await make_user(email="dup@example.com")

    response = await client.post(
        f"{API}/users",
        json={"email": "DUP@example.com", "password": DEFAULT_PASSWORD, "display_name": "Dup"},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
    assert body["error"]["details"]["email"] == "dup@example.com"


async def test_invalid_payload_returns_unified_error_body(client: AsyncClient) -> None:
    response = await client.post(
        f"{API}/users",
        json={"email": "not-an-email", "password": "short", "display_name": ""},
    )

    assert response.status_code == 422
    body = response.json()

    # 文档 §7：错误体必须是 {"error": {code, message, request_id, details}}
    assert set(body.keys()) == {"error"}
    error = body["error"]
    assert error["code"] == "VALIDATION_ERROR"
    assert error["request_id"].startswith("req-")
    assert "errors" in error["details"]

    # request_id 必须和响应头对齐，否则线上没法用 header 去捞日志
    assert response.headers["X-Request-ID"] == error["request_id"]


async def test_unknown_user_returns_404_with_specific_code(client: AsyncClient) -> None:
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
