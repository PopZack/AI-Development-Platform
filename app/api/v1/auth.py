"""认证接口（设计文档 §7.1）。

Layer: Presentation（Router）。

关于 logout 的实现取舍：本项目的访问令牌是无状态 JWT，登出靠的是
``users.token_version`` 自增（详见 app/application/auth_service.py 的说明）。
所以 ``/auth/logout`` 不是「删掉一个服务端会话记录」，而是
「让这个用户已经签发的所有令牌立刻作废」。语义是真的，不是空转。
"""

from __future__ import annotations

from fastapi import APIRouter, status

from app.common.dependencies import AuthServiceDep, CurrentUserDep
from app.common.openapi import AUTH_ERROR_RESPONSES, CREATE_ERROR_RESPONSES
from app.schemas.auth import (
    LoginRequest,
    PasswordChangeRequest,
    SessionRevokedResponse,
    TokenResponse,
)
from app.schemas.user import UserCreate, UserRead

router = APIRouter(prefix="/auth", tags=["auth"])


@router.post(
    "/register",
    response_model=UserRead,
    status_code=status.HTTP_201_CREATED,
    summary="注册",
    responses=CREATE_ERROR_RESPONSES,
)
async def register(payload: UserCreate, service: AuthServiceDep) -> UserRead:
    user = await service.register(payload)
    return UserRead.model_validate(user)


@router.post(
    "/login",
    response_model=TokenResponse,
    summary="登录",
    description="邮箱不存在与密码错误返回完全相同的错误码，避免被用来枚举已注册邮箱。",
    responses={401: {"description": "凭据无效（INVALID_CREDENTIALS）或账号已停用（ACCOUNT_DISABLED）"}},
)
async def login(payload: LoginRequest, service: AuthServiceDep) -> TokenResponse:
    user = await service.authenticate(payload)
    access_token, expires_in = service.issue_token(user)
    return TokenResponse(access_token=access_token, expires_in=expires_in)


@router.get(
    "/me",
    response_model=UserRead,
    summary="当前用户",
    responses=AUTH_ERROR_RESPONSES,
)
async def read_current_user(current_user: CurrentUserDep) -> UserRead:
    return UserRead.model_validate(current_user)


@router.post(
    "/logout",
    response_model=SessionRevokedResponse,
    summary="登出（撤销该用户全部会话）",
    description=(
        "使该用户已签发的**所有**访问令牌立即失效（不只是当前这一个）。"
        "要做「只踢掉当前设备」需要改成服务端会话表，见 auth_service 的说明。"
    ),
    responses=AUTH_ERROR_RESPONSES,
)
async def logout(current_user: CurrentUserDep, service: AuthServiceDep) -> SessionRevokedResponse:
    token_version = await service.revoke_all_sessions(current_user)
    return SessionRevokedResponse(revoked=True, token_version=token_version)


@router.post(
    "/password",
    response_model=SessionRevokedResponse,
    summary="修改密码",
    description="修改成功后，该用户已签发的全部令牌立即失效，客户端需要重新登录。",
    responses=AUTH_ERROR_RESPONSES,
)
async def change_password(
    payload: PasswordChangeRequest,
    current_user: CurrentUserDep,
    service: AuthServiceDep,
) -> SessionRevokedResponse:
    token_version = await service.change_password(current_user, payload)
    return SessionRevokedResponse(revoked=True, token_version=token_version)
