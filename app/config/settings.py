"""应用配置。

所有可变参数集中在这里，通过环境变量或 .env 覆盖。
Layer: Common —— 只提供配置值，不承载业务逻辑。
"""

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


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

    # ---------- 认证（Stage 2 生效）----------
    jwt_secret_key: str = "change-me-before-any-real-use"
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


@lru_cache
def get_settings() -> Settings:
    return Settings()
