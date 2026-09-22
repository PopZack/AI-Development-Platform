"""Agent 产出的接口模型（文档 §6.5 / §7.3）。

注意 ``agent_runs`` 本身**不通过 API 暴露** —— 它是运维排障数据，
不是产品数据。要排障时直接查库或看日志。
对外只暴露通过校验的交付物（artifact）。
"""

from __future__ import annotations

from uuid import UUID

from app.domain.enums import ArtifactType
from app.schemas.common import ReadModel, UtcDateTime

__all__ = ["ArtifactRead", "AgentArtifactList"]


class ArtifactRead(ReadModel):
    id: UUID
    requirement_id: UUID
    agent_run_id: UUID | None = None
    type: ArtifactType
    version: int
    # 交付物内容就是 Agent 通过校验后的结构化结果，
    # 直接以 JSON 形式返回，调用方不需要再解析一层包装
    content: dict
    created_at: UtcDateTime
    updated_at: UtcDateTime


class AgentArtifactList(ReadModel):
    items: list[ArtifactRead]
    total: int
