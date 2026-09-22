"""SQLAlchemy 声明式基类与公共 Mixin。

Layer: Repository / Infrastructure —— 只描述表结构，不含业务规则。

时区约定（重要）：SQLite 不保存时区信息，若用 ``DateTime(timezone=True)``
写 aware datetime，读回来会变成 naive，之后任何 aware/naive 比较都会抛
``TypeError``。因此本项目统一 **存 naive UTC**，由 API 层负责补 ``Z``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID, uuid4

from sqlalchemy import JSON, DateTime, MetaData, Uuid, func
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

__all__ = [
    "Base",
    "UUIDPrimaryKeyMixin",
    "TimestampMixin",
    "JSONColumn",
    "utcnow",
]

# 约束命名规范：现在不起眼，等 Stage 5 接 Alembic 时才知道有多必要
# （否则 autogenerate 出来的迁移脚本里约束名是随机/匿名的，无法稳定引用）
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    """所有 ORM 模型的基类。"""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


def utcnow() -> datetime:
    """当前 UTC 时间，naive（无 tzinfo），用于落库。"""
    return datetime.now(UTC).replace(tzinfo=None)


class UUIDPrimaryKeyMixin:
    """UUID 主键。文档 §6.3 要求所有核心表主键为 UUID。"""

    id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), primary_key=True, default=uuid4)


class TimestampMixin:
    """创建/更新时间。落库为 naive UTC。"""

    created_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=utcnow, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(), nullable=False, default=utcnow, onupdate=utcnow, server_default=func.now()
    )


# SQLite 下用 JSON，PostgreSQL 下用 JSONB；切换数据库时这里改一处即可
JSONColumn = JSON().with_variant(JSONB(), "postgresql")
