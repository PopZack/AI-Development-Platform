"""认证接口测试（设计文档 §7.1）。

覆盖三块：注册、登录、以及「会话撤销」——最后这块是这个提交的重点，
因为它决定了 ``/auth/logout`` 到底有没有真实语义。
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from httpx import AsyncClient

from app.config.settings import DEFAULT_JWT_SECRET, Settings
from app.infrastructure.auth.jwt import create_access_token
from tests.conftest import API, DEFAULT_PASSWORD


def _bearer(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# --------------------------------------------------------------------- 注册


async def test_register_returns_201_without_password_hash(client: AsyncClient, make_user: object) -> None:
    user = await make_user(email="alice@example.com", display_name="Alice")

    assert user["email"] == "alice@example.com"
    assert user["display_name"] == "Alice"
    assert user["status"] == "ACTIVE"
    assert "password" not in user
    assert "password_hash" not in user
    # token_version 属于内部撤销状态，不该出现在用户表示里
    assert "token_version" not in user


async def test_register_lowercases_email(client: AsyncClient, make_user: object) -> None:
    user = await make_user(email="MixedCase@Example.COM")
    assert user["email"] == "mixedcase@example.com"


async def test_duplicate_email_is_rejected_case_insensitively(client: AsyncClient, make_user: object) -> None:
    await make_user(email="dup@example.com")

    response = await client.post(
        f"{API}/auth/register",
        json={"email": "DUP@example.com", "password": DEFAULT_PASSWORD, "display_name": "Dup"},
    )

    assert response.status_code == 409
    body = response.json()
    assert body["error"]["code"] == "EMAIL_ALREADY_REGISTERED"
    assert body["error"]["details"]["email"] == "dup@example.com"


async def test_register_invalid_payload_returns_unified_error_body(client: AsyncClient) -> None:
    response = await client.post(
        f"{API}/auth/register",
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


# --------------------------------------------------------------------- 登录


async def test_login_returns_token_payload(client: AsyncClient, make_user: object) -> None:
    user = await make_user()

    response = await client.post(
        f"{API}/auth/login", json={"email": user["email"], "password": DEFAULT_PASSWORD}
    )

    assert response.status_code == 200
    body = response.json()
    assert body["token_type"] == "bearer"
    assert body["expires_in"] == 3600
    assert body["access_token"].count(".") == 2  # JWT 三段结构


async def test_login_accepts_email_case_insensitively(client: AsyncClient, make_user: object) -> None:
    await make_user(email="case@example.com")

    response = await client.post(
        f"{API}/auth/login", json={"email": "CASE@Example.com", "password": DEFAULT_PASSWORD}
    )

    assert response.status_code == 200


async def test_wrong_password_and_unknown_email_are_indistinguishable(
    client: AsyncClient, make_user: object
) -> None:
    """防账号枚举：两种失败必须给完全一样的 code 与 message。

    只要两者有差别，这个接口就成了「哪些邮箱已注册」的查询器。
    """
    user = await make_user()

    wrong_password = await client.post(
        f"{API}/auth/login", json={"email": user["email"], "password": "WrongPassword123!"}
    )
    unknown_email = await client.post(
        f"{API}/auth/login", json={"email": "nobody@example.com", "password": DEFAULT_PASSWORD}
    )

    assert wrong_password.status_code == 401
    assert unknown_email.status_code == 401

    # 只比 code 和 message：request_id 每个请求本来就不同，比整个响应体会假失败
    assert wrong_password.json()["error"]["code"] == unknown_email.json()["error"]["code"]
    assert wrong_password.json()["error"]["message"] == unknown_email.json()["error"]["message"]
    assert wrong_password.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_login_on_disabled_account_returns_401(
    client: AsyncClient, make_user: object, set_user_status: object
) -> None:
    """停用账号没法通过接口做到（见 test_users.py 的说明），所以直接改库构造。"""
    user = await make_user()
    await set_user_status(user["id"], "DISABLED")

    response = await client.post(
        f"{API}/auth/login", json={"email": user["email"], "password": DEFAULT_PASSWORD}
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"


async def test_disabling_an_account_kills_its_existing_tokens(
    client: AsyncClient, make_actor: object, set_user_status: object
) -> None:
    """停用账号比「撤销令牌」更狠：连还在有效期内的令牌都立刻不管用。

    这条分支只有在 ``authenticate_token`` 里检查 status 才成立 —— 如果只在登录时
    检查，已经登录的会话会一直有效到令牌自然过期。
    """
    actor = await make_actor()
    assert (await client.get(f"{API}/auth/me", headers=actor["headers"])).status_code == 200

    await set_user_status(actor["user"]["id"], "DISABLED")

    response = await client.get(f"{API}/auth/me", headers=actor["headers"])

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "ACCOUNT_DISABLED"


# ----------------------------------------------------------------- 当前用户


async def test_me_without_token_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"{API}/auth/me")

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "AUTHENTICATION_REQUIRED"


async def test_me_with_token_returns_current_user(client: AsyncClient, make_actor: object) -> None:
    actor = await make_actor()

    response = await client.get(f"{API}/auth/me", headers=actor["headers"])

    assert response.status_code == 200
    assert response.json()["id"] == actor["user"]["id"]


async def test_me_with_garbage_token_returns_401(client: AsyncClient) -> None:
    response = await client.get(f"{API}/auth/me", headers=_bearer("not-a-jwt"))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_INVALID"


async def test_me_with_token_signed_by_another_secret_returns_401(
    client: AsyncClient, make_user: object
) -> None:
    """签名不对就是无效令牌 —— 换个密钥自己签一个必须被拒。"""
    user = await make_user()
    forged, _ = create_access_token(
        user_id=UUID(user["id"]),
        token_version=0,
        settings=Settings(jwt_secret_key="attacker-chosen-secret-0123456789abcdef"),
    )

    response = await client.get(f"{API}/auth/me", headers=_bearer(forged))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_INVALID"


async def test_me_with_expired_token_returns_401_with_token_expired(
    client: AsyncClient, make_user: object
) -> None:
    user = await make_user()
    expired, _ = create_access_token(
        user_id=UUID(user["id"]),
        token_version=0,
        settings=Settings(access_token_expire_minutes=-1),
    )

    response = await client.get(f"{API}/auth/me", headers=_bearer(expired))

    assert response.status_code == 401
    # 区分 TOKEN_EXPIRED 和 TOKEN_INVALID：前者让客户端去重新登录，
    # 后者通常意味着被篡改，值得告警
    assert response.json()["error"]["code"] == "TOKEN_EXPIRED"


async def test_default_secret_is_rejected_in_production() -> None:
    """配置护栏本身在 test_jwt.py 里单测；这里只确认通过 HTTP 起不来。

    其实更准确的说法是：这条护栏在 ``create_app`` 之前就会抛，属于配置层，
    不属于接口层，所以这里只做一次存在性确认。
    """
    with pytest.raises(ValueError, match="JWT_SECRET_KEY"):
        Settings(app_env="prod", jwt_secret_key=DEFAULT_JWT_SECRET)


# --------------------------------------------------------------- 会话撤销


async def test_logout_revokes_the_token_immediately(client: AsyncClient, make_actor: object) -> None:
    """这是 token_version 方案的核心断言：登出后**旧令牌立刻不可用**。

    换成「只管过期时间」的方案，这里就会失败 —— 令牌还能一直用到过期为止。
    """
    actor = await make_actor()
    headers = actor["headers"]

    before = await client.get(f"{API}/auth/me", headers=headers)
    assert before.status_code == 200

    logout = await client.post(f"{API}/auth/logout", headers=headers)
    assert logout.status_code == 200
    assert logout.json() == {"revoked": True, "token_version": 1}

    after = await client.get(f"{API}/auth/me", headers=headers)
    assert after.status_code == 401
    assert after.json()["error"]["code"] == "TOKEN_REVOKED"


async def test_login_again_after_logout_works(client: AsyncClient, make_actor: object) -> None:
    """撤销不是封号：重新登录拿到的新令牌必须可用。"""
    actor = await make_actor()
    await client.post(f"{API}/auth/logout", headers=actor["headers"])

    response = await client.post(
        f"{API}/auth/login", json={"email": actor["user"]["email"], "password": DEFAULT_PASSWORD}
    )
    assert response.status_code == 200

    fresh = _bearer(response.json()["access_token"])
    assert (await client.get(f"{API}/auth/me", headers=fresh)).status_code == 200


async def test_logout_requires_authentication(client: AsyncClient) -> None:
    response = await client.post(f"{API}/auth/logout")

    assert response.status_code == 401


async def test_logout_kicks_every_session_not_just_the_current_one(
    client: AsyncClient, make_actor: object
) -> None:
    """粒度说明：撤销是「用户级」的，同一账号的另一个设备的令牌也会一起失效。"""
    actor = await make_actor()
    email = actor["user"]["email"]

    other_device = await client.post(f"{API}/auth/login", json={"email": email, "password": DEFAULT_PASSWORD})
    other_headers = _bearer(other_device.json()["access_token"])
    assert (await client.get(f"{API}/auth/me", headers=other_headers)).status_code == 200

    await client.post(f"{API}/auth/logout", headers=actor["headers"])

    assert (await client.get(f"{API}/auth/me", headers=other_headers)).status_code == 401


async def test_unknown_token_subject_returns_401(client: AsyncClient) -> None:
    """用户被删掉后，手上还捏着的令牌必须失效。"""
    token, _ = create_access_token(user_id=uuid4(), token_version=0, settings=Settings(app_env="test"))

    response = await client.get(f"{API}/auth/me", headers=_bearer(token))

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "TOKEN_SUBJECT_NOT_FOUND"


# --------------------------------------------------------------- 修改密码


async def test_change_password_revokes_all_sessions(client: AsyncClient, make_actor: object) -> None:
    """token_version 方案真正值钱的地方：改密码能把攻击者立刻踢出去。"""
    actor = await make_actor()
    headers = actor["headers"]

    response = await client.post(
        f"{API}/auth/password",
        headers=headers,
        json={"current_password": DEFAULT_PASSWORD, "new_password": "BrandNewPass456!"},
    )

    assert response.status_code == 200
    assert response.json() == {"revoked": True, "token_version": 1}

    # 旧令牌作废
    assert (await client.get(f"{API}/auth/me", headers=headers)).status_code == 401

    # 旧密码不再能登录，新密码可以
    old_password = await client.post(
        f"{API}/auth/login", json={"email": actor["user"]["email"], "password": DEFAULT_PASSWORD}
    )
    assert old_password.status_code == 401

    new_password = await client.post(
        f"{API}/auth/login",
        json={"email": actor["user"]["email"], "password": "BrandNewPass456!"},
    )
    assert new_password.status_code == 200


async def test_change_password_with_wrong_current_password_returns_401(
    client: AsyncClient, make_actor: object
) -> None:
    actor = await make_actor()

    response = await client.post(
        f"{API}/auth/password",
        headers=actor["headers"],
        json={"current_password": "WrongPassword123!", "new_password": "BrandNewPass456!"},
    )

    assert response.status_code == 401
    assert response.json()["error"]["code"] == "INVALID_CREDENTIALS"


async def test_change_password_to_the_same_value_returns_422(client: AsyncClient, make_actor: object) -> None:
    actor = await make_actor()

    response = await client.post(
        f"{API}/auth/password",
        headers=actor["headers"],
        json={"current_password": DEFAULT_PASSWORD, "new_password": DEFAULT_PASSWORD},
    )

    assert response.status_code == 422
    assert response.json()["error"]["code"] == "PASSWORD_UNCHANGED"


async def test_change_password_rejects_too_short_new_password(
    client: AsyncClient, make_actor: object
) -> None:
    actor = await make_actor()

    response = await client.post(
        f"{API}/auth/password",
        headers=actor["headers"],
        json={"current_password": DEFAULT_PASSWORD, "new_password": "short"},
    )

    assert response.status_code == 422
