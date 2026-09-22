"""JWT 签发与校验。

Layer: Infrastructure —— 具体技术选型（PyJWT / HS256）藏在这里，
Service 只知道「给我一个令牌」和「把这个令牌解成载荷」。

这里刻意只做两件事：签、解。**权限判断不在这里** ——
「这个令牌还有效吗」是解码问题，「这个人能不能访问这个项目」是
domain + service 的问题，混在一起后面就没法单独测试了。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

import jwt as pyjwt
from jwt import ExpiredSignatureError, InvalidTokenError

from app.common.exceptions import AuthenticationError
from app.config.settings import Settings

__all__ = ["TokenPayload", "create_access_token", "decode_access_token"]


@dataclass(frozen=True)
class TokenPayload:
    """从令牌里解出来的、值得信任的最小信息集。"""

    user_id: UUID
    token_version: int
    token_id: str
    expires_at: datetime


def create_access_token(*, user_id: UUID, token_version: int, settings: Settings) -> tuple[str, int]:
    """签发访问令牌。

    返回 ``(token, expires_in_seconds)``，其中 ``expires_in`` 直接给客户端用
    （文档 §7.1 的登录响应里有 ``expires_in`` 字段）。

    载荷里放 ``ver``：这是会话撤销的钩子。校验时把它和 users.token_version
    比对，不一致就说明这个令牌已被登出/改密码作废。
    """
    now = datetime.now(UTC)
    lifetime = timedelta(minutes=settings.access_token_expire_minutes)

    payload = {
        "sub": str(user_id),  # PyJWT 2.10+ 要求 sub 必须是字符串
        "ver": token_version,
        "jti": uuid4().hex,  # 便于日志里分辨是哪一个令牌
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
    }

    token = pyjwt.encode(payload, settings.jwt_secret_key, algorithm=settings.jwt_algorithm)
    return token, int(lifetime.total_seconds())


def decode_access_token(token: str, settings: Settings) -> TokenPayload:
    """解码并校验签名与有效期。

    任何失败都转成 ``AuthenticationError``（HTTP 401），且区分
    ``TOKEN_EXPIRED`` / ``TOKEN_INVALID`` 两个 code：前者提示客户端去重新登录，
    后者通常意味着令牌被篡改或用了错误的密钥，值得告警。
    """
    try:
        raw = pyjwt.decode(token, settings.jwt_secret_key, algorithms=[settings.jwt_algorithm])
    except ExpiredSignatureError as exc:
        raise AuthenticationError("Access token has expired", code="TOKEN_EXPIRED") from exc
    except InvalidTokenError as exc:
        # 签名不对、格式不对、算法不对都会落到这里。
        # 不把具体原因回给客户端 —— 那等于告诉攻击者他差在哪一步。
        raise AuthenticationError("Access token is invalid", code="TOKEN_INVALID") from exc

    try:
        user_id = UUID(str(raw["sub"]))
        token_version = int(raw["ver"])
        token_id = str(raw["jti"])
        expires_at = datetime.fromtimestamp(int(raw["exp"]), tz=UTC)
    except (KeyError, TypeError, ValueError) as exc:
        raise AuthenticationError("Access token payload is malformed", code="TOKEN_INVALID") from exc

    return TokenPayload(
        user_id=user_id,
        token_version=token_version,
        token_id=token_id,
        expires_at=expires_at,
    )
