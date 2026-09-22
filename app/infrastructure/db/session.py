"""数据库引擎与会话管理。

Layer: Infrastructure。

事务边界（文档 §5.3）：**Service 负责 commit**，这里不自动提交。
``get_session`` 只保证「异常回滚 + 用完关闭」。如果某个 Service 忘了
commit，改动会被丢弃 —— 这是刻意的，强制事务边界显式可见。

同理，绝不能在持有事务时等待 LLM 返回：先提交状态，再调外部服务，
最后开新事务保存结果。
"""

from __future__ import annotations

from collections.abc import AsyncGenerator
from pathlib import Path

from sqlalchemy import event
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import Pool

from app.config.settings import Settings, get_settings
from app.infrastructure.db.base import Base

__all__ = [
    "configure_database",
    "get_engine",
    "get_session_factory",
    "get_session",
    "create_all",
    "dispose_engine",
]

_engine: AsyncEngine | None = None
_session_factory: async_sessionmaker[AsyncSession] | None = None


def _ensure_sqlite_directory(settings: Settings) -> None:
    """SQLite 不会自动创建父目录，``./data/x.db`` 在 data/ 不存在时会直接报错。"""
    url = settings.database_url
    if not url.startswith("sqlite"):
        return
    if ":memory:" in url or "mode=memory" in url:
        return
    _, _, raw_path = url.partition("///")
    if not raw_path:
        return
    Path(raw_path).expanduser().resolve().parent.mkdir(parents=True, exist_ok=True)


def _enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """让 SQLite 真正执行外键约束。

    ⚠️ **SQLite 默认不强制外键**（``PRAGMA foreign_keys`` 默认为 OFF）。
    不开这个开关，模型上写的 ``ondelete="CASCADE"`` / ``"SET NULL"`` 全都是装饰：
    删掉一条需求时 agent_runs / artifacts 不会被级联清理，会留下指向不存在记录的
    孤儿数据；而且插入一条指向不存在外键的行也会被默默接受。

    这类问题在写入侧完全看不出来，只在真正删数据或做完整性核查时才暴露 ——
    声明了却不生效的约束，比一开始就不声明更危险，因为它让人以为有保护。

    挂在具体 engine 实例上而不是全局 ``Engine`` 类上：避免影响本项目之外的
    任何引擎（测试里也会造独立引擎）。
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_engine(settings: Settings | None = None, *, poolclass: type[Pool] | None = None) -> AsyncEngine:
    """按配置造引擎。测试里用它造独立引擎。

    ``poolclass`` 的用途很具体：测试要用 ``NullPool``。**pytest-asyncio 每个用例
    跑在新的事件循环里**，而池化连接是绑在创建它那个循环上的，跨循环复用会直接失效
    —— 表现是时好时坏的 "attached to a different loop"。

    刻意让测试也走这个函数（而不是自己调 ``create_async_engine``）：
    否则两边建出的引擎行为不一致，PRAGMA 之类的连接级设置就只在生产生效，
    测试测的其实是另一套东西。
    """
    settings = settings or get_settings()
    _ensure_sqlite_directory(settings)
    engine = create_async_engine(
        settings.database_url,
        echo=settings.sql_echo,
        future=True,
        pool_pre_ping=True,
        **({"poolclass": poolclass} if poolclass is not None else {}),
    )
    if settings.database_url.startswith("sqlite"):
        _enable_sqlite_foreign_keys(engine)
    return engine


def configure_database(engine: AsyncEngine) -> None:
    """绑定全局引擎。应用启动或测试 setup 时调用一次。"""
    global _engine, _session_factory
    _engine = engine
    _session_factory = async_sessionmaker(engine, expire_on_commit=False, autoflush=False)


def get_engine() -> AsyncEngine:
    if _engine is None:
        raise RuntimeError("Database engine is not configured; call configure_database() first")
    return _engine


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is None:
        raise RuntimeError("Session factory is not configured; call configure_database() first")
    return _session_factory


async def get_session() -> AsyncGenerator[AsyncSession, None]:
    """FastAPI 依赖：每请求一个会话。不自动 commit，不自动 flush。"""
    factory = get_session_factory()
    async with factory() as session:
        try:
            yield session
        except Exception:
            await session.rollback()
            raise


async def create_all() -> None:
    """按 ORM 元数据建表。

    Stage 1 的建表方式。文档 §11 把 Alembic 放在 Stage 5，所以首期
    不写迁移脚本 —— 换库或改表时直接重建本地 SQLite 即可。
    """
    # 必须先把所有模型 import 进来，metadata 才是完整的
    import app.models  # noqa: F401

    engine = get_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)


async def dispose_engine() -> None:
    if _engine is not None:
        await _engine.dispose()
