"""OpenAPI 文档里复用的错误响应声明。

只影响 Swagger 上显示的响应结构，不参与运行时逻辑 ——
统一错误格式的真正实现是 app/common/error_handlers.py。
"""

from __future__ import annotations

from typing import Any

from app.schemas.common import ErrorResponse

__all__ = ["COMMON_ERROR_RESPONSES", "READ_ERROR_RESPONSES", "CREATE_ERROR_RESPONSES"]

COMMON_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: {"model": ErrorResponse, "description": "资源不存在（code 形如 PROJECT_NOT_FOUND）"},
    409: {"model": ErrorResponse, "description": "冲突（重复创建、状态不允许、唯一约束冲突）"},
    422: {"model": ErrorResponse, "description": "请求参数校验失败"},
    500: {"model": ErrorResponse, "description": "未知错误，对外只返回通用消息"},
}

READ_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: COMMON_ERROR_RESPONSES[404],
    422: COMMON_ERROR_RESPONSES[422],
}

CREATE_ERROR_RESPONSES: dict[int | str, dict[str, Any]] = {
    404: COMMON_ERROR_RESPONSES[404],
    409: COMMON_ERROR_RESPONSES[409],
    422: COMMON_ERROR_RESPONSES[422],
}
