"""JWT 签发与校验的单元测试。

不用 HTTP，直接测基础设施层 —— 这样失败时能立刻区分「令牌本身的问题」
和「路由/依赖装配的问题」。
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import jwt as pyjwt
import pytest

from app.common.exceptions import AuthenticationError
from app.config.settings import DEFAULT_JWT_SECRET, MIN_JWT_SECRET_BYTES, Settings
from app.infrastructure.auth.jwt import create_access_token, decode_access_token


def _settings(**overrides: object) -> Settings:
    # 密钥刻意给足 32 字节：短密钥会被 PyJWT 判为不安全（InsecureKeyLengthWarning），
    # 那批警告会让真正的失败信息被淹没
    base: dict[str, object] = {
        "app_env": "test",
        "jwt_secret_key": "unit-test-secret-0123456789abcdef",
        "jwt_algorithm": "HS256",
        "access_token_expire_minutes": 60,
    }
    base.update(overrides)
    return Settings(**base)


def test_round_trip_preserves_identity_and_version() -> None:
    settings = _settings()
    user_id = uuid4()

    token, expires_in = create_access_token(user_id=user_id, token_version=7, settings=settings)

    payload = decode_access_token(token, settings)
    assert payload.user_id == user_id
    assert payload.token_version == 7
    assert payload.token_id  # jti 不能为空，否则日志里分辨不出是哪个令牌
    assert expires_in == 3600


def test_expires_at_is_about_one_hour_out() -> None:
    settings = _settings()
    before = datetime.now(UTC)

    token, _ = create_access_token(user_id=uuid4(), token_version=0, settings=settings)
    payload = decode_access_token(token, settings)

    # JWT 的 exp 是整秒精度，给 5 秒容差
    assert before + timedelta(minutes=60) - timedelta(seconds=5) <= payload.expires_at
    assert payload.expires_at <= datetime.now(UTC) + timedelta(minutes=60) + timedelta(seconds=5)


def test_expired_token_raises_token_expired() -> None:
    settings = _settings(access_token_expire_minutes=-1)

    token, _ = create_access_token(user_id=uuid4(), token_version=0, settings=settings)

    with pytest.raises(AuthenticationError) as excinfo:
        decode_access_token(token, settings)

    assert excinfo.value.status_code == 401
    assert excinfo.value.code == "TOKEN_EXPIRED"


def test_token_signed_with_another_secret_is_rejected() -> None:
    token, _ = create_access_token(user_id=uuid4(), token_version=0, settings=_settings())

    with pytest.raises(AuthenticationError) as excinfo:
        decode_access_token(token, _settings(jwt_secret_key="some-other-secret-0123456789abcdef"))

    assert excinfo.value.code == "TOKEN_INVALID"


def test_tampered_token_is_rejected() -> None:
    token, _ = create_access_token(user_id=uuid4(), token_version=0, settings=_settings())
    header, payload, signature = token.split(".")
    tampered = f"{header}.{payload}.{signature[:-2]}xx"

    with pytest.raises(AuthenticationError) as excinfo:
        decode_access_token(tampered, _settings())

    assert excinfo.value.code == "TOKEN_INVALID"


def test_token_without_version_claim_is_rejected() -> None:
    """少了 ver 就无法判断是否被撤销 —— 必须当成无效令牌，不能默认放行。"""
    settings = _settings()
    now = datetime.now(UTC)
    token = pyjwt.encode(
        {
            "sub": str(uuid4()),
            "jti": "x",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(AuthenticationError) as excinfo:
        decode_access_token(token, settings)

    assert excinfo.value.code == "TOKEN_INVALID"


def test_token_with_non_uuid_subject_is_rejected() -> None:
    settings = _settings()
    now = datetime.now(UTC)
    token = pyjwt.encode(
        {
            "sub": "not-a-uuid",
            "ver": 0,
            "jti": "x",
            "iat": int(now.timestamp()),
            "exp": int((now + timedelta(minutes=5)).timestamp()),
        },
        settings.jwt_secret_key,
        algorithm=settings.jwt_algorithm,
    )

    with pytest.raises(AuthenticationError) as excinfo:
        decode_access_token(token, settings)

    assert excinfo.value.code == "TOKEN_INVALID"


# --------------------------------------------------------------- 配置护栏


def test_default_secret_satisfies_hs256_minimum_length() -> None:
    """默认密钥必须自身就够长，否则本地每次跑都会刷 PyJWT 的密钥长度警告。

    一堆黄色警告的后果不是「安全」而是「所有人都学会无视警告」。
    """
    assert len(DEFAULT_JWT_SECRET.encode("utf-8")) >= MIN_JWT_SECRET_BYTES


def test_production_rejects_default_secret() -> None:
    with pytest.raises(ValueError, match="default value"):
        Settings(app_env="prod", jwt_secret_key=DEFAULT_JWT_SECRET)


def test_production_rejects_too_short_secret() -> None:
    """长度不足的密钥在 HS256 下可被暴力破解，生产环境必须直接起不来。"""
    with pytest.raises(ValueError, match="at least 32 bytes"):
        Settings(app_env="prod", jwt_secret_key="still-too-short")


def test_local_environment_tolerates_short_secret() -> None:
    """本地不该被这条护栏拦住 —— 否则开发时会一直起不来。"""
    Settings(app_env="local", jwt_secret_key="short")
