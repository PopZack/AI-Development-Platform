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
    tool_call_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True), nullable=True, index=True)
    # ⚠️ 这一列**刻意不加外键**，与上面的 requirement_id 不同。
    #
    # 原因：``approvals.tool_call_id → tool_calls`` 与
    # ``tool_calls.approval_id → approvals`` 互相引用，构成外键环。
    # 后果不是「警告一下」而已，是**真的建不出表**：
    #   - SQLAlchemy 的排序器遇到环会直接放弃这张表的依赖关系，只发一条
    #     SAWarning（Cannot correctly sort tables…），于是 ``approvals`` 被排到
    #     ``users`` 前面；
    #   - ``create_all()`` 会自己把环上的外键延后成 ALTER，所以本地 SQLite 与
    #     直接建表都看不出问题；
    #   - 但 **autogenerate 生成的迁移是内联建外键的**，于是换到 MySQL 直接报
    #     ``(1824, "Failed to open the referenced table 'users'")`` —— 这个 bug
    #     只有在真 MySQL 上跑迁移才暴露（2026-09-23 实测）。
    #
    # 拆环的方式选了「保留列、去掉这条约束」：这列目前只读不写（纯追溯用），
    # 而 ``tool_calls.approval_id`` 是审批重放判定要查的列，必须留着。
    # 用 ``use_alter=True`` 也能绕开环，但 SQLite 不支持 ALTER 添加约束，
    # 代价是这条外键在 SQLite 上静默失效 —— 声明了却不生效的约束比不声明更危险。

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
