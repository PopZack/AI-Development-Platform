"""统一异常体系。

对应设计文档 §9.2「异常分类」表。约定：

- 每个异常自带 ``code`` 与 ``status_code``，由 error_handlers 统一转成
  ``{"error": {"code", "message", "request_id", "details"}}``（文档 §7）。
- ``code`` 是给调用方做分支判断的稳定标识，业务侧可以覆盖得更具体，
  例如 ``NotFoundError(code="REQUIREMENT_NOT_FOUND")``。

注意：这里刻意不复用内置异常名，避免和 ``TimeoutError`` 这类内置异常混淆；
文档中的 TimeoutError 在本项目实现为 ``UpstreamTimeoutError``。
"""

from __future__ import annotations

from typing import Any

__all__ = [
    "AppError",
    "ValidationError",
    "AuthenticationError",
    "AuthorizationError",
    "NotFoundError",
    "ConflictError",
    "AgentOutputError",
    "ToolDeniedError",
    "ApprovalRequiredError",
    "ProviderError",
    "UpstreamTimeoutError",
    "InternalError",
]


class AppError(Exception):
    """所有业务异常的基类。

    捕获方只依赖 ``code`` / ``status_code`` / ``details``，
    内部堆栈信息绝不返回给客户端。
    """

    code: str = "INTERNAL_ERROR"
    status_code: int = 500
    default_message: str = "Internal server error"

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
    ) -> None:
        self.message = message or self.default_message
        if code is not None:
            self.code = code
        if status_code is not None:
            self.status_code = status_code
        self.details: dict[str, Any] = details or {}
        super().__init__(self.message)

    def to_error_body(self, request_id: str) -> dict[str, Any]:
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "request_id": request_id,
                "details": self.details,
            }
        }


class ValidationError(AppError):
    """请求字段格式错误 / 业务前置校验失败。HTTP 422。"""

    code = "VALIDATION_ERROR"
    status_code = 422
    default_message = "Request validation failed"


class AuthenticationError(AppError):
    """Token 缺失或无效。HTTP 401。"""

    code = "AUTHENTICATION_REQUIRED"
    status_code = 401
    default_message = "Authentication required"


class AuthorizationError(AppError):
    """已认证但无资源权限。HTTP 403。"""

    code = "FORBIDDEN"
    status_code = 403
    default_message = "No permission to access this resource"


class NotFoundError(AppError):
    """资源不存在。HTTP 404。建议传具体 code，如 REQUIREMENT_NOT_FOUND。"""

    code = "NOT_FOUND"
    status_code = 404
    default_message = "Resource does not exist"

    @classmethod
    def for_resource(cls, resource_code: str, message: str | None = None) -> NotFoundError:
        """``NotFoundError.for_resource("REQUIREMENT")`` → code ``REQUIREMENT_NOT_FOUND``。"""
        return cls(message, code=f"{resource_code.upper()}_NOT_FOUND")


class ConflictError(AppError):
    """重复创建 / 状态冲突 / 唯一约束冲突。HTTP 409。"""

    code = "CONFLICT"
    status_code = 409
    default_message = "Resource conflict"


class AgentOutputError(AppError):
    """LLM 输出不符合 Pydantic Schema。业务错误或有限重试，不直接落库。"""

    code = "AGENT_OUTPUT_INVALID"
    status_code = 502
    default_message = "Agent output failed schema validation"


class ToolDeniedError(AppError):
    """工具权限不足 / 参数越界 / 路径越界。HTTP 403。"""

    code = "TOOL_DENIED"
    status_code = 403
    default_message = "Tool call denied by policy"


class ApprovalRequiredError(AppError):
    """高风险操作需要人工审批。工作流应进入 WAITING_APPROVAL 而不是当失败处理。"""

    code = "APPROVAL_REQUIRED"
    status_code = 409
    default_message = "Human approval required before this action"


class ProviderError(AppError):
    """LLM Provider 异常（网络、限流、5xx）。可重试。"""

    code = "PROVIDER_ERROR"
    status_code = 502
    default_message = "LLM provider error"


class UpstreamTimeoutError(AppError):
    """模型或工具调用超时。终止或有限重试（对应文档的 TimeoutError）。"""

    code = "TIMEOUT"
    status_code = 504
    default_message = "Upstream call timed out"


class InternalError(AppError):
    """未知错误。HTTP 500，对外只给通用消息。"""

    code = "INTERNAL_ERROR"
    status_code = 500
    default_message = "Internal server error"
