"""approvals 表（设计文档 §6.x）。

Layer: Repository / Infrastructure。

一次审批对应**一次** L4 工具调用。刻意在表里冗余存 ``tool_name``：
审批列表是给人看的，为了显示「要做什么」去 join tool_calls 不值得。

``requested_by`` 记的是「谁触发了这个工具」（通常是 Agent 背后的用户），
``reviewed_by`` 记的是「谁批的」。两者分开是刻意的**职责分离**：
申请的人不该自己批。
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.domain.enums import ApprovalStatus
from app.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Approval(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "approvals"

    requirement_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workflow_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    tool_call_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("tool_calls.id", ondelete="SET NULL"), nullable=True, index=True
    )

    tool_name: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    requested_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    reason: Mapped[str | None] = mapped_column(Text, nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    @validates("status")
    def _validate_status(self, _key: str, value: str) -> str:
        ApprovalStatus(value)  # 未知状态直接抛错，不留脏数据
        return value

    @property
    def content(self) -> dict[str, Any]:
        """审批上下文里最常被读的几个字段，收拢成一个属性方便 Schema 映射。"""
        return {
            "tool_name": self.tool_name,
            "reason": self.reason,
            "review_note": self.review_note,
        }

    def __repr__(self) -> str:
        return f"<Approval tool={self.tool_name} status={self.status}>"
