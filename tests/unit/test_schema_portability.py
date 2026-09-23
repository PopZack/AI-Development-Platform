"""表结构的跨方言校验（Stage 5：部署可能不用 SQLite）。

不需要真的连一个 MySQL / PostgreSQL —— 把 ``CREATE TABLE`` 编译成目标方言
就能抓到绝大多数「在 SQLite 上跑得好好的，换个库就崩」的问题：

- 类型没有对应实现（``CompileError: can't render element of type XXX``）
- 主键 / 外键 / 索引写法依赖某个方言的特性
- 字符串长度缺失（MySQL 的 VARCHAR 必须有长度）
- **建表顺序不满足「被引用的表必须已存在」**（MySQL 会直接报 1824；
  SQLite 容忍前向引用，所以本地看不出问题 —— 见下面的外键环用例）

真机验证（连一个真实 MySQL 跑一遍迁移与全流程）仍然是必要的，
但那是部署时的验收步骤，不该等到那时候才发现类型不兼容。
"""

from __future__ import annotations

import re
import warnings
from pathlib import Path

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
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "migrations" / "versions"


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


# ------------------------------------------------------------------ 外键环与建表顺序
#
# 这一组用例的由来（2026-09-23 在真 MySQL 上踩到）：
#
#   ``approvals.tool_call_id → tool_calls`` 与 ``tool_calls.approval_id → approvals``
#   互相引用，构成外键环。SQLAlchemy 的排序器遇到环会**放弃这两张表的依赖关系**
#   （只发一条 SAWarning），于是 ``approvals`` 被排到 ``users`` 前面。
#   而 ``create_all()`` 会自己把环上的外键延后成 ALTER，所以：
#     - 本地 SQLite 一切正常（SQLite 本来就容忍前向引用）
#     - `create_all` 建库也正常
#     - **autogenerate 生成的迁移是内联外键的** → 换到 MySQL 直接
#       `(1824, "Failed to open the referenced table 'users'")`
#
# 这三个用例把这条路径封住：环不许有、SQLAlchemy 排序不许告警、迁移里的顺序必须合法。


def _foreign_key_graph() -> dict[str, set[str]]:
    """表 → 它引用的其它表（自引用不算环）。"""
    graph: dict[str, set[str]] = {}
    for name, table in Base.metadata.tables.items():
        targets = {fk.column.table.name for fk in table.foreign_keys}
        graph[name] = {t for t in targets if t != name}
    return graph


def test_model_foreign_keys_have_no_cycles() -> None:
    """模型里不能有外键环。

    外键环不是「风格问题」：它让 ``sorted_tables`` 丢掉依赖关系、让迁移建表顺序错乱，
    最终表现为「SQLite 全绿、MySQL 建不出表」。要约束环上的某一侧，就得用
    ``use_alter=True`` —— 但 SQLite 不支持 ALTER 添加约束，那条路会让约束在
    SQLite 上静默失效，比没有约束更危险。所以本项目的规则是：**不留环**。
    """
    remaining = _foreign_key_graph()
    resolved: set[str] = set()
    while remaining:
        ready = [t for t, deps in remaining.items() if deps <= resolved]
        if not ready:
            raise AssertionError(
                f"外键存在环，涉及表：{sorted(remaining)}。"
                "MySQL 无法按此顺序建表，请拆掉环（保留列、去掉其中一侧的 ForeignKey）"
            )
        resolved.update(ready)
        for name in ready:
            remaining.pop(name)


def test_sqlalchemy_can_sort_tables_without_warning() -> None:
    """SQLAlchemy 自己也得能排出顺序 —— 有环时它只发警告然后丢掉依赖关系。"""
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        order = [t.name for t in Base.metadata.sorted_tables]

    cycles = [str(w.message) for w in caught if "unresolvable cycles" in str(w.message)]
    assert not cycles, f"SQLAlchemy 报告无法排序的外键环：{cycles}"
    assert len(order) == len(TABLES)

    # 顺序本身也要正确：每张表引用的表必须排在它前面
    graph = _foreign_key_graph()
    seen: set[str] = set()
    for name in order:
        missing = graph[name] - seen
        assert not missing, f"{name} 引用了排在它后面的表 {sorted(missing)}；顺序：{order}"
        seen.add(name)


_CREATE_TABLE = re.compile(r"op\.create_table\(\s*'([a-z_]+)'")
_FK_TARGET = re.compile(r"ForeignKeyConstraint\(\[[^\]]*\],\s*\['([a-z_]+)\.")


def _migration_tables_in_order() -> list[tuple[str, set[str]]]:
    """从迁移文件里抽出「建表顺序」与每张表当时引用的表。

    直接解析文件文本而不是跑 alembic：用例要快，而 autogenerate 的输出格式很固定
    （``op.create_table('x', ...)`` 后面跟着 ``sa.ForeignKeyConstraint([...], ['y.id'])``）。
    """
    blocks: list[tuple[str, set[str]]] = []
    for path in sorted(MIGRATIONS_DIR.glob("*.py")):
        if path.name == "__init__.py":
            continue
        text = path.read_text(encoding="utf-8")
        for part in text.split("op.create_table(")[1:]:
            head = _CREATE_TABLE.search("op.create_table(" + part)
            if head is None:
                continue
            name = head.group(1)
            targets = {t for t in _FK_TARGET.findall(part) if t != name}
            blocks.append((name, targets))
    return blocks


def test_migration_creates_tables_in_dependency_order() -> None:
    """迁移里的建表顺序必须满足 MySQL 的「被引用的表必须已存在」。

    SQLite 容忍前向引用，所以顺序错了本地照样能跑 —— 这个用例是**唯一**能在
    没有 MySQL 的情况下提前发现它的地方。
    """
    blocks = _migration_tables_in_order()
    assert len(blocks) >= 9, f"没解析到迁移里的建表语句，解析逻辑可能失效了：{blocks}"

    created: set[str] = set()
    problems: list[str] = []
    for name, targets in blocks:
        missing = targets - created
        if missing:
            problems.append(f"{name} 引用了尚未创建的表 {sorted(missing)}")
        created.add(name)

    assert not problems, (
        "MySQL 上会报 (1824, Failed to open the referenced table)：\n"
        + "\n".join(problems)
        + f"\n实际顺序：{' → '.join(n for n, _ in blocks)}"
    )
