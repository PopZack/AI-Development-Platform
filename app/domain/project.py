"""项目访问规则。

Layer: Domain —— 只回答「这个角色能不能做这件事」，不碰数据库、不碰 HTTP，
也不关心调用者到底是谁（那是 Service 的输入）。

文档 §14.1 的硬性要求：**权限等级由后端策略确定，不是由 LLM 或客户端决定**。
所以这套规则必须写死在代码里 —— 它不可配置，也不接受任何来自请求体的放宽。
"""

from __future__ import annotations

from uuid import UUID

from app.common.exceptions import AuthorizationError
from app.domain.enums import ProjectRole

__all__ = [
    "READ_ROLES",
    "WRITE_ROLES",
    "MANAGE_ROLES",
    "ensure_role_allowed",
]

# 能看项目和它的内容。VIEWER 是只读成员，所以它出现在这里而不是下面两个
READ_ROLES: frozenset[ProjectRole] = frozenset({ProjectRole.OWNER, ProjectRole.DEVELOPER, ProjectRole.VIEWER})

# 能在项目里产生内容（建需求、改需求）
WRITE_ROLES: frozenset[ProjectRole] = frozenset({ProjectRole.OWNER, ProjectRole.DEVELOPER})

# 能改项目本身、管理成员。只有 OWNER
MANAGE_ROLES: frozenset[ProjectRole] = frozenset({ProjectRole.OWNER})


def ensure_role_allowed(
    role: ProjectRole,
    allowed: frozenset[ProjectRole],
    *,
    action: str,
    project_id: UUID,
) -> None:
    """角色不在允许集合里就抛 403。

    ``details`` 里带上 ``required_roles``：报错信息要能告诉调用方「你差在哪」，
    否则前端只能靠猜，或者去翻文档 —— 而文档会和代码走偏。
    """
    if role in allowed:
        return

    raise AuthorizationError(
        f"Project role {role} is not allowed to {action}",
        code="PROJECT_ROLE_REQUIRED",
        details={
            "project_id": str(project_id),
            "action": action,
            "actual_role": str(role),
            "required_roles": sorted(r.value for r in allowed),
        },
    )
