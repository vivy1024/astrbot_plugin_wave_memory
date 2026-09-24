"""9876 热参数的持久层：``config_overrides`` 表。

v5 里没有映射到 AstrBot 静态配置的热参数只在当前进程生效，重启就丢。v6 把
这些值存进 WaveMemory 自己的数据库，启动时叠加在静态配置之上（优先级：
内置默认 < AstrBot 静态配置 < 9876 覆盖）。存在自己的表里也避开了 AstrBot
保存表单时把布尔值覆盖成 False 的问题。
"""

from __future__ import annotations

import json
import time
from typing import Any

from .connection import ConnectionManager

_SCHEMA = """
CREATE TABLE IF NOT EXISTS config_overrides (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
)
"""


class ConfigOverrideRepo:
    def __init__(self, cm: ConnectionManager):
        self.cm = cm
        with cm.migration_transaction() as tx:
            tx.execute(_SCHEMA)

    def all(self) -> dict[str, dict[str, Any]]:
        rows = self.cm.execute("SELECT key, value, updated_at, updated_by FROM config_overrides ORDER BY key").fetchall()
        out: dict[str, dict[str, Any]] = {}
        for key, raw, updated_at, updated_by in rows:
            try:
                value = json.loads(raw)
            except (TypeError, ValueError):
                continue
            out[str(key)] = {"value": value, "updated_at": float(updated_at), "updated_by": str(updated_by or "")}
        return out

    def values(self) -> dict[str, Any]:
        return {key: item["value"] for key, item in self.all().items()}

    def set_many(self, values: dict[str, Any], *, updated_by: str = "") -> None:
        now = time.time()
        with self.cm.write_transaction() as tx:
            for key, value in values.items():
                tx.execute(
                    """INSERT INTO config_overrides(key, value, updated_at, updated_by) VALUES (?, ?, ?, ?)
                       ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at,
                           updated_by=excluded.updated_by""",
                    (str(key), json.dumps(value, ensure_ascii=False), now, str(updated_by)[:80]),
                )

    def delete(self, keys: list[str]) -> int:
        if not keys:
            return 0
        with self.cm.write_transaction() as tx:
            placeholders = ",".join("?" for _ in keys)
            cursor = tx.execute(f"DELETE FROM config_overrides WHERE key IN ({placeholders})", tuple(keys))
            return int(cursor.rowcount or 0)


__all__ = ["ConfigOverrideRepo"]
