"""工具包汇总。

Agent 要用工具时，从这里 import ``ToolGateway``。
**不要绕过 Gateway 直接读工作区文件** —— 权限分级、路径校验、审计记录
三条约束只在同一道门后才成立。
"""

from app.infrastructure.tools.gateway import ToolContext, ToolGateway
from app.infrastructure.tools.paths import WorkspacePathValidator
from app.infrastructure.tools.read_tools import READ_TOOLS, ToolDefinition, ToolRequest

__all__ = [
    "ToolContext",
    "ToolGateway",
    "WorkspacePathValidator",
    "ToolDefinition",
    "ToolRequest",
    "READ_TOOLS",
]
