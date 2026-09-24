"""FastAPI 应用入口。

启动顺序：装日志 → 建应用 → 挂 request_id 中间件 → 注册统一异常处理器
→ 挂 v1 路由。数据库连接与建表放在 lifespan 里，保证「应用能用」和
「应用已启动」是同一件事。

本地启动：
    uv run uvicorn app.main:app --reload
Swagger：
    http://127.0.0.1:8000/docs
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI

from app.api.router import api_router
from app.application.workflow_tasks import WorkflowTaskManager, recover_orphaned_runs
from app.common.error_handlers import register_exception_handlers
from app.common.rate_limit import (
    TIER_AUTH,
    TIER_DEFAULT,
    TIER_LLM,
    InMemoryRateLimiter,
    RateLimitMiddleware,
    RedisRateLimiter,
)
from app.common.request_context import RequestContextMiddleware
from app.common.security import mask_url
from app.config.logging import setup_logging
from app.config.settings import Settings, get_settings
from app.infrastructure.db.session import (
    configure_database,
    create_all,
    create_engine,
    dispose_engine,
)
from app.infrastructure.events import EventBus, configure_event_bus
from app.infrastructure.llm.factory import create_llm_provider
from app.infrastructure.redis import create_redis

logger = logging.getLogger(__name__)

DESCRIPTION = """
AI Dev Team —— AI 软件开发团队协作平台。

已实现：JWT 认证与会话撤销、Owner/Developer/Viewer 项目级权限、
用户 / 项目 / 需求 / 工作流 / 审批接口、LLM Provider 抽象、
五个 Agent（Product / Architect / Developer / Tester / Reviewer）、
显式工作流状态机与执行编排、Tool Gateway（L0~L5 权限 + 路径校验 + 审计）、
人工审批闭环、交付物汇总、SSE 实时事件、限流。

Web UI：`/ui`
""".strip()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings
    llm_provider = app.state.llm_provider
    bus: EventBus = app.state.event_bus
    redis_client = app.state.redis

    configure_database(create_engine(settings))
    if settings.auto_create_tables:
        await create_all()
    else:
        logger.info("AUTO_CREATE_TABLES=false：表结构由 alembic 管理（alembic upgrade head）")
    await bus.start()

    # 进程重启后把卡在执行中的运行标记为 FAILED/INTERRUPTED（别的 worker
    # 正在执行的会被运行锁跳过）
    await recover_orphaned_runs(
        skip_running=app.state.workflow_tasks.active_run_ids(),
        redis=redis_client,
    )

    logger.info(
        "%s started | env=%s | db=%s | llm_provider=%s | events=%s",
        settings.app_name,
        settings.app_env,
        # ⚠️ 必须脱敏：连接串里带密码，而日志会被采集、转发、长期保存。
        # 这条日志原本把 MySQL 密码明文打了 4 遍（每个 worker 一行），
        # 一旦接上日志收集就等于把库密码公布出去。
        mask_url(settings.database_url),
        llm_provider.name,
        "redis" if redis_client is not None else "in-process",
    )
    _warn_about_single_process_setup(settings, redis_client)
    try:
        yield
    finally:
        # Provider 可能持有连接池，不关会留下未回收的 socket
        await llm_provider.aclose()
        await bus.stop()
        if redis_client is not None:
            await redis_client.aclose()
        await dispose_engine()
        logger.info("%s stopped", settings.app_name)


def _warn_about_single_process_setup(settings: Settings, redis_client: object | None) -> None:
    """多 worker 但没配 Redis 时，说清楚会出什么问题。

    刻意只告警不阻止启动：单 worker 生产部署是合理的，
    而「多 worker」这个事实只有运维知道 —— 文档说清楚 + 日志提醒，
    比在配置层猜一个 worker 数更诚实。
    """
    if redis_client is None:
        if settings.redis_url.strip():
            # ⚠️ 「配了 URL 却拿不到客户端」与「没配」是两件事，不能报同一句话。
            # 原来这两种情况都打 "REDIS_URL is not set"，于是运维照着这句话去查环境变量，
            # 永远查不出真正的原因是**依赖没装**（redis 是可选依赖，compose 一度漏装，
            # 结果部署时配了 REDIS_URL 仍然静默退化成进程内状态）。
            logger.error(
                "REDIS_URL is set but no Redis client could be created — "
                "most likely the 'redis' package is not installed (`uv sync --extra redis`). "
                "Falling back to per-process rate limiting and event broadcast: "
                "with multiple workers the quota is multiplied by the worker count and "
                "SSE events are visible only on the worker that produced them."
            )
        else:
            logger.warning(
                "REDIS_URL is not set: rate limiting is per-process and SSE events are "
                "visible only on the worker that produced them. "
                "Set REDIS_URL before running with multiple workers."
            )
    if not settings.rate_limit_enabled:
        logger.warning("rate limiting is disabled (RATE_LIMIT_ENABLED=false)")
    if settings.app_env == "prod" and settings.debug:
        # DEBUG 会把根 logger 降到 DEBUG，于是 httpx / sqlalchemy 的内部日志全进日志流：
        # 一行业务日志配几十行 httpcore 的 connect/tls 细节，既费存储又淹没真正的错误。
        logger.warning(
            "APP_ENV=prod 且 DEBUG=true：日志级别被降到 DEBUG，"
            "httpx / sqlalchemy 的内部细节会全部进日志流，建议关掉"
        )
    if settings.app_env == "prod" and settings.auto_create_tables:
        logger.warning(
            "APP_ENV=prod 且 AUTO_CREATE_TABLES=true：create_all 不会给已有表加列，"
            "发版时必须靠 `alembic upgrade head` 迁移，建议把 AUTO_CREATE_TABLES 设为 false"
        )


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    setup_logging(debug=settings.debug, sql_echo=settings.sql_echo)

    app = FastAPI(
        title=settings.app_name,
        version="0.1.0",
        description=DESCRIPTION,
        lifespan=lifespan,
        # 刻意不跟随 settings.debug：一旦开启，Starlette 会用自己的 HTML
        # traceback 页面顶掉下面注册的统一异常处理器，把内部堆栈直接暴露给
        # 客户端 —— 文档 §9.2 明确要求不能这样。本地排查看日志里的堆栈。
        debug=False,
    )
    app.state.settings = settings
    # Provider 在这里装配（而不是在 lifespan 里）是为了让测试能替换：
    # app.state.llm_provider = MockLLMProvider(script=[...]) 即可，无需改环境变量
    app.state.llm_provider = create_llm_provider(settings)

    # Redis 可选：装了就用（多 worker 部署需要），没配就降级成单进程模式
    redis_client = create_redis(settings.redis_url)
    app.state.redis = redis_client
    bus = EventBus(redis=redis_client)
    app.state.event_bus = bus
    # 后台工作流执行：注册表 + 跨 worker 运行锁（redis 未配置时退化为进程内）
    app.state.workflow_tasks = WorkflowTaskManager(
        workspace_root=Path(settings.workspace_root),
        redis=redis_client,
    )
    configure_event_bus(bus)

    # 限流器同样二选一：有 Redis 才能在多 worker 之间共享计数
    limiter = RedisRateLimiter(redis_client) if redis_client is not None else InMemoryRateLimiter()
    app.add_middleware(
        RateLimitMiddleware,
        limiter=limiter,
        limits={
            TIER_DEFAULT: settings.rate_limit_default_per_minute,
            TIER_LLM: settings.rate_limit_llm_per_minute,
            TIER_AUTH: settings.rate_limit_auth_per_minute,
        },
        window_seconds=settings.rate_limit_window_seconds,
        enabled=settings.rate_limit_enabled,
        trust_proxy=settings.rate_limit_trust_proxy,
        fail_open=settings.rate_limit_fail_open,
    )

    # 中间件顺序：后 add 的在外层。request_id 必须在最外层生成，
    # 这样被限流的响应里也带着 request_id（最需要对着日志查的情况）
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/health", tags=["meta"], summary="健康检查")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name, "env": settings.app_env}

    _mount_web_ui(app)

    return app


def _mount_web_ui(app: FastAPI) -> None:
    """挂载前端静态资源（Stage 5 的 Web UI）。

    目录不存在时静默跳过：后端不该因为前端还没构建而无法启动 ——
    这正是「可选增强不阻塞核心闭环」的意思。
    """
    from pathlib import Path

    from fastapi.responses import RedirectResponse
    from fastapi.staticfiles import StaticFiles

    web_dir = Path(__file__).resolve().parent.parent / "web"
    if not (web_dir / "index.html").is_file():
        logger.info("web UI not mounted (no %s/index.html)", web_dir)
        return

    app.mount("/ui", StaticFiles(directory=web_dir, html=True), name="ui")

    @app.get("/", include_in_schema=False)
    async def root() -> RedirectResponse:
        return RedirectResponse("/ui/")


app = create_app()
