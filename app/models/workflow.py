"""workflow_runs 表（设计文档 §6.3）。

Layer: Repository / Infrastructure。

关于文档的一处收敛：§6.3 只给了 ``started_at`` / ``finished_at``，没有审计字段。
这里照旧加 TimestampMixin（``created_at`` / ``updated_at``）——
「这行记录什么时候被创建的」和「什么时候开始跑的」是两件事，
用 ``started_at`` 兼任前者会让「已创建但还没启动」这个状态无法区分。
"""

from __future__ import annotations

from datetime import datetime
from uuid import UUID

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class WorkflowRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "workflow_runs"

    requirement_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )

    # §3.3 那台状态机。取值集合由 WorkflowStatus 约束，Domain 层校验，
    # 不加 CHECK 约束 —— 否则每加一个状态都要跑一次迁移
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # 阶段内部的细粒度进度（WorkflowStep）。与 status 的区分见 domain/enums.py
    current_step: Mapped[str] = mapped_column(String(32), nullable=False)

    # 幂等键。这是 Stage 2 的核心：客户端重试或双击「启动」时，同一个键只能
    # 产出一条工作流。UNIQUE 是关键 —— 应用层的「先查再插」在并发下不成立，
    # 真正兜底的是这个约束（Service 会捕获 IntegrityError 并返回已存在的那条）
    idempotency_key: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)

    started_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(), nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    def __repr__(self) -> str:
        return f"<WorkflowRun id={self.id} status={self.status} step={self.current_step}>"
