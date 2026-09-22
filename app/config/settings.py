"""应用配置。

所有可变参数集中在这里，通过环境变量或 .env 覆盖。
Layer: Common —— 只提供配置值，不承载业务逻辑。
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# 默认密钥只允许在本地/测试环境使用。写成常量而不是散落的字面量，
# 这样「配置校验」和「.env.example」引用的是同一个值，不会各写一遍后走偏。
# 长度刻意超过 32 字节：RFC 7518 §3.2 要求 HS256 的密钥不短于哈希输出长度，
# 否则 PyJWT 每次调用都会抛 InsecureKeyLengthWarning —— 本地一堆黄色警告
# 的结果是所有人都学会无视它。
DEFAULT_JWT_SECRET = "change-me-before-any-real-use-32bytes-min"

# HS256 的密钥长度下限（字节）
MIN_JWT_SECRET_BYTES = 32


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ---------- 应用 ----------
    app_name: str = "AI Dev Team"
    app_env: Literal["local", "test", "prod"] = "local"
    debug: bool = True
    api_v1_prefix: str = "/api/v1"

    # ---------- 数据库 ----------
    # Stage 1 用 SQLite；换 MySQL 时只改这里与驱动，Repository/Service 接口不变
    database_url: str = "sqlite+aiosqlite:///./data/ai_dev_team.db"
    sql_echo: bool = False

    # ---------- 认证 ----------
    jwt_secret_key: str = DEFAULT_JWT_SECRET
    jwt_algorithm: str = "HS256"
    access_token_expire_minutes: int = 60

    # ---------- LLM Provider（Stage 3 生效）----------
    # provider 名决定装配哪个实现；Agent 层只依赖抽象，不依赖具体供应商
    llm_provider: str = "ark"
    llm_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    llm_api_key: str = ""
    llm_model: str = ""
    llm_timeout_seconds: float = 60.0
    llm_max_retries: int = 2

    # ---------- 本地工作区（Stage 4 生效）----------
    # Tool Gateway 的授权根目录；所有文件工具路径必须规范化后落在它内部
    workspace_root: str = "./workspace"

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @model_validator(mode="after")
    def _guard_production_secrets(self) -> Settings:
        """生产环境绝不允许用默认密钥或过短的密钥 —— 否则令牌可被伪造。

        这条护栏写在配置层而不是部署脚本里：配置错了就应该起不来，
        而不是带着一个众所周知的口令安静地跑起来。
        """
        if self.app_env != "prod":
            return self

        if self.jwt_secret_key == DEFAULT_JWT_SECRET:
            raise ValueError(
                "JWT_SECRET_KEY is still the default value; set a real secret before APP_ENV=prod"
            )

        actual_bytes = len(self.jwt_secret_key.encode("utf-8"))
        if actual_bytes < MIN_JWT_SECRET_BYTES:
            raise ValueError(
                f"JWT_SECRET_KEY must be at least {MIN_JWT_SECRET_BYTES} bytes "
                f"for {self.jwt_algorithm}, got {actual_bytes}"
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
