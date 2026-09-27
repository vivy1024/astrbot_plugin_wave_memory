"""旧记忆的空 version 补成 1，只执行一次，不触发全文索引重写。"""

from __future__ import annotations

import sqlite3
import sys
import types
from types import SimpleNamespace

if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None, debug=lambda *a, **k: None, error=lambda *a, **k: None)
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from engine.database import WaveMemoryDB
from engine.db.connection import ConnectionManager
from engine.db.migrations.memory_version_backfill import MARKER_TABLE, backfill_null_memory_versions


def test_null_versions_become_one_once(tmp_path):
    path = str(tmp_path / "v.db")
    WaveMemoryDB(path, dimension=4).close()
    conn = sqlite3.connect(path)
    conn.execute(f"DROP TABLE {MARKER_TABLE}")
    conn.executemany(
        "INSERT INTO memories(id, group_id, content, timestamp, version) VALUES (?, 'g', ?, 1.0, ?)",
        [(1, "旧记忆", None), (2, "新记忆", 3)],
    )
    conn.commit()
    fts_before = conn.execute("SELECT COUNT(*) FROM fts_memories_data").fetchone()[0]
    conn.close()

    cm = ConnectionManager(path)
    try:
        assert backfill_null_memory_versions(cm) == 1
        assert backfill_null_memory_versions(cm) == 0
    finally:
        cm.close()
    conn = sqlite3.connect(path)
    assert dict(conn.execute("SELECT id, version FROM memories")) == {1: 1, 2: 3}
    assert conn.execute("SELECT COUNT(*) FROM fts_memories_data").fetchone()[0] == fts_before
    conn.close()
