"""领域枚举。

Layer: Domain —— 纯 Python，不依赖 FastAPI、SQLAlchemy 或任何 LLM 客户端。

这里有一个需要说明的设计判断：设计文档 §3.3 定义的那台状态机是
**工作流**的状态机（对应 workflow_runs.status），但文档从头到尾没有定义
requirements.status 的取值集合（只在 §12.1 出现过一次 ``status = DRAFT``）。
两者不是一回事，所以这里拆成两个枚举：

- ``RequirementStatus``：需求自身的轻量生命周期，Stage 1 使用。
- ``WorkflowStatus``：文档 §3.3 的完整状态机，Stage 4 使用；现在定义好，
  并把文档缺失的两个状态补上（见 workflow.py 的转换表）。
"""

from __future__ import annotations

from enum import StrEnum

__all__ = [
    "UserStatus",
    "ProjectStatus",
    "ProjectRole",
    "RequirementPriority",
    "RequirementStatus",
    "WorkflowStatus",
]


class UserStatus(StrEnum):
    ACTIVE = "ACTIVE"
    DISABLED = "DISABLED"


class ProjectStatus(StrEnum):
    ACTIVE = "ACTIVE"
    ARCHIVED = "ARCHIVED"


class ProjectRole(StrEnum):
    """文档 §7.2：「首期可以先实现 Owner 和 Developer 两种项目角色」。

    保留 VIEWER 占位，权限模型后续扩展时不需要改表结构。
    """

    OWNER = "OWNER"
    DEVELOPER = "DEVELOPER"
    VIEWER = "VIEWER"


class RequirementPriority(StrEnum):
    """优先级。文档 §12.1 的请求示例用的是 "P0"。"""

    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class RequirementStatus(StrEnum):
    """需求自身的生命周期 —— 注意不要和 WorkflowStatus 混用。

    刻意保持精简：需求只关心「有没有被设计过、有没有做完」，
    逐步执行到哪一步是工作流的职责。
    """

    DRAFT = "DRAFT"
    """刚创建，尚未进入任何 AI 流程。文档 §12.1 的初始状态。"""

    ANALYZING = "ANALYZING"
    """Product / Architect Agent 正在处理。"""

    DESIGNED = "DESIGNED"
    """PRD 与架构设计已生成并通过校验。"""

    COMPLETED = "COMPLETED"
    """关联工作流已走完全流程。"""

    FAILED = "FAILED"
    """分析或设计阶段不可恢复失败。"""

    CANCELLED = "CANCELLED"
    """用户或系统取消。"""


class WorkflowStatus(StrEnum):
    """文档 §3.3 的工作流状态机。

    ``REJECTED`` 与 ``REVISION_REQUIRED`` 在文档的状态机图里出现，
    但没被列进紧随其后的状态表 —— 这里按状态机图的语义补齐，
    否则 §12.7 那个 ``needs_revision`` 的 Reviewer 结果根本无处可去。
    """

    CREATED = "CREATED"
    RUNNING = "RUNNING"
    ANALYZING = "ANALYZING"
    PLANNING = "PLANNING"
    IMPLEMENTING = "IMPLEMENTING"
    TESTING = "TESTING"
    REVIEWING = "REVIEWING"
    WAITING_APPROVAL = "WAITING_APPROVAL"
    APPROVED = "APPROVED"
    COMPLETED = "COMPLETED"

    REJECTED = "REJECTED"
    REVISION_REQUIRED = "REVISION_REQUIRED"

    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    CANCELLED = "CANCELLED"


WORKFLOW_TERMINAL_STATUSES: frozenset[WorkflowStatus] = frozenset(
    {WorkflowStatus.COMPLETED, WorkflowStatus.FAILED, WorkflowStatus.CANCELLED}
)
