"""tool_calls 表（设计文档 §6.6，Stage 1 扩展 5 表之一）。

Layer: Repository / Infrastructure。

「L1 允许**并记录**」—— 这条不是可选的。没有记录就无法回答：
「Agent 到底读了哪些文件？」「它试过越界吗？」「这条 Patch 是谁批准的？」

刻意让 Gateway 在**自己的事务里**提交审计记录（而不是跟着外层业务事务走）：
外层回滚时，审计记录要留下来 —— 出问题的时候你恰恰需要知道它做过什么。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class ToolCall(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "tool_calls"

    workflow_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )
    requirement_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("requirements.id", ondelete="SET NULL"), nullable=True, index=True
    )

    tool_name: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    level: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    #: L4 工具消费掉的审批。「一条审批只能换一次成功执行」就靠查这张表判重放
    approval_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("approvals.id", ondelete="SET NULL"), nullable=True, index=True
    )

    #: 只存工具的入参。文件路径没问题；如果将来有带密钥的工具，这里要脱敏
    params_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    result_summary: Mapped[str | None] = mapped_column(Text, nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    @property
    def params(self) -> Any:
        return ToolCall.decode(self.params_json)

    @staticmethod
    def decode(raw: str | None) -> Any:
        import json

        return json.loads(raw) if raw else None

    def __repr__(self) -> str:
        return f"<ToolCall tool={self.tool_name} status={self.status}>"
