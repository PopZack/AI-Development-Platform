"""Schema 公共构件。

Layer: Presentation（请求/响应模型）。

三层对象刻意分开，文档 §6.4 专门提醒过：
- SQLAlchemy Model  → 数据库表映射（app/models/）
- Domain Entity      → 业务实体与规则（app/domain/）
- Pydantic Schema    → 请求与响应校验（app/schemas/）

不要为了省事把数据库对象、API 输入对象和业务规则塞进一个类。
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Annotated, Any

from pydantic import BaseModel, ConfigDict, Field, PlainSerializer


def _iso_utc(value: datetime) -> str:
    """库里存的是 naive UTC，对外统一成带 ``Z`` 的 ISO-8601。

    如果直接把 naive datetime 序列化出去，前端会按本地时区解读，
    同一个时间在不同机器上会显示出不同的值。
    """
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    return value.astimezone(UTC).isoformat().replace("+00:00", "Z")


UtcDateTime = Annotated[datetime, PlainSerializer(_iso_utc, return_type=str, when_used="json")]


class ReadModel(BaseModel):
    """响应模型基类：允许直接从 ORM 对象构造（``from_attributes``）。"""

    model_config = ConfigDict(from_attributes=True)


class ErrorDetail(BaseModel):
    code: str
    message: str
    request_id: str
    details: dict[str, Any] | list[Any] = Field(default_factory=dict)


class ErrorResponse(BaseModel):
    """文档 §7 的统一错误格式，用于 OpenAPI 文档展示实际响应结构。"""

    error: ErrorDetail
