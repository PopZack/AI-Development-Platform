"""审批的接口模型（文档 §6.x / 资源树 `/approvals/*`）。"""

from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel, Field

from app.domain.enums import ApprovalStatus
from app.schemas.common import ReadModel, UtcDateTime

__all__ = ["ApprovalDecision", "ApprovalList", "ApprovalRead"]


class ApprovalRead(ReadModel):
    id: UUID
    requirement_id: UUID
    tool_call_id: UUID | None = None
    tool_name: str
    status: ApprovalStatus
    requested_by: UUID | None = None
    reviewed_by: UUID | None = None
    reason: str | None = None
    review_note: str | None = None
    # 前端拿它渲染倒计时；PENDING 且已过期会在读取时被惰性改成 EXPIRED
    expires_at: UtcDateTime | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime


class ApprovalList(ReadModel):
    items: list[ApprovalRead]
    total: int


class ApprovalDecision(BaseModel):
    """批准 / 驳回的请求体。

    用请求体而不是 Query：审批意见是业务数据（要落库、要进审计），
    放在 URL 上既长又容易被中间设备记录。
    """

    note: str | None = Field(default=None, max_length=2000, description="审批意见 / 驳回理由")
