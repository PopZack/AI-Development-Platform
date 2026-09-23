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
    # 默认 SQLite；换 MySQL / PostgreSQL 时只改这里与驱动，Repository/Service 接口不变
    database_url: str = "sqlite+aiosqlite:///./data/ai_dev_team.db"
    sql_echo: bool = False
    # 启动时自动建表（本地/测试方便）。
    # ⚠️ 生产必须关掉并用 `alembic upgrade head`：create_all **只建缺失的表，
    #    不会给已有表加列** —— 发新版本时它会静默什么都不做，然后应用在运行中
    #    报 "no such column"，而且看起来像代码 bug 而不是迁移漏了。
    auto_create_tables: bool = True

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
    # Agent 一次调用实测 20~40 秒（PRD / 架构这类长输出），60s 会超时。
    # 只作用于 LLM 的 HTTP 客户端，不影响其它接口的响应时间。
    llm_timeout_seconds: float = 180.0
    llm_max_retries: int = 2

    # ---------- 本地工作区（Stage 4 生效）----------
    # Tool Gateway 的授权根目录；所有文件工具路径必须规范化后落在它内部
    workspace_root: str = "./workspace"

    # ---------- 限流（Stage 5：多人部署的前置条件）----------
    rate_limit_enabled: bool = True
    rate_limit_window_seconds: int = 60
    # 默认额度按「一个页面正常操作」估：列表 / 详情 / 查看交付物都不会撞到
    rate_limit_default_per_minute: int = 300
    # 最贵的资源：一次 20~40 秒 + 真金白银的 token
    rate_limit_llm_per_minute: int = 10
    # 反口令爆破，按 IP
    rate_limit_auth_per_minute: int = 20
    # 挂在反向代理后面时必须开启，否则所有请求都算在代理的 IP 上
    # （等于把全站限成一份额度，第一个用户就把额度用光）
    rate_limit_trust_proxy: bool = False
    # Redis 挂了是放行还是拒绝。默认放行：让保护措施的故障变成全站不可用，
    # 是拿小风险换大风险。要更严就改成 False
    rate_limit_fail_open: bool = True

    # ---------- Redis（可选；多 worker 部署需要）----------
    # 留空 = 单进程模式：限流走内存、事件走进程内广播。
    # 多 worker 时不配 Redis 会出两个问题：限额被乘以 worker 数、
    # SSE 事件只在产生它的那个 worker 上可见。启动时会告警。
    redis_url: str = ""
    # SSE 心跳间隔：代理（nginx 默认 60s）会掐掉长时间无数据的连接
    sse_heartbeat_seconds: float = 15.0

    @property
    def is_sqlite(self) -> bool:
        return self.database_url.startswith("sqlite")

    @model_validator(mode="after")
    def _guard_production_config(self) -> Settings:
        """生产环境不许带着占位配置跑起来。

        这条护栏写在配置层而不是部署脚本里：配置错了就应该起不来，
        而不是带着一个众所周知的口令、或者一个永远返回假数据的 Mock
        安静地跑在生产上。本地/测试环境不受影响 —— 否则开发时会被它拦住。
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

        if self.llm_provider.strip().lower() == "mock":
            # Mock 返回固定内容。生产上用它等于整个平台在演假戏，
            # 而且从接口响应上完全看不出来
            raise ValueError("LLM_PROVIDER=mock must not be used with APP_ENV=prod")

        if not self.llm_api_key.strip() or not self.llm_model.strip():
            raise ValueError("LLM_API_KEY and LLM_MODEL must be set before APP_ENV=prod")

        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
