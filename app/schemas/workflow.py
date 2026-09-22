"""工作流相关请求 / 响应模型（设计文档 §7.4）。

创建运行不需要请求体：幂等键走 ``Idempotency-Key`` 请求头（文档 §12.2 的示例就是这么发的）。
把它放在请求头而不是请求体，是因为它的语义是「这次请求的标识」而不是业务数据 ——
重试时请求体可以完全不变，头也天然跟着一起重发。

``Idempotency-Key`` 头的声明放在 Router 层（app/api/v1/workflows.py），
Schema 层不 import FastAPI。
"""

from __future__ import annotations

from uuid import UUID

from app.domain.enums import WorkflowStatus, WorkflowStep
from app.schemas.common import ReadModel, UtcDateTime

__all__ = ["WorkflowRunRead"]


class WorkflowRunRead(ReadModel):
    id: UUID
    requirement_id: UUID
    status: WorkflowStatus
    current_step: WorkflowStep
    idempotency_key: str
    # 已创建但尚未启动时这三个都是 null —— 这也是为什么要额外保留 created_at，
    # 否则「还没开始跑」和「跑了但没记时间」无法区分
    started_at: UtcDateTime | None = None
    finished_at: UtcDateTime | None = None
    error_code: str | None = None
    error_message: str | None = None
    created_at: UtcDateTime
    updated_at: UtcDateTime
