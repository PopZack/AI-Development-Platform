"""需求相关请求与响应模型。

两个刻意的设计：

1. ``RequirementUpdate`` **不包含 status**。文档 §3.3 明确要求
   「不允许客户端通过一个请求直接把状态任意修改为 COMPLETED」，
   所以状态只能由 Domain 规则 + 后续的 Workflow Service 推动，
   不能出现在请求体里。

2. 请求体里的字段叫 ``acceptance_criteria``（贴合文档 §12.1 的示例），
   而库里叫 ``acceptance_criteria_json``。响应模型用 ``validation_alias``
   把 ORM 属性映射成对外友好的字段名。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.enums import RequirementPriority, RequirementStatus
from app.schemas.common import ReadModel, UtcDateTime

__all__ = [
    "RequirementCreate",
    "RequirementUpdate",
    "RequirementRead",
    "PaginatedRequirements",
]


class RequirementCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, description="自然语言需求描述，Stage 3 会喂给 Product Agent")
    priority: RequirementPriority = RequirementPriority.P1
    acceptance_criteria: list[str] = Field(default_factory=list)
    created_by: UUID = Field(description="Stage 1 由客户端指定；Stage 2 起改为取 JWT 主体")


class RequirementUpdate(BaseModel):
    title: str | None = Field(default=None, min_length=1, max_length=300)
    description: str | None = Field(default=None, min_length=1)
    priority: RequirementPriority | None = None
    acceptance_criteria: list[str] | None = None


class RequirementRead(ReadModel):
    model_config = ConfigDict(from_attributes=True, populate_by_name=True)

    id: UUID
    project_id: UUID
    title: str
    description: str
    status: RequirementStatus
    priority: RequirementPriority
    acceptance_criteria: list[str] = Field(default_factory=list, validation_alias="acceptance_criteria_json")
    prd: dict[str, Any] | None = Field(default=None, validation_alias="prd_json")
    version: int
    created_by: UUID
    created_at: UtcDateTime
    updated_at: UtcDateTime


class PaginatedRequirements(BaseModel):
    items: list[RequirementRead]
    total: int
