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

from fastapi import FastAPI

from app.api.router import api_router
from app.common.error_handlers import register_exception_handlers
from app.common.request_context import RequestContextMiddleware
from app.config.logging import setup_logging
from app.config.settings import Settings, get_settings
from app.infrastructure.db.session import (
    configure_database,
    create_all,
    create_engine,
    dispose_engine,
)

logger = logging.getLogger(__name__)

DESCRIPTION = """
AI Dev Team —— 本地 AI 软件开发团队协作平台。

当前实现进度：**Stage 1（基础 API + 业务数据）**。

已实现：用户 / 项目 / 项目成员 / 需求的 CRUD、统一错误格式、request_id 贯穿。
未实现（按设计文档的分阶段计划推进）：JWT 认证与资源级权限（Stage 2）、
Agent Runtime 与 PRD / 架构生成（Stage 3）、工作流状态机 + Tool Gateway + 审批（Stage 4）。
""".strip()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    settings: Settings = app.state.settings

    configure_database(create_engine(settings))
    await create_all()

    logger.info(
        "%s started | env=%s | db=%s",
        settings.app_name,
        settings.app_env,
        settings.database_url,
    )
    try:
        yield
    finally:
        await dispose_engine()
        logger.info("%s stopped", settings.app_name)


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

    # 中间件要在路由之前挂，否则 request_id 覆盖不到最早的异常
    app.add_middleware(RequestContextMiddleware)
    register_exception_handlers(app)
    app.include_router(api_router, prefix=settings.api_v1_prefix)

    @app.get("/health", tags=["meta"], summary="健康检查")
    async def health() -> dict[str, str]:
        return {"status": "ok", "app": settings.app_name, "env": settings.app_env}

    return app


app = create_app()
