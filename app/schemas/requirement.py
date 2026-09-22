"""需求相关请求与响应模型。

两个刻意的设计：

1. ``RequirementUpdate`` **不包含 status**。文档 §3.3 明确要求
   「不允许客户端通过一个请求直接把状态任意修改为 COMPLETED」，
   所以状态只能由 Domain 规则 + 后续的 Workflow Service 推动，
   不能出现在请求体里。

2. 请求体里的字段叫 ``acceptance_criteria``（贴合文档 §12.1 的示例），
   而库里叫 ``acceptance_criteria_json``。响应模型用 ``validation_alias``
   把 ORM 属性映射成对外友好的字段名。

3. ``acceptance_criteria`` 在库里是**可空列**（没传验收标准时存 NULL），
   但对外的契约是「永远返回一个列表」。中间靠 ``BeforeValidator`` 把 ``None``
   归一成 ``[]`` —— 少了这一步，建一条不带验收标准的需求再读出来就会 500。
"""

from __future__ import annotations

from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field

from app.domain.enums import RequirementPriority, RequirementStatus
from app.schemas.common import ReadModel, UtcDateTime

__all__ = [
    "RequirementCreate",
    "RequirementUpdate",
    "RequirementRead",
    "PaginatedRequirements",
]


def _none_to_empty_list(value: Any) -> Any:
    """库里的 NULL 对外表现为空列表，避免可空列泄漏成 API 契约的一部分。"""
    return [] if value is None else value


AcceptanceCriteria = Annotated[list[str], BeforeValidator(_none_to_empty_list)]


class RequirementCreate(BaseModel):
    title: str = Field(min_length=1, max_length=300)
    description: str = Field(min_length=1, description="自然语言需求描述，Stage 3 会喂给 Product Agent")
    priority: RequirementPriority = RequirementPriority.P1
    acceptance_criteria: list[str] = Field(default_factory=list)
    # created_by 不出现在请求体里：它取自 JWT 主体。允许客户端指定创建人
    # 就等于允许冒名 —— 而且这个字段对客户端没有任何使用价值


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
    acceptance_criteria: AcceptanceCriteria = Field(
        default_factory=list, validation_alias="acceptance_criteria_json"
    )
    prd: dict[str, Any] | None = Field(default=None, validation_alias="prd_json")
    version: int
    created_by: UUID
    created_at: UtcDateTime
    updated_at: UtcDateTime


class PaginatedRequirements(BaseModel):
    items: list[RequirementRead]
    total: int
