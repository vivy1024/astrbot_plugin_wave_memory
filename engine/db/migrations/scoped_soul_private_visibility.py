"""让 Scoped Soul 表与事实表同时接受群聊与私聊行。

羽书在私聊里同样会形成印象、关系、心情、关切，也会得知关于对方的事实。旧表以
``CHECK (visibility = 'group')`` 拒绝私聊行；SQLite 不能原地修改 CHECK，
因此在调用方持有的迁移事务内按"新建 → 复制 → 删除 → 改名 → 重建索引"重建，
并保留 AUTOINCREMENT 序列，不改写任何数据。已迁移的表直接跳过（幂等）。
"""

from __future__ import annotations

import re
import sqlite3
from typing import Any

_GROUP_ONLY_CHECK = re.compile(r"CHECK\s*\(\s*visibility\s*=\s*'group'\s*\)", re.IGNORECASE)
_GROUP_OR_PRIVATE_CHECK = "CHECK (visibility IN ('group', 'private'))"
_TABLE_PREFIX = "scoped_soul_"
# 事实及其审核/历史：私聊里得知的事实只在该私聊可见，按原会话保存。
FACT_TABLES = ("scoped_facts", "scoped_fact_history", "scoped_fact_reviews")


def _group_only_tables(connection: Any, names: tuple[str, ...] | None = None) -> list[tuple[str, str]]:
    if names is None:
        rows = connection.execute(
            "SELECT name, sql FROM sqlite_master WHERE type='table' AND name LIKE ? ESCAPE '/'",
            (_TABLE_PREFIX.replace("_", "/_") + "%",),
        ).fetchall()
    else:
        marks = ",".join("?" for _ in names)
        rows = connection.execute(
            f"SELECT name, sql FROM sqlite_master WHERE type='table' AND name IN ({marks})",
            tuple(names),
        ).fetchall()
    return [
        (str(name), str(sql))
        for name, sql in rows
        if sql and _GROUP_ONLY_CHECK.search(str(sql)) and "__" not in str(name)
    ]


def _rebuild(connection: Any, table: str, create_sql: str) -> None:
    temporary = f"{table}__group_or_private"
    head = re.match(r'\s*CREATE\s+TABLE\s+(?:IF\s+NOT\s+EXISTS\s+)?("?)' + re.escape(table) + r'\1', create_sql, re.IGNORECASE)
    if head is None:
        raise RuntimeError(f"unexpected CREATE TABLE statement for {table}")
    new_sql = f'CREATE TABLE "{temporary}"' + create_sql[head.end():]
    new_sql = _GROUP_ONLY_CHECK.sub(_GROUP_OR_PRIVATE_CHECK, new_sql)
    index_sql = [
        str(row[0])
        for row in connection.execute(
            "SELECT sql FROM sqlite_master WHERE type='index' AND tbl_name=? AND sql IS NOT NULL",
            (table,),
        ).fetchall()
    ]
    sequence = None
    if connection.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='sqlite_sequence'").fetchone():
        row = connection.execute("SELECT seq FROM sqlite_sequence WHERE name=?", (table,)).fetchone()
        sequence = row[0] if row else None
    columns = ", ".join(
        f'"{row[1]}"' for row in connection.execute(f'PRAGMA table_info("{table}")').fetchall()
    )
    before = connection.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
    connection.execute(new_sql)
    connection.execute(f'INSERT INTO "{temporary}" ({columns}) SELECT {columns} FROM "{table}"')
    after = connection.execute(f'SELECT COUNT(*) FROM "{temporary}"').fetchone()[0]
    if before != after:
        raise RuntimeError(f"row count mismatch while rebuilding {table}: {before} != {after}")
    connection.execute(f'DROP TABLE "{table}"')
    connection.execute(f'ALTER TABLE "{temporary}" RENAME TO "{table}"')
    for statement in index_sql:
        connection.execute(statement)
    if sequence is not None:
        connection.execute("UPDATE sqlite_sequence SET seq=MAX(seq, ?) WHERE name=?", (sequence, table))
        if connection.execute("SELECT changes()").fetchone()[0] == 0:
            connection.execute("INSERT INTO sqlite_sequence(name, seq) VALUES (?, ?)", (table, sequence))


def apply_scoped_soul_private_visibility(connection: Any, tables: tuple[str, ...] | None = None) -> list[str]:
    """在已持有的迁移事务内执行；返回本次重建的表名。``tables`` 为空时处理全部 soul 表。"""
    rebuilt: list[str] = []
    for table, create_sql in _group_only_tables(connection, tables):
        _rebuild(connection, table, create_sql)
        rebuilt.append(table)
    return rebuilt


def apply_scoped_fact_private_visibility(connection: Any) -> list[str]:
    return apply_scoped_soul_private_visibility(connection, FACT_TABLES)


def ensure_scoped_soul_private_visibility_connection(connection: sqlite3.Connection) -> list[str]:
    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    return apply_scoped_soul_private_visibility(connection)


__all__ = [
    "FACT_TABLES",
    "apply_scoped_fact_private_visibility",
    "apply_scoped_soul_private_visibility",
    "ensure_scoped_soul_private_visibility_connection",
]
