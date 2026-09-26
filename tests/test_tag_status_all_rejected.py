"""模型给了标签但全被准入拒掉时，提取状态记 skipped，不再出现「done 但无标签」。"""

from __future__ import annotations

import sys
import types
from types import SimpleNamespace

import numpy as np
import pytest

if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.migrations.tag_status_repair import BACKUP_TABLE, REASON, repair_done_without_tags
from services.system_convergence_runtime import ProductionWriteGateway


def _scope() -> RuntimeScope:
    return RuntimeScope(
        bot_id="bot-alpha",
        visibility="group",
        session=SessionRef("qq:group:group-1", "qq", "group", "group-1"),
    )


async def _append(gateway: ProductionWriteGateway, hint: str) -> int:
    return await gateway.append_memory(
        scope=_scope(),
        group_id="group-1",
        content=f"一条足够长的群聊消息用于提取标签 {hint}",
        vector=np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
        sender_id="qq:user:user-1",
        sender_name="tester",
        timestamp=100.0,
        importance=1.0,
        source="chat",
        provenance={"event_id": hint},
        origin_metadata={"event_id": hint},
        quarantine=False,
        idempotency_hint=hint,
    )


def _status(db: WaveMemoryDB, memory_id: int) -> str:
    return db.conn.execute(
        "SELECT status FROM tag_extraction_status WHERE memory_id=?", (memory_id,)
    ).fetchone()[0]


@pytest.mark.asyncio
async def test_all_rejected_tags_record_skipped_even_when_caller_says_done(tmp_path):
    path = str(tmp_path / "rejected.sqlite3")
    db = WaveMemoryDB(path, dimension=4)
    gateway = ProductionWriteGateway(path)
    try:
        rejected_id = await _append(gateway, "event-rejected")
        kept_id = await _append(gateway, "event-kept")
        # TagWorker 按准入前有没有标签传 status="done"
        saved = await gateway.apply_tag_extraction(
            scope=_scope(), memory_id=rejected_id, status="done",
            tags=[{"name": "低", "type": "keyword", "confidence": 0.9},
                  {"name": "话题", "type": "topic", "confidence": 0.1}],
        )
        assert saved == 0
        assert _status(db, rejected_id) == "skipped"

        saved = await gateway.apply_tag_extraction(
            scope=_scope(), memory_id=kept_id, status="done",
            tags=[{"name": "缺氧", "type": "topic", "confidence": 0.9}],
        )
        assert saved == 1
        assert _status(db, kept_id) == "done"
    finally:
        await gateway.shutdown()
        db.close()


def test_repair_marks_existing_done_without_tags_once(tmp_path):
    path = str(tmp_path / "repair.sqlite3")
    db = WaveMemoryDB(path, dimension=4)
    try:
        conn = db.conn
        # WaveMemoryDB 初始化时已跑过一次（空库），删掉备份表模拟升级前的库
        conn.execute(f"DROP TABLE {BACKUP_TABLE}")
        rows = [
            (1, "yushu", "s1", "group", "有标签的记忆内容"),
            (2, "yushu", "s1", "group", "标签全被拒掉的记忆"),
            (3, None, None, None, "旧版群聊行，无标签"),
            (4, None, None, None, "旧版群聊行，有旧标签"),
        ]
        for memory_id, bot, session, visibility, content in rows:
            conn.execute(
                "INSERT INTO memories(id, group_id, content, timestamp, bot_id, session_id, visibility) "
                "VALUES (?, 'g', ?, 1.0, ?, ?, ?)",
                (memory_id, content, bot, session, visibility),
            )
            conn.execute(
                "INSERT INTO tag_extraction_status(memory_id, status, updated_at) VALUES (?, 'done', 1.0)",
                (memory_id,),
            )
        conn.execute(
            "INSERT INTO scoped_tags(bot_id, session_id, visibility, name, tag_type, created_at, updated_at) "
            "VALUES ('yushu', 's1', 'group', '缺氧', 'topic', 1.0, 1.0)"
        )
        conn.commit()  # ConnectionManager 读写分连接，先提交再读 id
        tag_id = conn.execute("SELECT id FROM scoped_tags").fetchone()[0]
        conn.execute(
            "INSERT INTO scoped_memory_tags(bot_id, session_id, visibility, memory_id, tag_id, position, relevance, created_at) "
            "VALUES ('yushu', 's1', 'group', 1, ?, 1, 1.0, 1.0)",
            (tag_id,),
        )
        conn.execute("INSERT INTO tags(name, tag_type, created_at) VALUES ('旧标签', 'topic', 1.0)")
        conn.commit()
        legacy_tag = conn.execute("SELECT id FROM tags WHERE name='旧标签'").fetchone()[0]
        conn.execute("INSERT INTO memory_tags(memory_id, tag_id) VALUES (4, ?)", (legacy_tag,))
        conn.commit()

        assert repair_done_without_tags(db._cm) == 2
        statuses = dict(conn.execute("SELECT memory_id, status FROM tag_extraction_status").fetchall())
        assert statuses == {1: "done", 2: "skipped", 3: "skipped", 4: "done"}
        assert conn.execute(
            "SELECT last_error FROM tag_extraction_status WHERE memory_id=2"
        ).fetchone()[0] == REASON
        assert conn.execute(f"SELECT memory_id, old_status FROM {BACKUP_TABLE} ORDER BY 1").fetchall() == [
            (2, "done"), (3, "done"),
        ]

        # 备份表存在即视为已修复
        conn.execute("UPDATE tag_extraction_status SET status='done' WHERE memory_id=2")
        conn.commit()
        assert repair_done_without_tags(db._cm) == 0
    finally:
        db.close()
