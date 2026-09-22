"""项目 / 项目成员相关请求与响应模型。

关于 ``owner_id``：它**只在响应里出现，不在请求里**。

Stage 1 因为还没有认证，创建项目时由客户端显式指定 owner —— 那是个临时做法。
Stage 2 起 owner 一律取自 JWT 主体，``ProjectCreate.owner_id`` 已删除。注意这不是
「服务端忽略客户端传的值」，而是请求体里**根本没有这个入口**：前者仍然要求调用方
理解一个无意义的字段，后者直接让提权尝试无从写起。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field, field_validator

from app.common.utils import validate_slug
from app.domain.enums import ProjectRole, ProjectStatus
from app.schemas.common import ReadModel, UtcDateTime

__all__ = [
    "ProjectCreate",
    "ProjectUpdate",
    "ProjectRead",
    "ProjectMemberCreate",
    "ProjectMemberRead",
    "PaginatedProjects",
    "PaginatedProjectMembers",
]


class ProjectCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    slug: str | None = Field(
        default=None,
        max_length=200,
        description="URL 标识。不传则根据 name 自动生成；传入则必须是 kebab-case",
    )
    description: str | None = None

    @field_validator("slug")
    @classmethod
    def _check_slug(cls, value: str | None) -> str | None:
        if value is not None and not validate_slug(value):
            raise ValueError("slug must be kebab-case, e.g. 'todo-api-demo'")
        return value


class ProjectUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = None
    status: ProjectStatus | None = None


class ProjectRead(ReadModel):
    id: UUID
    name: str
    slug: str
    description: str | None
    owner_id: UUID
    status: ProjectStatus
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ProjectMemberCreate(BaseModel):
    user_id: UUID
    role: ProjectRole = ProjectRole.DEVELOPER


class ProjectMemberRead(ReadModel):
    id: UUID
    project_id: UUID
    user_id: UUID
    role: ProjectRole
    created_at: UtcDateTime


class PaginatedProjects(BaseModel):
    items: list[ProjectRead]
    total: int


class PaginatedProjectMembers(BaseModel):
    items: list[ProjectMemberRead]
    total: int
