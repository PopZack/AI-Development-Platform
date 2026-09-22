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
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.pool import NullPool

from app.config.settings import Settings
from app.infrastructure.db.base import Base
from app.infrastructure.db.session import configure_database, get_session_factory
from app.main import create_app
from app.models.user import User

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


@pytest.fixture
async def set_user_status(db_session: AsyncSession) -> Callable[..., Awaitable[None]]:
    """直接改库设置用户状态。

    账号停用**没有 API 入口** —— 设计文档只定义了项目级角色，没有全局管理员，
    不凭空造一个。但 ``ACCOUNT_DISABLED`` 这条分支仍然需要被覆盖，所以测试
    绕开接口直接改库（等价于运维手工处理），而不是为了测试方便去开一个后门接口。
    """

    async def _set(user_id: str, status: str) -> None:
        user = await db_session.get(User, UUID(user_id))
        assert user is not None, f"user {user_id} not found"
        user.status = status
        await db_session.commit()

    return _set


# ---------- 造数据的夹具：让每个用例只写「与它断言相关」的那几个字段 ----------

MakeUser = Callable[..., Awaitable[dict[str, Any]]]
MakeProject = Callable[..., Awaitable[dict[str, Any]]]
MakeRequirement = Callable[..., Awaitable[dict[str, Any]]]
MakeActor = Callable[..., Awaitable[dict[str, Any]]]
Login = Callable[..., Awaitable[dict[str, Any]]]
AuthHeaders = Callable[..., Awaitable[dict[str, str]]]


@pytest.fixture
def make_user(client: AsyncClient) -> MakeUser:
    """走真实的注册接口，而不是直接写库 —— 否则注册链路的校验规则不会被覆盖。"""

    async def _make(**overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "email": f"user-{uuid4().hex[:8]}@example.com",
            "password": DEFAULT_PASSWORD,
            "display_name": "Test User",
        }
        payload.update(overrides)
        response = await client.post(f"{API}/auth/register", json=payload)
        assert response.status_code == 201, response.text
        return response.json()

    return _make


@pytest.fixture
def login(client: AsyncClient) -> Login:
    """邮箱 + 密码换访问令牌。"""

    async def _login(email: str, password: str = DEFAULT_PASSWORD) -> dict[str, Any]:
        response = await client.post(f"{API}/auth/login", json={"email": email, "password": password})
        assert response.status_code == 200, response.text
        return response.json()

    return _login


@pytest.fixture
def auth_headers(login: Login) -> AuthHeaders:
    """``auth_headers(email)`` → ``{"Authorization": "Bearer ..."}``，直接展开进请求。"""

    async def _headers(email: str, password: str = DEFAULT_PASSWORD) -> dict[str, str]:
        token = (await login(email, password))["access_token"]
        return {"Authorization": f"Bearer {token}"}

    return _headers


@pytest.fixture
def make_actor(client: AsyncClient, auth_headers: AuthHeaders) -> MakeActor:
    """一次造出「用户 + 他的请求头」，省掉每个用例都写两行。"""

    async def _make(**overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "email": f"user-{uuid4().hex[:8]}@example.com",
            "password": DEFAULT_PASSWORD,
            "display_name": "Test User",
        }
        payload.update(overrides)
        response = await client.post(f"{API}/auth/register", json=payload)
        assert response.status_code == 201, response.text
        user = response.json()
        return {"user": user, "headers": await auth_headers(user["email"], payload["password"])}

    return _make


@pytest.fixture
def make_project(client: AsyncClient) -> MakeProject:
    """``make_project(headers, **overrides)`` —— owner 由令牌决定，不再需要传 id。"""

    async def _make(headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {"name": "Todo API Demo"}
        payload.update(overrides)
        response = await client.post(f"{API}/projects", json=payload, headers=headers)
        assert response.status_code == 201, response.text
        return response.json()

    return _make


@pytest.fixture
def make_requirement(client: AsyncClient) -> MakeRequirement:
    """``make_requirement(project_id, headers, **overrides)`` —— 创建人由令牌决定。"""

    async def _make(project_id: str, headers: dict[str, str], **overrides: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "title": "实现 Todo API",
            "description": "使用 FastAPI 和 SQLAlchemy 实现 Todo 的增删改查。",
            "priority": "P0",
            "acceptance_criteria": ["可以创建 Todo", "可以查询 Todo 列表"],
        }
        payload.update(overrides)
        response = await client.post(
            f"{API}/projects/{project_id}/requirements", json=payload, headers=headers
        )
        assert response.status_code == 201, response.text
        return response.json()

    return _make


@pytest.fixture
def make_project_with_member(
    client: AsyncClient, make_project: object, make_actor: object
) -> Callable[..., Awaitable[tuple[dict[str, Any], dict[str, Any], dict[str, Any]]]]:
    """一次造出「OWNER + 项目 + 另一个已加入的成员」，返回三方，省掉权限类用例的样板。

    返回 ``(owner_actor, project, member_actor)``。
    """

    async def _make(role: str = "DEVELOPER", **project_overrides: Any) -> tuple[Any, Any, Any]:
        owner = await make_actor()
        project = await make_project(owner["headers"], **project_overrides)
        member = await make_actor()
        response = await client.post(
            f"{API}/projects/{project['id']}/members",
            json={"user_id": member["user"]["id"], "role": role},
            headers=owner["headers"],
        )
        assert response.status_code == 201, response.text
        return owner, project, member

    return _make
