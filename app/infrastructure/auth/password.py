"""密码哈希。

Layer: Infrastructure —— 具体技术选型（Argon2）藏在这里，
Service 只知道「有个函数能把密码变哈希、能校验哈希」。

文档 §6.3 要求：「密码只保存安全哈希结果，不能保存明文密码」。
"""

from __future__ import annotations

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

__all__ = ["hash_password", "verify_password", "DUMMY_HASH"]

# 参数用 argon2-cffi 的默认值（time_cost=3, memory_cost=64MiB, parallelism=4），
# 已是 OWASP 推荐区间，Stage 1 不自定义
_hasher = PasswordHasher()

# 用于「用户不存在」时也跑一次校验，避免通过响应时间枚举出哪些邮箱已注册
DUMMY_HASH = _hasher.hash("dummy-password-for-timing-equalization")


def hash_password(plain_password: str) -> str:
    return _hasher.hash(plain_password)


def verify_password(plain_password: str, password_hash: str) -> bool:
    """校验失败一律返回 False，不区分「密码错」和「哈希格式坏」。"""
    try:
        return _hasher.verify(password_hash, plain_password)
    except (VerifyMismatchError, VerificationError, InvalidHashError):
        return False
