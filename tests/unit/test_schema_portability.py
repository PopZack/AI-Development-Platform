"""表结构的跨方言校验（Stage 5：部署可能不用 SQLite）。

不需要真的连一个 MySQL / PostgreSQL —— 把 ``CREATE TABLE`` 编译成目标方言
就能抓到绝大多数「在 SQLite 上跑得好好的，换个库就崩」的问题：

- 类型没有对应实现（``CompileError: can't render element of type XXX``）
- 主键 / 外键 / 索引写法依赖某个方言的特性
- 字符串长度缺失（MySQL 的 VARCHAR 必须有长度）

真机验证（连一个真实 MySQL 跑一遍迁移与全流程）仍然是必要的，
但那是部署时的验收步骤，不该等到那时候才发现类型不兼容。
"""

from __future__ import annotations

import pytest
from sqlalchemy.dialects import mysql, postgresql, sqlite
from sqlalchemy.schema import CreateIndex, CreateTable

import app.models  # noqa: F401  —— 导入以填充 Base.metadata
from app.infrastructure.db.base import Base

TABLES = sorted(Base.metadata.tables)
DIALECTS = {
    "sqlite": sqlite.dialect(),
    "mysql": mysql.dialect(),
    "postgresql": postgresql.dialect(),
}


def test_metadata_is_populated() -> None:
    """护栏：模型没被导入时下面的用例会「全部通过」——那是最坏的情况。"""
    assert len(TABLES) >= 9, f"表数量异常，模型可能没被导入：{TABLES}"


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
def test_all_tables_compile_for_every_dialect(dialect_name: str) -> None:
    """每张表的 CREATE TABLE 都要能编译出来。"""
    dialect = DIALECTS[dialect_name]

    for name in TABLES:
        try:
            ddl = str(CreateTable(Base.metadata.tables[name]).compile(dialect=dialect))
        except Exception as exc:  # noqa: BLE001 - 编译器抛的类型随方言而异
            pytest.fail(f"{dialect_name} 上 {name} 无法编译: {type(exc).__name__}: {exc}")
        assert f"CREATE TABLE {name}" in ddl


@pytest.mark.parametrize("dialect_name", sorted(DIALECTS))
def test_indexes_compile_for_every_dialect(dialect_name: str) -> None:
    """索引也要能编译 —— 组合索引与唯一约束最容易踩到长度/类型限制。"""
    dialect = DIALECTS[dialect_name]

    for name in TABLES:
        for index in Base.metadata.tables[name].indexes:
            try:
                str(CreateIndex(index).compile(dialect=dialect))
            except Exception as exc:  # noqa: BLE001
                pytest.fail(f"{dialect_name} 上索引 {index.name} 无法编译: {exc}")


def test_uuid_columns_have_dialect_appropriate_types() -> None:
    """主键统一 UUID，但每个方言的落地方式不同 —— 这是换库时最容易出问题的地方。

    - PostgreSQL：原生 ``UUID``
    - MySQL：``CHAR(32)``（SQLAlchemy 的 ``Uuid`` 默认不启用 MySQL 原生 UUID）
    - SQLite：``CHAR(32)``
    """
    users = Base.metadata.tables["users"]
    column_type = users.c.id.type

    assert "UUID" in str(column_type.compile(dialect=DIALECTS["postgresql"])).upper()
    assert str(column_type.compile(dialect=DIALECTS["mysql"])).upper().startswith("CHAR(32)")


def test_json_columns_use_native_json_on_mysql() -> None:
    """JSON 列在 MySQL 上是原生 ``JSON``，不是 TEXT —— 否则索引与函数都没法用。"""
    requirements = Base.metadata.tables["requirements"]
    compiled = str(requirements.c.prd_json.type.compile(dialect=DIALECTS["mysql"])).upper()
    assert "JSON" in compiled


def test_no_sqlite_specific_ddl_leaks_into_other_dialects() -> None:
    """SQLite 特有的写法不能漏到别的方言里（例如 AUTOINCREMENT）。"""
    for dialect_name in ("mysql", "postgresql"):
        for name in TABLES:
            ddl = str(CreateTable(Base.metadata.tables[name]).compile(dialect=DIALECTS[dialect_name]))
            assert "AUTOINCREMENT" not in ddl.upper(), f"{dialect_name}.{name} 里出现了 AUTOINCREMENT"
