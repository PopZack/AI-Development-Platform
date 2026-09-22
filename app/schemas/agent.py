"""Agent 产出的接口模型（文档 §6.5 / §7.3）。

注意 ``agent_runs`` 本身**不通过 API 暴露** —— 它是运维排障数据，
不是产品数据。要排障时直接查库或看日志。
对外只暴露通过校验的交付物（artifact）。
"""

from __future__ import annotations

from uuid import UUID

from pydantic import Field

from app.domain.enums import ArtifactType, WorkflowStatus, WorkflowStep
from app.schemas.common import ReadModel, UtcDateTime

__all__ = [
    "AgentArtifactList",
    "ArtifactRead",
    "DeliverablesSummary",
    "RequirementBrief",
    "WorkflowRunBrief",
]


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

    @classmethod
    def from_entity(cls, artifact) -> ArtifactRead:
        """显式构造而不是 ``model_validate``：``content_json`` 要改名成对外的 ``content``。

        这层名字映射是故意的 —— 库里叫 ``content_json``（SQLite 的 JSON 列习惯带后缀），
        对外叫 ``content``（调用方不关心存储细节）。
        """
        return cls(
            id=artifact.id,
            requirement_id=artifact.requirement_id,
            agent_run_id=artifact.agent_run_id,
            type=ArtifactType(artifact.type),
            version=artifact.version,
            content=artifact.content_json,
            created_at=artifact.created_at,
            updated_at=artifact.updated_at,
        )


class AgentArtifactList(ReadModel):
    items: list[ArtifactRead]
    total: int


class RequirementBrief(ReadModel):
    """交付物汇总里带一份需求摘要，避免调用方再多打一次接口。"""

    id: UUID
    title: str
    status: str


class WorkflowRunBrief(ReadModel):
    id: UUID
    status: WorkflowStatus
    current_step: WorkflowStep


class DeliverablesSummary(ReadModel):
    """需求的完整交付物汇总（文档 §13 Stage 4 验收：「用户可以查看完整交付物」）。

    - ``deliverables`` 按类型取**最新版本**（历史版本仍可通过
      ``GET /requirements/{id}/artifacts`` 带过滤查询）
    - ``workspace_files`` 是工具网关写入工作区的实际文件清单
    """

    requirement: RequirementBrief
    latest_run: WorkflowRunBrief | None = None
    deliverables: list[ArtifactRead]
    workspace_files: list[str] = Field(default_factory=list)
