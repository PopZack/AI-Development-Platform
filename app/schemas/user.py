"""用户相关请求 / 响应模型。

``UserCreate`` 现在只被 ``/auth/register`` 使用（``POST /users`` 已下线）。

``UserUpdate`` **不含 status**：设计文档只定义了项目级角色（Owner / Developer），
没有全局管理员。保留 status 等于「任何登录用户都能停用任何账号」，而凭空造一个
admin 角色又是替文档加设计。所以停用账号暂时没有 API 入口 ——
``ACCOUNT_DISABLED`` 分支保留在认证层，将来有管理功能时立刻生效。
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
    """本人可修改的字段。未传表示不改。"""

    display_name: str | None = Field(default=None, min_length=1, max_length=120)


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
