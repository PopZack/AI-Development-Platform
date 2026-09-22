"""认证相关请求与响应模型（设计文档 §7.1）。

注册请求复用 ``UserCreate`` —— 不再另定义一个字段相同的类，
否则两个注册入口的校验规则迟早会走偏。
"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.schemas.user import UserRead

__all__ = [
    "LoginRequest",
    "TokenResponse",
    "SessionRevokedResponse",
    "PasswordChangeRequest",
]


class LoginRequest(BaseModel):
    email: str = Field(min_length=1, max_length=255)
    password: str = Field(min_length=1, max_length=128)


class TokenResponse(BaseModel):
    """文档 §7.1 的登录响应结构。"""

    access_token: str
    token_type: str = "bearer"
    expires_in: int = Field(description="访问令牌有效秒数")


class SessionRevokedResponse(BaseModel):
    """登出 / 改密码后的会话撤销结果。

    把新的 ``token_version`` 返回出去，是为了让「撤销真的生效了」这件事
    在客户端可见、可断言 —— 否则一个 204 什么也说明不了。
    """

    revoked: bool = True
    token_version: int = Field(description="自增后的令牌版本；低于此版本的令牌全部失效")


class PasswordChangeRequest(BaseModel):
    current_password: str = Field(min_length=1, max_length=128)
    new_password: str = Field(min_length=8, max_length=128)


# 复用 UserRead 作为 /auth/register 与 /auth/me 的响应体
RegisterResponse = UserRead
CurrentUserResponse = UserRead
