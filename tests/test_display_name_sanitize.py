"""昵称清洗：规则、写入口统一清洗、存量一次性迁移。"""

from __future__ import annotations

import json
import sqlite3
import sys
import types
from types import SimpleNamespace

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

from domain.display_name import sanitize_alias_list, sanitize_display_name
from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.connection import ConnectionManager
from engine.db.migrations.display_name_repair import BACKUP_TABLE, repair_dirty_display_names

PROTOBUF_CARD = "\n\x11\x12\x0f猫猫副队长\n\t\n\x07$ÿĀ\x11\x10\x10\x00"


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        (PROTOBUF_CARD, "猫猫副队长"),
        ("zz[$ÿĀ\x11\x10]", "zz"),
        ("岸本�的锅", "岸本的锅"),
        ("abc\ud800def", "abcdef"),
        ("José\x00", "José"),
        ("abc~\x00", "abc~"),
        ("Renée\x01x", "Renée"),
        ("abc\x00defg", "defg"),
        ("\x00\x01\x7f\x85", ""),
        ("  a   b ", "a b"),
        ("a\tb\nc", "a b c"),
        ("　山田　太郎　", "山田　太郎"),
        ("　　", ""),
        (None, ""),
        (12345, "12345"),
        (b"\xe7\x8c\xab\xff", "猫"),
    ],
)
def test_sanitize_display_name_rules(raw, expected):
    assert sanitize_display_name(raw) == expected


@pytest.mark.parametrize(
    "name",
    ["猫猫副队长", "さくら🌸", "Tom & Jerry", "★彡Kira彡★", "Ｏ(≧▽≦)Ｏ", "[LM导入]", "[AM:fact]",
     "👨‍👩‍👧 家族", "José", "Zoë", "唱完山歌唱 帅哥ˇ", "- Noel", "김민수", "Ñandú"],
)
def test_sanitize_keeps_normal_names(name):
    assert sanitize_display_name(name) == name


def test_sanitize_alias_list_dedupes_after_cleaning():
    assert sanitize_alias_list(["猫猫副队长", PROTOBUF_CARD, "　", "-  Noel", "- Noel"]) == ["猫猫副队长", "- Noel"]
    assert sanitize_alias_list("not-a-list") == []


def _scope() -> RuntimeScope:
    return RuntimeScope(
        bot_id="bot-alpha",
        visibility="group",
        session=SessionRef("qq:group:group-1", "qq", "group", "group-1"),
    )


def test_add_memory_stores_clean_sender_name(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "wm.db"), dimension=4)
    try:
        memory_id = db.add_memory(
            group_id="group-1", content="一条消息", sender_id="u1", sender_name=PROTOBUF_CARD,
            scope=_scope(), provenance={}, origin_metadata={},
        )
        row = db.conn.execute("SELECT sender_name FROM memories WHERE id=?", (memory_id,)).fetchone()
        assert row[0] == "猫猫副队长"
    finally:
        db.close()


@pytest.mark.asyncio
async def test_write_gateway_stores_clean_sender_name(tmp_path):
    import numpy as np

    from services.system_convergence_runtime import ProductionWriteGateway

    path = str(tmp_path / "gw.db")
    db = WaveMemoryDB(path, dimension=4)
    gateway = ProductionWriteGateway(path)
    try:
        memory_id = await gateway.append_memory(
            scope=_scope(), group_id="group-1", content="一条足够长的群聊消息",
            vector=np.asarray([1.0, 0.0, 0.0, 0.0], dtype=np.float32),
            sender_id="u1", sender_name=PROTOBUF_CARD, timestamp=100.0, importance=1.0,
            source="chat", provenance={"event_id": "e1"}, origin_metadata={"event_id": "e1"},
            quarantine=False, idempotency_hint="e1",
        )
        row = db.conn.execute("SELECT sender_name FROM memories WHERE id=?", (memory_id,)).fetchone()
        assert row[0] == "猫猫副队长"
    finally:
        db.close()


def test_repair_cleans_legacy_rows_once_and_keeps_backup(tmp_path):
    path = str(tmp_path / "legacy.db")
    WaveMemoryDB(path, dimension=4).close()
    # 模拟迁移之前就已入库的脏数据：去掉「已修复」标记表，绕过写入口直接写原始值。
    conn = sqlite3.connect(path)
    conn.execute(f"DROP TABLE {BACKUP_TABLE}")
    conn.executemany(
        "INSERT INTO memories (group_id, sender_id, sender_name, content, timestamp) VALUES (?, ?, ?, ?, ?)",
        [
            ("group-1", "10000002", PROTOBUF_CARD, "喵喵喵", 1.0),
            ("group-1", "10000002", PROTOBUF_CARD, "第二条", 2.0),
            ("group-1", "10000002", "猫猫副队长", "干净的一条", 3.0),
            ("group-1", "10000006", "zz[$ÿĀ\x11\x10]", "hello", 4.0),
            ("group-1", "42", "さくら🌸", "不该被动", 5.0),
        ],
    )
    conn.execute(
        "INSERT INTO user_profiles (user_id, group_id, bot_id, nickname) VALUES ('10000002', 'group-1', 'bot-alpha', ?)",
        ("猫猫副队长",),
    )
    conn.execute(
        "INSERT INTO user_profiles (user_id, group_id, bot_id, nickname) VALUES ('7', 'group-1', 'bot-alpha', ?)",
        ("-  Noel",),
    )
    conn.execute(
        "INSERT INTO person_registry (qq_id, display_name, aliases) VALUES ('10000002', ?, ?)",
        (PROTOBUF_CARD, json.dumps(["猫猫副队长", PROTOBUF_CARD], ensure_ascii=False)),
    )
    conn.execute(
        "INSERT INTO person_registry (qq_id, display_name, aliases) VALUES ('42', 'さくら🌸', ?)",
        (json.dumps(["さくら🌸"], ensure_ascii=False),),
    )
    conn.commit()
    conn.close()

    db = WaveMemoryDB(path, dimension=4)
    try:
        names = [row[0] for row in db.conn.execute("SELECT sender_name FROM memories ORDER BY timestamp")]
        assert names == ["猫猫副队长", "猫猫副队长", "猫猫副队长", "zz", "さくら🌸"]
        nicknames = dict(db.conn.execute("SELECT user_id, nickname FROM user_profiles"))
        # 只修损坏的值：多空格只是样式问题，存量不改写（新消息入库时照常折叠）
        assert nicknames == {"10000002": "猫猫副队长", "7": "-  Noel"}
        registry = {
            row[0]: (row[1], json.loads(row[2]))
            for row in db.conn.execute("SELECT qq_id, display_name, aliases FROM person_registry")
        }
        assert registry == {"10000002": ("猫猫副队长", ["猫猫副队长"]), "42": ("さくら🌸", ["さくら🌸"])}

        backup = db.conn.execute(
            f"SELECT table_name, column_name, old_value, new_value FROM {BACKUP_TABLE} ORDER BY table_name, row_key"
        ).fetchall()
        by_table: dict[tuple[str, str], int] = {}
        for table, column, old, new in backup:
            by_table[(table, column)] = by_table.get((table, column), 0) + 1
            assert old != new
        assert by_table == {
            ("memories", "sender_name"): 3,
            ("person_registry", "display_name"): 1,
            ("person_registry", "aliases"): 1,
        }
        assert ("memories", "sender_name", PROTOBUF_CARD, "猫猫副队长") in backup

        # 全文索引随触发器同步更新，外部内容表保持一致。
        db.conn.execute("INSERT INTO fts_memories(fts_memories) VALUES('integrity-check')")
        hits = db.conn.execute(
            "SELECT rowid FROM fts_memories WHERE fts_memories MATCH ?", ('sender_name:"zz"',)
        ).fetchall()
        assert len(hits) == 1
    finally:
        db.close()

    # 备份表存在即视为已修复：再脏的新数据也不会被这次迁移重复处理。
    cm = ConnectionManager(path)
    try:
        assert repair_dirty_display_names(cm) == {}
    finally:
        cm.close()


def test_repair_tolerates_missing_tables(tmp_path):
    path = str(tmp_path / "bare.db")
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, sender_name TEXT)")
    conn.execute("INSERT INTO memories (sender_name) VALUES (?)", (PROTOBUF_CARD,))
    conn.commit()
    conn.close()
    cm = ConnectionManager(path)
    try:
        assert repair_dirty_display_names(cm) == {"memories.sender_name": 1}
        assert cm.conn.execute("SELECT sender_name FROM memories").fetchone()[0] == "猫猫副队长"
    finally:
        cm.close()
