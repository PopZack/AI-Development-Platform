"""requirements 表（设计文档 §6.3）。

Layer: Repository / Infrastructure。

prd_json / acceptance_criteria_json 用 SQLAlchemy 的 JSON 类型承载；文档说
「首期只保存小型结构化结果即可」，所以 Stage 1 不引入对象存储。
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from sqlalchemy import ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base, JSONColumn, TimestampMixin, UUIDPrimaryKeyMixin


class Requirement(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "requirements"

    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title: Mapped[str] = mapped_column(String(300), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(16), nullable=False)

    # Stage 3 由 Product Agent 填充；Stage 1 保持为 NULL，绝不放未经验证的模型输出
    prd_json: Mapped[dict[str, Any] | None] = mapped_column(JSONColumn, nullable=True)
    acceptance_criteria_json: Mapped[list[Any] | None] = mapped_column(JSONColumn, nullable=True)

    created_by: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    # 需求被修改时自增，用于判断已有 PRD 是否已过期（文档 §6.3 的 version 字段）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    def __repr__(self) -> str:
        return f"<Requirement id={self.id} title={self.title!r} status={self.status}>"
