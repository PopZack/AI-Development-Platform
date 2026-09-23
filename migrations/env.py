"""Alembic 迁移环境。

Layer: 工具链。

三件事需要说明：

1. **数据库 URL 从应用配置读**（``app.config.settings.Settings``），不写在
   ``alembic.ini`` 里。迁移和应用必须连同一个库，两处各配一次迟早漂移，
   而漂移的表现是「迁移跑在 A 库、应用连的是 B 库」—— 极难排查。
2. **显式导入 ``app.models``**。``Base.metadata`` 是靠「模型类被导入」才填充的；
   漏掉这一步，autogenerate 会认为所有表都是多余的，生成一个把库删干净的迁移。
3. **SQLite 用 batch 模式渲染**（``render_as_batch``）。SQLite 的
   ``ALTER TABLE`` 能力极弱（不能改列、不能加约束），batch 模式用「建新表 +
   拷数据 + 换名」来模拟。不开启的话，将来第一次改列就会失败。
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

import app.models  # noqa: F401  —— 必须导入：它负责把模型注册进 Base.metadata
from app.config.settings import Settings
from app.infrastructure.db.base import Base

config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _database_url() -> str:
    return Settings().database_url


def run_migrations_offline() -> None:
    """生成 SQL 而不连库（``alembic upgrade head --sql``）。"""
    context.configure(
        url=_database_url(),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def _run(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        # SQLite 的 ALTER 能力有限，用 batch 模式模拟（见模块文档第 3 条）
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    engine = create_async_engine(_database_url(), poolclass=pool.NullPool)
    async with engine.connect() as connection:
        await connection.run_sync(_run)
    await engine.dispose()


if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
