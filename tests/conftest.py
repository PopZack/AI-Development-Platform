"""pytest 公共夹具。

测试数据库选 **每个用例一个临时文件库 + NullPool**，而不是内存库。

内存 SQLite 有个坑：它只属于「创建它的那条连接」，而连接池会为每个会话
开新连接，于是第二个请求就看到「表不存在」。常见解法是 StaticPool 强制
复用同一条连接 —— 但那样多个 Session 会共享同一个 DBAPI 连接，测试里
只要一边开着事务、另一边又去写，事务就会互相串扰，报出来的错还很难懂。

文件库 + NullPool 让每个会话都拿到独立连接：事务边界是真实的，回滚是真的
回滚，和不内存库的取舍换来的是「测试失败时错误信息指向真正的问题」。

另外这里刻意 **不触发应用的 lifespan**（httpx 的 ASGITransport 默认不跑
lifespan），避免它把全局引擎换成配置里的文件库、把测试数据写到仓库里。
建表由 engine 夹具显式完成。
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import Settings
from app.infrastructure.db.base import Base
from app.infrastructure.db.session import configure_database, get_session_factory
from app.main import create_app

API = "/api/v1"
DEFAULT_PASSWORD = "StrongPass123!"


@pytest.fixture
def test_settings(tmp_path: Path) -> Settings:
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"
    return Settings(
        app_env="test",
        # 关掉 debug 只为让测试输出干净（debug 会把根 logger 降到 DEBUG，
        # asyncio / aiosqlite 的马达日志会淹没真正的失败信息）
        debug=False,
        database_url=database_url,
        sql_echo=False,
        llm_provider="ark",
        llm_api_key="test-key",
        llm_model="test-model",
    )


@pytest.fixture
async def engine(test_settings: Settings) -> AsyncIterator[Any]:
    engine = create_async_engine(test_settings.database_url, poolclass=NullPool)
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
    yield engine
    await engine.dispose()


@pytest.fixture
async def app(engine: Any, test_settings: Settings) -> AsyncIterator[Any]:
    configure_database(engine)
    yield create_app(test_settings)


@pytest.fixture
async def client(app: Any) -> AsyncIterator[AsyncClient]:
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver") as client:
        yield client


@pytest.fixture
async def db_session(engine: Any) -> AsyncIterator[AsyncSession]:
    """直连数据库，用来验证「数据真的落库了」而不是只被响应体糊过去。"""
    factory: async_sessionmaker[AsyncSession] = get_session_factory()
    async with factory() as session:
        yield session


# ---------- 造数据的夹具：让每个用例只写「与它断言相关」的那几个字段 ----------

MakeUser = Callable[..., Awaitable[dict[str, Any]]]
MakeProject = Callable[..., Awaitable[dict[str, Any]]]
MakeRequirement = Callable[..., Awaitable[dict[str, Any]]]


@pytest.fixture
def make_user(client: AsyncClient) -> MakeUser:
    async def _make(**overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "email": f"user-{uuid4().hex[:8]}@example.com",
            "password": DEFAULT_PASSWORD,
            "display_name": "Test User",
        }
        payload.update(overrides)
        response = await client.post(f"{API}/users", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return _make


@pytest.fixture
def make_project(client: AsyncClient) -> MakeProject:
    async def _make(owner_id: str, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": "Todo API Demo", "owner_id": owner_id}
        payload.update(overrides)
        response = await client.post(f"{API}/projects", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return _make


@pytest.fixture
def make_requirement(client: AsyncClient) -> MakeRequirement:
    async def _make(project_id: str, created_by: str, **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": "实现 Todo API",
            "description": "使用 FastAPI 和 SQLAlchemy 实现 Todo 的增删改查。",
            "priority": "P0",
            "acceptance_criteria": ["可以创建 Todo", "可以查询 Todo 列表"],
            "created_by": created_by,
        }
        payload.update(overrides)
        response = await client.post(f"{API}/projects/{project_id}/requirements", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return _make
