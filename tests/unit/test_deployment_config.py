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
