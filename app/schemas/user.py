"""用户相关请求 / 响应模型。

注意：Stage 1 尚无认证，创建用户靠 ``POST /users``。
文档 §7.1 的 ``POST /auth/register`` 会在 Stage 2 落地，届时本模块的
``UserCreate`` 会被 auth 复用，而不是再定义一个。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, EmailStr, Field

from app.domain.enums import UserStatus
from app.schemas.common import ReadModel, UtcDateTime

__all__ = ["UserCreate", "UserUpdate", "UserRead", "PaginatedUsers"]


class UserCreate(BaseModel):
    email: EmailStr
    password: str = Field(min_length=8, max_length=128, description="明文密码，仅用于计算哈希，不落库")
    display_name: str = Field(min_length=1, max_length=120)


class UserUpdate(BaseModel):
    """部分更新。所有字段可选，未传表示不改。"""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)
    status: UserStatus | None = None


class UserRead(ReadModel):
    id: UUID
    email: EmailStr
    display_name: str
    status: UserStatus
    created_at: UtcDateTime
    updated_at: UtcDateTime


class PaginatedUsers(BaseModel):
    items: list[UserRead]
    total: int
