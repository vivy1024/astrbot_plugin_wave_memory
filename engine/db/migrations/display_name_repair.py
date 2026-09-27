"""v5.1 一次性数据修复：清洗存量发送者昵称 / 显示名。

QQ 群名片偶尔带着 protobuf 残片入库（``'\\n\\x11\\x12\\x0f猫猫副队长\\n\\t\\n\\x07$ÿĀ...'``），
WebUI「人物与关系」页显示成乱码。写入口已统一走 ``domain.display_name.sanitize_display_name``，
这里把存量按同一规则清洗一次：

- 只修**真正损坏**的值：含硬控制字符、U+FFFD 或孤立代理项（``is_corrupt_display_name``）。
  纯全角空格、多空格这类只是样式问题的昵称不改写（新消息入库时照常折叠）；
- ``memories.sender_name``、``user_profiles.nickname``、``person_registry.display_name``：
  只改损坏且清洗后确实变化的行；
- ``person_registry.aliases``（JSON 数组）：含损坏别名时逐个清洗、去空、去重。

改动前的旧值按 (表, 行键, 列) 写进 ``display_name_repair_v51``，需要时可原样还原，例如：
``UPDATE memories SET sender_name = (SELECT old_value FROM display_name_repair_v51 b
WHERE b.table_name='memories' AND b.column_name='sender_name' AND b.row_key = CAST(memories.id AS TEXT))
WHERE CAST(id AS TEXT) IN (SELECT row_key FROM display_name_repair_v51 WHERE table_name='memories')``。
备份表存在即视为已修复，不会重复执行。

注意 ``memories`` 上的 ``fts_memories_au`` 触发器会随 sender_name 更新重写对应行的全文索引；
按不同取值分组 UPDATE，受影响行数只有十几条，开销可以忽略。
"""

from __future__ import annotations

import json
import re
from typing import Any

try:
    from ....domain.display_name import sanitize_alias_list, sanitize_display_name
except ImportError:  # pragma: no cover - repository tests import engine as top-level
    from domain.display_name import sanitize_alias_list, sanitize_display_name

BACKUP_TABLE = "display_name_repair_v51"

# 硬控制字符（C0 除 \t\n\r、DEL、C1）、替换字符、孤立代理项
_CORRUPT_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f-\x9f�\ud800-\udfff]")


def is_corrupt_display_name(value: Any) -> bool:
    """含硬控制字符、替换字符或孤立代理项：说明是入库时带进来的残片，需要修。"""
    return isinstance(value, str) and bool(_CORRUPT_RE.search(value))


def _tables(tx: Any) -> set[str]:
    return {row[0] for row in tx.execute("SELECT name FROM sqlite_master WHERE type='table'")}


def _columns(tx: Any, table: str) -> set[str]:
    return {str(row[1]) for row in tx.execute(f"PRAGMA table_info({table})")}


def _repair_column(tx: Any, table: str, key: str, column: str) -> int:
    """按不同取值清洗一列，旧值逐行备份；返回改动行数。"""
    changed = 0
    values = tx.execute(
        f"SELECT DISTINCT {column} FROM {table} WHERE typeof({column})='text' AND {column} != ''"
    ).fetchall()
    for (old,) in values:
        if not is_corrupt_display_name(old):
            continue
        new = sanitize_display_name(old)
        if new == old:
            continue
        tx.execute(
            f"""INSERT OR IGNORE INTO {BACKUP_TABLE}(table_name, row_key, column_name, old_value, new_value, repaired_at)
                SELECT ?, CAST({key} AS TEXT), ?, {column}, ?, strftime('%s','now')
                  FROM {table} WHERE {column} = ?""",
            (table, column, new, old),
        )
        cursor = tx.execute(f"UPDATE {table} SET {column} = ? WHERE {column} = ?", (new, old))
        changed += int(cursor.rowcount or 0)
    return changed


def _repair_aliases(tx: Any) -> int:
    changed = 0
    rows = tx.execute(
        "SELECT qq_id, aliases FROM person_registry WHERE aliases IS NOT NULL AND aliases != ''"
    ).fetchall()
    for qq_id, raw in rows:
        try:
            aliases = json.loads(raw)
        except (TypeError, ValueError):
            continue
        if not isinstance(aliases, list) or not any(is_corrupt_display_name(item) for item in aliases):
            continue
        cleaned = sanitize_alias_list(aliases)
        if cleaned == aliases:
            continue
        new_raw = json.dumps(cleaned, ensure_ascii=False)
        tx.execute(
            f"""INSERT OR IGNORE INTO {BACKUP_TABLE}(table_name, row_key, column_name, old_value, new_value, repaired_at)
                VALUES ('person_registry', ?, 'aliases', ?, ?, strftime('%s','now'))""",
            (str(qq_id), raw, new_raw),
        )
        tx.execute("UPDATE person_registry SET aliases = ? WHERE qq_id = ?", (new_raw, qq_id))
        changed += 1
    return changed


def repair_dirty_display_names(cm: Any) -> dict[str, int]:
    """返回 ``{"表.列": 改动行数}``（只含非零项）；已修复过或缺表时返回空字典。"""
    with cm.migration_transaction() as tx:
        tables = _tables(tx)
        if BACKUP_TABLE in tables:
            return {}
        tx.execute(
            f"""CREATE TABLE {BACKUP_TABLE} (
                    table_name TEXT NOT NULL,
                    row_key TEXT NOT NULL,
                    column_name TEXT NOT NULL,
                    old_value TEXT,
                    new_value TEXT,
                    repaired_at REAL,
                    PRIMARY KEY (table_name, row_key, column_name)
                )"""
        )
        counts: dict[str, int] = {}
        targets = (
            ("memories", "id", "sender_name"),
            ("user_profiles", "id", "nickname"),
            ("person_registry", "qq_id", "display_name"),
        )
        for table, key, column in targets:
            if table in tables and {key, column} <= _columns(tx, table):
                counts[f"{table}.{column}"] = _repair_column(tx, table, key, column)
        if "person_registry" in tables and {"qq_id", "aliases"} <= _columns(tx, "person_registry"):
            counts["person_registry.aliases"] = _repair_aliases(tx)
        return {name: count for name, count in counts.items() if count}


__all__ = ["BACKUP_TABLE", "is_corrupt_display_name", "repair_dirty_display_names"]
