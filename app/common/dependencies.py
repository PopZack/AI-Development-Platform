"""FastAPI 依赖装配。

Layer: Common（DI 装配）—— 只负责把 Session / Settings 包成 Service，
以及把 Bearer 令牌换成当前用户，不含业务判断。

文档 §14.2 的要求：Router 只做「接收请求 → 转交 Service」，
所以依赖注入是 Router 唯一允许出现的装配逻辑。
"""

from __future__ import annotations

from typing import Annotated

from fastapi import Depends, Query, Request
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy.ext.asyncio import AsyncSession

from app.application.auth_service import AuthService
from app.application.project_service import ProjectService
from app.application.requirement_service import RequirementService
from app.application.user_service import UserService
from app.application.workflow_service import WorkflowService
from app.common.exceptions import AuthenticationError
from app.config.settings import Settings
from app.infrastructure.db.session import get_session
from app.models.user import User

__all__ = [
    "SessionDep",
    "SettingsDep",
    "AuthServiceDep",
    "UserServiceDep",
    "ProjectServiceDep",
    "RequirementServiceDep",
    "WorkflowServiceDep",
    "CurrentUserDep",
    "LimitQuery",
    "OffsetQuery",
]

SessionDep = Annotated[AsyncSession, Depends(get_session)]


def get_app_settings(request: Request) -> Settings:
    """取「当前 app 实例」的配置，而不是全局缓存的 get_settings()。

    这个区别在测试里是致命的：测试用 ``create_app(test_settings)`` 装了一份配置，
    如果这里回落到全局 ``get_settings()``，Service 会读到 .env 里的另一份
    —— 于是「测试配置」和「实际生效配置」不是同一个东西，排查起来极其费劲。
    """
    return request.app.state.settings


SettingsDep = Annotated[Settings, Depends(get_app_settings)]


def get_auth_service(session: SessionDep, settings: SettingsDep) -> AuthService:
    return AuthService(session, settings)


def get_user_service(session: SessionDep) -> UserService:
    return UserService(session)


def get_project_service(session: SessionDep) -> ProjectService:
    return ProjectService(session)


def get_requirement_service(session: SessionDep) -> RequirementService:
    return RequirementService(session)


def get_workflow_service(session: SessionDep) -> WorkflowService:
    return WorkflowService(session)


AuthServiceDep = Annotated[AuthService, Depends(get_auth_service)]
UserServiceDep = Annotated[UserService, Depends(get_user_service)]
ProjectServiceDep = Annotated[ProjectService, Depends(get_project_service)]
RequirementServiceDep = Annotated[RequirementService, Depends(get_requirement_service)]
WorkflowServiceDep = Annotated[WorkflowService, Depends(get_workflow_service)]

# auto_error=False 是刻意的：HTTPBearer 默认的失败行为是抛 403 且响应体不符合
# 我们的统一错误结构。关掉它，由我们自己抛 AuthenticationError（401 + 统一错误体）。
# 401 才是对的语义 —— 403 的含义是「已认证但无权限」，和「没带令牌」是两回事。
bearer_scheme = HTTPBearer(auto_error=False, scheme_name="BearerToken", description="Bearer <access_token>")


async def get_current_user(
    service: AuthServiceDep,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
) -> User:
    """把 ``Authorization: Bearer <token>`` 换成当前用户。

    这个依赖同时也承担了会话撤销的校验：令牌里的 token_version 一旦落后于
    用户当前版本（登出或改密码导致），就在这里被拒。
    """
    if credentials is None or not credentials.credentials:
        raise AuthenticationError("Authorization header with a Bearer token is required")
    return await service.authenticate_token(credentials.credentials)


CurrentUserDep = Annotated[User, Depends(get_current_user)]

LimitQuery = Annotated[int, Query(ge=1, le=200, description="每页条数")]
OffsetQuery = Annotated[int, Query(ge=0, description="偏移量")]
