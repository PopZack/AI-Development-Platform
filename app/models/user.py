"""users 表（设计文档 §6.3）。

Layer: Repository / Infrastructure —— 只描述 schema。

密码只保存 Argon2 哈希结果，任何情况下都不保存明文。
"""

from __future__ import annotations

from sqlalchemy import Integer, String
from sqlalchemy.orm import Mapped, mapped_column

from app.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class User(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "users"

    email: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    display_name: Mapped[str] = mapped_column(String(120), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    # 会话撤销：签发的 JWT 里带一份 token_version 快照，校验时与这里的值比对。
    # 登出或改密码时 +1，该用户已签发的所有令牌立即失效。
    #
    # 选它而不是「jti 黑名单表」的关键原因：鉴权本来就要读这一行（判断账号状态、
    # 查项目成员关系），所以版本比对是零额外查询；黑名单方案则要给每个请求
    # 硬加一次查表，等于拿鉴权主链路的开销去换一个低频操作的能力。
    token_version: Mapped[int] = mapped_column(Integer, nullable=False, default=0, server_default="0")

    def __repr__(self) -> str:
        return f"<User id={self.id} email={self.email!r}>"
