"""agent_runs 与 artifacts 两张表（设计文档 §6.4 / §6.5）。

Layer: Repository / Infrastructure。

### 关于 agent_runs 的粒度（文档没定义，这是本项目的选择）

**一行 = 一次 provider 调用**，不是「一次逻辑上的 Agent 执行」。

理由：内容层重试的价值恰恰在于「第一次模型回了坏 JSON、第二次改好了」——
而这个信息只有按次记录才能看到。如果一次执行只落一行，就只能看到最终结果，
排查时无法回答「它是一次就成功，还是重试了三次才勉强成功」。

代价是同一逻辑执行会产生多行，所以加了 ``execution_id``：
同一次逻辑执行的所有尝试共享它，``attempt`` 从 1 开始递增。

### 为什么同时存 output 和 parsed

- ``output_json``：模型**原始输出**，不经过加工。排障必须能看到原文。
- ``parsed_json``：**通过 Pydantic 校验后**的结构化结果。

``parsed_json`` 为 ``NULL`` 而 ``status=INVALID_OUTPUT``，含义就非常明确：
「模型返回了内容，但它没通过校验，所以没有可用结果」。
文档 §14.3 的硬线就体现在这里 —— **没通过校验的输出绝不会进 parsed_json**。
"""

from __future__ import annotations

from json import loads
from typing import Any
from uuid import UUID

from sqlalchemy import JSON, ForeignKey, Integer, String, Text, Uuid
from sqlalchemy.orm import Mapped, mapped_column, validates

from app.domain.enums import AgentRole, AgentRunStatus, ArtifactType
from app.infrastructure.db.base import Base, TimestampMixin, UUIDPrimaryKeyMixin


class AgentRun(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    __tablename__ = "agent_runs"

    #: 同一次逻辑执行的所有尝试共享这个 id
    execution_id: Mapped[UUID] = mapped_column(Uuid(as_uuid=True), nullable=False, index=True)
    #: 第几次尝试，从 1 开始
    attempt: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    requirement_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workflow_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True, index=True
    )

    agent_role: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)

    #: 实际被调用的模型。Provider 可能把短名解析成具体 build
    #: （实测：请求 `deepseek-v4-flash`，返回 `deepseek-v4-flash-ga-260731`），
    #: 所以记录的是**响应里**的 model，不是请求里那个
    model: Mapped[str | None] = mapped_column(String(128), nullable=True)

    prompt_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    completion_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    total_tokens: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    latency_ms: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    system_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)
    user_prompt: Mapped[str | None] = mapped_column(Text, nullable=True)

    output_json: Mapped[str | None] = mapped_column(Text, nullable=True)
    parsed_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)

    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)

    @validates("agent_role")
    def _validate_role(self, _key: str, value: str) -> str:
        AgentRole(value)  # 未知角色直接抛错，不留脏数据
        return value

    @validates("status")
    def _validate_status(self, _key: str, value: str) -> str:
        AgentRunStatus(value)
        return value

    @property
    def output(self) -> Any:
        """原始输出解析成 JSON 后的对象；不是合法 JSON 就返回原字符串。

        排障时要能看到「模型到底吐了什么」，包括它吐的是一坨非 JSON 文本。
        """
        if self.output_json is None:
            return None
        try:
            return loads(self.output_json)
        except ValueError:
            return self.output_json

    def __repr__(self) -> str:
        return (
            f"<AgentRun exec={self.execution_id} attempt={self.attempt} "
            f"role={self.agent_role} status={self.status}>"
        )


class Artifact(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Agent 产出的交付物（文档 §6.5）。

    只存**通过校验后**的结构化内容。原始输出留在 agent_runs.output_json 里 ——
    交付物这张表代表「可信的结果」，不该混进未经验证的东西。
    """

    __tablename__ = "artifacts"

    requirement_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("requirements.id", ondelete="CASCADE"), nullable=False, index=True
    )
    workflow_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("workflow_runs.id", ondelete="SET NULL"), nullable=True
    )
    agent_run_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("agent_runs.id", ondelete="SET NULL"), nullable=True
    )

    type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    #: 同一份需求的同类交付物可以有多版（需求改了就要重跑）
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)

    content_json: Mapped[dict[str, Any]] = mapped_column(JSON, nullable=False)

    @validates("type")
    def _validate_type(self, _key: str, value: str) -> str:
        ArtifactType(value)
        return value

    def __repr__(self) -> str:
        return f"<Artifact type={self.type} v{self.version} requirement={self.requirement_id}>"
