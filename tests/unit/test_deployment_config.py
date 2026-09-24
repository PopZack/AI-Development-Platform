"""部署配置的一致性校验：compose 与 pyproject 必须对得上。

不连 Docker、不构建镜像，只读文件 —— 但抓的是真踩过的两类问题：

1. **compose 配了 `REDIS_URL`，镜像里却没装 `redis` 包。**
   `redis` 是可选依赖，缺了它应用不会崩，只在启动日志里报一行错、
   然后**静默退化成进程内状态**：多 worker 下额度被乘以 worker 数、
   SSE 事件只在产生它的那个 worker 上可见 —— 正好是「配 Redis 想避免」的两个问题。
   配了却不生效，比没配更难查。

2. **`mysql` extra 里少了 `cryptography`。**
   MySQL 8 默认认证插件是 `caching_sha2_password`，`aiomysql`/`PyMySQL` 处理它
   需要 `cryptography`。没装的话连接直接抛
   ``RuntimeError: 'cryptography' package is required for sha256_password or caching_sha2_password``
   —— 而 pyproject 原来只写了 `aiomysql`，等于「按文档装起来就连不上库」。

这两条都是 2026-09-23 在真 MySQL + Docker 上验证时暴露的，所以固化成用例。
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
COMPOSE_PATH = ROOT / "docker-compose.yml"


def _compose_value(name: str) -> str | None:
    """从 compose 里取某个键的值。

    刻意不引 YAML 依赖：这里只需要几个标量键，正则足够，而且
    用例失败时给出的信息比「YAML 解析报错」更贴近问题。
    """
    text = COMPOSE_PATH.read_text(encoding="utf-8")
    match = re.search(rf"^\s*{name}:\s*(.+?)\s*$", text, re.M)
    return match.group(1).strip().strip('"').strip("'") if match else None


def _extras() -> list[str]:
    raw = _compose_value("EXTRAS") or ""
    return [item.strip() for item in raw.split(",") if item.strip()]


def _optional_dependencies() -> dict[str, list[str]]:
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    return data["project"]["optional-dependencies"]


def _package_names(requirements: list[str]) -> set[str]:
    """把 ``aiomysql>=0.3.2`` 这类声明还原成包名（去掉版本与环境标记）。"""
    return {re.split(r"[<>=!\[;]", req, maxsplit=1)[0].strip() for req in requirements}


def test_compose_installs_redis_extra_because_it_sets_redis_url() -> None:
    """配了 REDIS_URL 就必须真的装上 redis 包，否则是「配了不生效」。"""
    assert _compose_value("REDIS_URL"), "compose 里没有 REDIS_URL —— 这个用例的前提变了"

    extras = _extras()
    assert "redis" in extras, (
        f"compose 配了 REDIS_URL，但构建参数 EXTRAS={extras} 里没有 redis。"
        "镜像里没有 redis 包时应用只会警告然后退化成进程内限流与事件广播 —— "
        "多 worker 下额度会被乘以 worker 数，SSE 事件只在单个 worker 上可见。"
    )
    assert "redis" in _optional_dependencies(), (
        "pyproject 里没有 redis 可选依赖，compose 的 `--extra redis` 会让构建失败"
    )


def test_compose_uses_mysql_driver_and_migrations() -> None:
    """MySQL 部署的配套前提：驱动 extra、迁移开关。"""
    database_url = _compose_value("DATABASE_URL") or ""
    assert database_url.startswith("mysql+aiomysql://"), database_url
    assert "mysql" in _extras(), f"EXTRAS={_extras()} 里没有 mysql，镜像里会缺驱动"
    assert _compose_value("AUTO_CREATE_TABLES") == "false", (
        "生产必须走 alembic：create_all 只建缺失的表，不会给已有表加列，"
        "发版时会静默失效，然后在运行中报 no such column"
    )


def test_mysql_extra_includes_cryptography() -> None:
    """MySQL 8 默认 caching_sha2_password，没 cryptography 连不上。"""
    mysql_extra = _optional_dependencies().get("mysql")
    assert mysql_extra, "pyproject 里没有 mysql 可选依赖"

    packages = _package_names(mysql_extra)
    assert "aiomysql" in packages, packages
    assert "cryptography" in packages, (
        f"mysql extra 只有 {sorted(packages)}，缺少 cryptography："
        "MySQL 8 默认认证插件 caching_sha2_password 需要它，"
        "否则连接直接抛 RuntimeError"
    )


# ------------------------------------------------------------------ 运行时依赖完整性
#
# 这一组的由来：镜像构建成功、容器起来后立刻崩在
#   ModuleNotFoundError: No module named 'httpx'
# 因为 httpx 只写在 dev 依赖组里，而 `app/infrastructure/llm/ark.py` 顶层就 import 它 ——
# 开发机上 `uv sync` 装了 dev 组所以一切正常，**镜像里（--no-dev）没有 HTTP 客户端**，
# 于是「能调模型」这个核心能力在部署时直接没了。
#
# 这类问题（导入写了、依赖放错地方）不会在本地暴露，只能靠构建+运行镜像发现 ——
# 除非有一条静态检查。下面就是那条检查。

# 导入名 → 发行包名不一致的少数几个（其余按同名处理）
_IMPORT_TO_DISTRIBUTION = {
    "argon2": "argon2-cffi",
    "email_validator": "email-validator",
    "jwt": "pyjwt",
    "pydantic_settings": "pydantic-settings",
    "yaml": "pyyaml",
}

# **刻意可选**的包：代码里延迟导入且有降级分支，装在 extra 里即可，不必进主依赖。
# 往这里加东西前先想清楚：主依赖缺失会让镜像启动即崩，而 extra 缺失只会降级 ——
# 只有当「没有它也能正常跑」时才放这里。
_OPTIONAL_BY_DESIGN = {"redis"}


def _normalize(name: str) -> str:
    """把发行包名归一化：去掉版本号、extra、下划线连字符差异。"""
    base = re.split(r"[<>=!\[;]", name, maxsplit=1)[0].strip().lower()
    return base.replace("_", "-")


def _root_dependencies(group: str) -> set[str]:
    """从 pyproject 读根依赖：``main`` 是主依赖，其它值取全部 extra 的并集。"""
    data = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    if group == "main":
        return {_normalize(dep) for dep in data["project"]["dependencies"]}
    return {
        _normalize(dep) for deps in data["project"].get("optional-dependencies", {}).values() for dep in deps
    }


def _closure(roots: set[str]) -> set[str]:
    """在 ``uv.lock`` 的依赖图上求传递闭包。

    必须算闭包而不是只看直接声明：``starlette`` 是 fastapi 的传递依赖，
    没直接写进 pyproject 也是装得上的 —— 只看直接声明会误报。
    """
    data = tomllib.loads((ROOT / "uv.lock").read_text(encoding="utf-8"))
    graph: dict[str, set[str]] = {}
    for package in data.get("package", []):
        deps = {_normalize(dep["name"]) for dep in package.get("dependencies", [])}
        # 包自身的 optional-dependencies 也算进来（宁可宽松，不要误报）
        for group in (package.get("optional-dependencies") or {}).values():
            deps |= {_normalize(dep["name"]) for dep in group}
        graph.setdefault(_normalize(package["name"]), set()).update(deps)

    seen: set[str] = set()
    stack = list(roots)
    while stack:
        name = stack.pop()
        if name in seen:
            continue
        seen.add(name)
        stack.extend(graph.get(name, ()))
    return seen


def _runtime_distributions() -> set[str]:
    """``uv sync --no-dev`` 之后镜像里**实际会有**的发行包。"""
    return _closure(_root_dependencies("main"))


def _imported_top_level_modules() -> set[str]:
    """静态扫出 ``app/`` 里所有顶层导入的第三方模块名。"""
    import ast
    import sys

    first_party = {"app"}
    stdlib = set(sys.stdlib_module_names)
    found: set[str] = set()

    for path in sorted((ROOT / "app").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                # level > 0 是相对导入（本包内），跳过
                names = [node.module or ""] if node.level == 0 else []
            else:
                continue
            for name in names:
                top = name.split(".")[0]
                if top and top not in stdlib and top not in first_party:
                    found.add(top)
    return found


def test_app_imports_are_declared_as_runtime_dependencies() -> None:
    """``app/`` 里导入的第三方包必须能在主依赖（含传递依赖）里找到。

    为什么需要这条：``app/infrastructure/llm/ark.py`` 顶层 ``import httpx``，
    而 httpx 一度只写在 dev 依赖组里。开发机 `uv sync` 装了 dev 组所以一切正常，
    镜像里（``--no-dev``）**没有 HTTP 客户端** —— 容器起来直接
    `ModuleNotFoundError: No module named 'httpx'`，「能调模型」这个核心能力没了。

    这类问题本地测不出来（测试跑在开发环境），只能靠构建+运行镜像 ——
    除非有这条静态检查。
    """
    runtime = _runtime_distributions()
    optional = _closure(_root_dependencies("extras"))
    missing: dict[str, str] = {}

    for module in sorted(_imported_top_level_modules()):
        if module in _OPTIONAL_BY_DESIGN:
            # 可选的也必须是「装得上」的，否则 extra 写错了同样发现不了
            assert module in optional, (
                f"{module} 被列为「刻意可选」，但主依赖与 extra 的闭包里都没有它："
                "镜像里根本装不上，等于这个降级分支永远不会被启用"
            )
            continue
        distribution = _normalize(_IMPORT_TO_DISTRIBUTION.get(module, module))
        if distribution not in runtime:
            missing[module] = distribution

    assert not missing, (
        "app/ 里导入了、但镜像（uv sync --no-dev）里不会有的包："
        f"{missing}。要么加进 [project] dependencies，要么装进 extra 并在代码里"
        "延迟导入 + 提供降级分支后加入 _OPTIONAL_BY_DESIGN。"
    )


def test_optional_by_design_list_has_no_stale_entries() -> None:
    """豁免名单必须保持精简 —— 只写不删会慢慢变成「什么都放过」。"""
    imported = _imported_top_level_modules()
    stale = {module for module in _OPTIONAL_BY_DESIGN if module not in imported}
    assert not stale, f"_OPTIONAL_BY_DESIGN 里的 {sorted(stale)} 已经用不到了，删掉"


def test_pytest_is_a_runtime_dependency() -> None:
    """pytest 必须在运行时闭包里 —— 它不是开发工具，是 L3 工具的执行引擎。

    L3 ``run_pytest`` 用 ``sys.executable -m pytest`` 执行**模型写的测试代码**。
    pytest 放在 dev 组时，镜像（``--no-dev``）里没有它，测试执行只会得到
    ``No module named pytest``：Tester 如实判 fail、Reviewer 如实打回，
    而**模型改自己的代码永远修不好环境缺包** —— 返工循环走不出去
    （2026-09-23 容器内实测：同一需求连打 5 轮 needs_revision）。

    上一条 import 检查抓不到这个：``app/`` 从不 ``import pytest``，
    它是**子进程调用** —— 所以单独立一条。
    """
    runtime = _runtime_distributions()
    assert "pytest" in runtime, (
        "pytest 不在运行时依赖闭包里：L3 run_pytest 在镜像里会报 "
        "'No module named pytest'，真实测试执行功能失效。"
        "它必须放在 [project] dependencies（不是 dev 组、也不是 extra）。"
    )


# ---------------------------------------------------------------- T9：REQUIRE_REDIS


def test_require_redis_rejects_missing_url(tmp_path: Path) -> None:
    """REQUIRE_REDIS=true 但没配 REDIS_URL → 拒绝启动，并说明两种出路。"""
    import pytest

    from app.config.settings import Settings
    from app.main import create_app

    settings = Settings(
        app_env="test",
        require_redis=True,
        redis_url="",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}",
        workspace_root=str(tmp_path / "ws"),
        llm_provider="mock",
    )
    with pytest.raises(RuntimeError, match="REDIS_URL 未配置"):
        create_app(settings)


def test_require_redis_rejects_missing_package(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """REQUIRE_REDIS=true 且配了 URL 但包没装（客户端创建失败）→ 拒绝启动。

    这条对应真实事故：compose 配了 REDIS_URL 却没装 redis extra，
    应用静默退化成进程内状态 —— REQUIRE_REDIS 就是让这种形态在门口失败。
    """
    import pytest

    import app.main as main_module
    from app.config.settings import Settings
    from app.main import create_app

    def _broken_create_redis(url: str) -> None:
        return None  # 模拟「redis 包没装」时 create_redis 的返回

    monkeypatch.setattr(main_module, "create_redis", _broken_create_redis)
    settings = Settings(
        app_env="test",
        require_redis=True,
        redis_url="redis://localhost:6379/0",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}",
        workspace_root=str(tmp_path / "ws"),
        llm_provider="mock",
    )
    with pytest.raises(RuntimeError, match="redis 包没装"):
        create_app(settings)


async def test_require_redis_ping_failure_rejects_startup(tmp_path: Path) -> None:
    """REQUIRE_REDIS=true 且 Redis 服务不可达 → lifespan 拒绝启动。

    redis-py 的连接是**惰性**的：客户端创建成功不代表服务可达，
    所以必须真发一次 ping。用本机必然没有服务的高位端口模拟不可达。
    """
    import pytest

    from app.config.settings import Settings
    from app.main import create_app

    settings = Settings(
        app_env="test",
        require_redis=True,
        redis_url="redis://127.0.0.1:6390/0",  # 本机无服务，连接立即被拒
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}",
        workspace_root=str(tmp_path / "ws"),
        llm_provider="mock",
    )
    app = create_app(settings)  # 客户端能创建（惰性），拦截发生在 lifespan
    with pytest.raises(RuntimeError, match="Redis 连接失败"):
        async with app.router.lifespan_context(app):
            pass  # pragma: no cover - lifespan 在 ping 处抛出


def test_require_redis_defaults_to_false() -> None:
    """默认关闭：本地单进程开发不该因为没装 redis 包而起不来。"""
    from app.config.settings import Settings

    assert Settings().require_redis is False


async def test_startup_log_contains_the_full_shape(tmp_path: Path) -> None:
    """T10：启动自检一行看完形态 —— env / db 脱敏 / llm 供应商与模型 / events / 限流档位。

    不能用 caplog：``create_app`` 里的 ``setup_logging`` 会**清空根 logger 的
    全部 handler**（包括 pytest 挂的捕获 handler）—— 这是它作为应用入口的正当
    行为。所以在 create_app 之后自己挂一个捕获 handler。
    """
    import logging

    from app.config.settings import Settings
    from app.main import create_app

    settings = Settings(
        app_env="test",
        llm_provider="mock",
        llm_model="test-model",
        database_url=f"sqlite+aiosqlite:///{(tmp_path / 't.db').as_posix()}",
        workspace_root=str(tmp_path / "ws"),
        rate_limit_llm_per_minute=7,  # 非默认值：确认日志里是真实配置而不是写死的文案
        rate_limit_auth_per_minute=9,
        rate_limit_default_per_minute=11,
    )
    app = create_app(settings)

    records: list[logging.LogRecord] = []

    class _Capture(logging.Handler):
        def emit(self, record: logging.LogRecord) -> None:
            records.append(record)

    handler = _Capture()
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
    try:
        async with app.router.lifespan_context(app):
            pass
    finally:
        logging.getLogger().removeHandler(handler)

    started = [r for r in records if "started |" in r.message]
    assert started, "启动自检日志缺失"
    message = started[-1].getMessage()
    assert "env=test" in message
    assert "db=sqlite+aiosqlite://" in message  # SQLite 无凭据，原文可读
    assert "llm=mock/test-model" in message
    assert "events=in-process" in message
    assert "limits=llm 7/min, auth 9/min, default 11/min" in message
