"""认证用例。

Layer: Application（Service）—— 编排「注册 / 登录 / 令牌换用户 / 撤销会话」，
并持有事务边界。

三件事刻意不放在这里：
- 密码怎么哈希（infrastructure/auth/password.py）
- 令牌怎么签、怎么解（infrastructure/auth/jwt.py）
- 谁能访问什么资源（Stage 2 的权限依赖 + domain）

本模块只回答「你是谁」和「怎么让你不再是」。
"""

from __future__ import annotations

import logging

from sqlalchemy.ext.asyncio import AsyncSession

from app.common.exceptions import AuthenticationError, ConflictError, ValidationError
from app.config.settings import Settings
from app.domain.enums import UserStatus
from app.infrastructure.auth.jwt import create_access_token, decode_access_token
from app.infrastructure.auth.password import DUMMY_HASH, hash_password, verify_password
from app.models.user import User
from app.repositories.user_repository import UserRepository
from app.schemas.auth import LoginRequest, PasswordChangeRequest
from app.schemas.user import UserCreate

__all__ = ["AuthService"]

logger = logging.getLogger(__name__)


class AuthService:
    def __init__(self, session: AsyncSession, settings: Settings) -> None:
        self._session = session
        self._settings = settings
        self._users = UserRepository(session)

    # ------------------------------------------------------------------ 注册

    async def register(self, payload: UserCreate) -> User:
        # 邮箱统一小写后落库，否则唯一索引挡不住 "A@x.com" 和 "a@x.com" 这种重复
        email = payload.email.strip().lower()
        if await self._users.email_exists(email):
            raise ConflictError(
                "Email already registered",
                code="EMAIL_ALREADY_REGISTERED",
                details={"email": email},
            )

        user = User(
            email=email,
            password_hash=hash_password(payload.password),
            display_name=payload.display_name.strip(),
            status=UserStatus.ACTIVE.value,
            token_version=0,
        )
        await self._users.add(user)
        await self._session.commit()
        logger.info("user registered | id=%s email=%s", user.id, user.email)
        return user

    # ------------------------------------------------------------------ 登录

    async def authenticate(self, payload: LoginRequest) -> User:
        """校验邮箱 + 密码。

        关键取舍：**邮箱不存在和密码错误返回完全相同的 code 与 message**。
        否则这个接口就变成了「哪些邮箱已注册」的枚举器 —— 攻击者拿一份邮箱
        字典就能筛出有效账号，再针对性撞库。
        """
        email = payload.email.strip().lower()
        user = await self._users.get_by_email(email)

        if user is None:
            # 仍然跑一次哈希校验。不跑的话「用户不存在」会比「密码错误」快
            # 一个数量级，响应时间本身又把账号枚举泄露回去了。
            verify_password(payload.password, DUMMY_HASH)
            raise self._invalid_credentials()

        if not verify_password(payload.password, user.password_hash):
            raise self._invalid_credentials()

        if user.status != UserStatus.ACTIVE:
            # 这里可以明确报「账号已停用」：走到这一步密码已经验证通过，
            # 说明对方确实拥有这个账号，不存在枚举风险。
            raise AuthenticationError("Account is disabled", code="ACCOUNT_DISABLED")

        logger.info("user logged in | id=%s", user.id)
        return user

    @staticmethod
    def _invalid_credentials() -> AuthenticationError:
        return AuthenticationError("Email or password is incorrect", code="INVALID_CREDENTIALS")

    def issue_token(self, user: User) -> tuple[str, int]:
        """签发访问令牌，返回 ``(token, expires_in_seconds)``。"""
        return create_access_token(
            user_id=user.id,
            token_version=user.token_version,
            settings=self._settings,
        )

    # -------------------------------------------------------- 令牌 -> 当前用户

    async def authenticate_token(self, token: str) -> User:
        """把 Bearer 令牌换成用户实体。这是所有受保护接口的入口。"""
        payload = decode_access_token(token, self._settings)

        user = await self._users.get(payload.user_id)
        if user is None:
            raise AuthenticationError("Access token subject no longer exists", code="TOKEN_SUBJECT_NOT_FOUND")

        # 会话撤销的落点：令牌里的版本落后于用户当前版本，说明它已被登出或改密码作废
        if user.token_version != payload.token_version:
            raise AuthenticationError("Access token has been revoked", code="TOKEN_REVOKED")

        if user.status != UserStatus.ACTIVE:
            raise AuthenticationError("Account is disabled", code="ACCOUNT_DISABLED")

        return user

    # ------------------------------------------------------------------ 撤销

    async def revoke_all_sessions(self, user: User) -> int:
        """作废该用户已签发的全部令牌，返回自增后的 ``token_version``。

        粒度是「这个人的所有设备」。要做「只踢掉某一台设备」需要改成
        服务端会话表，届时 ``/auth/login``、``/auth/logout`` 的路径与语义都不用变，
        只是把用户级撤销收紧成会话级。
        """
        user.token_version += 1
        await self._session.commit()
        logger.info("sessions revoked | user_id=%s token_version=%s", user.id, user.token_version)
        return user.token_version

    async def change_password(self, user: User, payload: PasswordChangeRequest) -> int:
        """改密码，并连带作废全部旧会话。

        这一步是 token_version 方案真正值钱的地方：密码泄露后改密码能把攻击者
        立刻踢出去，而不是等令牌自然过期。「jti 黑名单」方案要么做不到，
        要么得把所有在途令牌逐个加黑名单。
        """
        if not verify_password(payload.current_password, user.password_hash):
            raise AuthenticationError("Current password is incorrect", code="INVALID_CREDENTIALS")

        if verify_password(payload.new_password, user.password_hash):
            raise ValidationError("New password must differ from the current one", code="PASSWORD_UNCHANGED")

        user.password_hash = hash_password(payload.new_password)
        user.token_version += 1
        await self._session.commit()
        logger.info("password changed, sessions revoked | user_id=%s", user.id)
        return user.token_version
