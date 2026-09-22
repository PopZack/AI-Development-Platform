"""projects / project_members 表（设计文档 §6.3）。

Layer: Repository / Infrastructure。

对文档的一处收敛：§6.3 里 projects 只列了 created_at，project_members 也只列了
created_at，而 users / requirements 都有 created_at + updated_at。这里统一使用
TimestampMixin，让所有核心表的审计字段口径一致 —— 排查问题时不用先猜哪张表有
updated_at。
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import ForeignKey, String, Text, UniqueConstraint, Uuid
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class Project(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "projects"

    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(String(200), nullable=False, unique=True, index=True)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    # Owner 删除前必须先转移项目归属，否则报错而不是静默级联删除整个项目
    owner_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    def __repr__(self) -> str:
        return f"<Project id={self.id} slug={self.slug!r}>"


class ProjectMember(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """项目成员与角色。文档 §6.3 要求 UNIQUE(project_id, user_id)。"""

    __tablename__ = "project_members"
    __table_args__ = (UniqueConstraint("project_id", "user_id", name="uq_project_members_project_user"),)

    project_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    role: Mapped[str] = mapped_column(String(32), nullable=False)

    def __repr__(self) -> str:
        return f"<ProjectMember project={self.project_id} user={self.user_id} role={self.role}>"
