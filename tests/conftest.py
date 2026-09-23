"""pytest 公共夹具。

测试数据库选 **每个用例一个临时文件库 + NullPool**，而不是内存库。

内存 SQLite 有个坑：它只属于「创建它的那条连接」，而连接池会为每个会话
开新连接，于是第二个请求就看到「表不存在」。常见解法是 StaticPool 强制
复用同一条连接 —— 但那样多个 Session 会共享同一个 DBAPI 连接，测试里
只要一边开着事务、另一边又去写，事务就会互相串扰，报出来的错还很难懂。

文件库 + NullPool 让每个会话都拿到独立连接：事务边界是真实的，回滚是真的
回滚，和不内存库的取舍换来的是「测试失败时错误信息指向真正的问题」。

**真数据库模式（T1）**：设 ``TEST_DATABASE_URL`` 环境变量即可把
integration / api 套件跑在真 MySQL 上，模板里用 ``{dbname}`` 占位：

    TEST_DATABASE_URL='mysql+aiomysql://root:密码@127.0.0.1:13306/{dbname}?charset=utf8mb4' \\
        pytest tests/integration tests/api

每个用例 **CREATE DATABASE 一个独立库**（毫秒级），用完 DROP。之前设想的
「整轮建一次库 + 用例间 TRUNCATE」没有采用：它要处理外键检查开关、
TRUNCATE 顺序、以及任何测试遗留状态对后续用例的渗漏；而实测建库 +
create_all 的开销完全可接受（integration 约 1.5 倍、api 约 1.8 倍耗时），
换来的是与 SQLite 模式**完全相同**的隔离语义 —— 不多一种要理解的机制。
不设变量时行为与从前完全一致（SQLite）。

另外这里刻意 **不触发应用的 lifespan**（httpx 的 ASGITransport 默认不跑
lifespan），避免它把全局引擎换成配置里的文件库、把测试数据写到仓库里。
建表由 engine 夹具显式完成。
"""

from __future__ import annotations

import os
from collections.abc import AsyncIterator, Awaitable, Callable, Iterator
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
from sqlalchemy.pool import NullPool

from app.config.settings import Settings
from app.infrastructure.db.base import Base
from app.infrastructure.db.session import configure_database, create_engine, get_session_factory
from app.main import create_app
from app.models.requirement import Requirement
from app.models.user import User

API = "/api/v1"
DEFAULT_PASSWORD = "StrongPass123!"

# 真 MySQL 模式的开关：模板里必须含 {dbname} 占位。不设 = SQLite（默认）
_TEST_DATABASE_URL_TEMPLATE = os.environ.get("TEST_DATABASE_URL", "").strip()


def _mysql_admin_kwargs() -> dict[str, Any]:
    """从模板 URL 解析出建库 / 删库用的连接参数（不需要库名本身）。"""

    url = make_url(_TEST_DATABASE_URL_TEMPLATE.replace("{dbname}", "admin"))
    assert url.drivername.startswith("mysql"), "TEST_DATABASE_URL 目前只支持 MySQL 模板（含 {dbname} 占位）"
    return {
        "host": url.host or "127.0.0.1",
        "port": url.port or 3306,
        "user": url.username or "root",
        "password": url.password or "",
        "charset": "utf8mb4",
        "autocommit": True,
    }


def _create_test_database(dbname: str) -> None:
    import pymysql

    conn = pymysql.connect(**_mysql_admin_kwargs())
    try:
        with conn.cursor() as cur:
            # dbname 由本模块生成（固定前缀 + hex），无注入面；反引号是防御性写法
            cur.execute(f"CREATE DATABASE `{dbname}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
    finally:
        conn.close()


def _drop_test_database(dbname: str) -> None:
    import pymysql

    try:
        conn = pymysql.connect(**_mysql_admin_kwargs())
    except Exception as exc:  # noqa: BLE001 - 清理失败不该把测试结果染红
        print(f"[conftest] 清理测试库 {dbname} 时连接失败（保留现场）：{exc}")
        return
    try:
        with conn.cursor() as cur:
            cur.execute(f"DROP DATABASE IF EXISTS `{dbname}`")
    except Exception as exc:  # noqa: BLE001
        print(f"[conftest] 清理测试库 {dbname} 失败（保留现场）：{exc}")
    finally:
        conn.close()


@pytest.fixture
def test_settings(tmp_path: Path) -> Iterator[Settings]:
    if _TEST_DATABASE_URL_TEMPLATE:
        # 真库模式：库名唯一，隔离语义与 SQLite 模式完全一致。
        # CREATE 在这里（setup）、DROP 在 yield 之后（teardown）——
        # engine 夹具依赖本夹具，它的 teardown 先跑（连接全部关闭），
        # 之后才能安全 DROP，否则 MySQL 会因为仍有连接而行为不明
        dbname = f"aidev_test_{uuid4().hex[:12]}"
        _create_test_database(dbname)
        database_url = _TEST_DATABASE_URL_TEMPLATE.replace("{dbname}", dbname)
    else:
        dbname = None
        database_url = f"sqlite+aiosqlite:///{(tmp_path / 'test.db').as_posix()}"

    yield Settings(
        app_env="test",
        # 关掉 debug 只为让测试输出干净（debug 会把根 logger 降到 DEBUG，
        # asyncio / aiosqlite 的马达日志会淹没真正的失败信息）
        debug=False,
        database_url=database_url,
        sql_echo=False,
        llm_provider="ark",
        llm_api_key="test-key",
        llm_model="test-model",
        # 工作区也要关进 tmp：不设的话 ./workspace 会写进仓库目录
        workspace_root=str(tmp_path / "workspace"),
        # 业务测试不该被限流影响：额度放大，限流本身由专门的用例验证
        rate_limit_default_per_minute=100_000,
        rate_limit_llm_per_minute=10_000,
        rate_limit_auth_per_minute=10_000,
        # SSE 心跳调小，测试不用等 15 秒
        sse_heartbeat_seconds=0.2,
    )

    if dbname is not None:
        _drop_test_database(dbname)


@pytest.fixture
async def engine(test_settings: Settings) -> AsyncIterator[Any]:
    """走应用自己的 ``create_engine`` 建引擎。

    不直接调 ``create_async_engine``：那样会绕过连接级设置（例如 SQLite 的
    ``PRAGMA foreign_keys=ON``），于是「测试通过」和「生产行为」是两回事 ——
    外键在测试里不生效，级联删除也就永远测不出来。

    ``NullPool`` 是必须的：pytest-asyncio 每个用例跑在新事件循环里，
    池化连接跨循环复用会失效。
    """
    engine = create_engine(test_settings, poolclass=NullPool)
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
async def db_session(app: Any) -> AsyncIterator[AsyncSession]:
    """直连数据库，用来验证「数据真的落库了」而不是只被响应体糊过去。

    依赖 ``app`` 是**必须显式声明**的，不是笔误：``configure_database()`` 在
    ``app`` 夹具里调用，少了它 ``get_session_factory()`` 会直接抛
    「Session factory is not configured」。之前这里只依赖 ``engine``，所有用到
    ``db_session`` 的用例恰好同时也请求了 ``client``（间接依赖 ``app``）才没出事 ——
    靠巧合成立的顺序依赖，迟早会在新用例上炸。
    """
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


@pytest.fixture
async def set_requirement_status(db_session: AsyncSession) -> Callable[..., Awaitable[None]]:
    """直接改库设置需求状态。

    状态迁移本来应该由 Workflow Service 推动（Stage 4），Stage 2 还没有那条路径，
    但「已关闭的需求不能起新工作流」这条规则需要被覆盖，所以直接从库里造状态。
    """

    async def _set(requirement_id: str, status: str) -> None:
        requirement = await db_session.get(Requirement, UUID(requirement_id))
        assert requirement is not None, f"requirement {requirement_id} not found"
        requirement.status = status
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
