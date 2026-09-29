"""game_event 经真实 MessageWriter 与写入网关落库：同一事件重放只入库一次。"""

from __future__ import annotations

import asyncio
import json
import logging
import sys
import types
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

if "astrbot.api" not in sys.modules:
    astrbot_module = types.ModuleType("astrbot")
    api_module = types.ModuleType("astrbot.api")
    api_module.logger = logging.getLogger("astrbot-test")
    sys.modules.setdefault("astrbot", astrbot_module)
    sys.modules["astrbot.api"] = api_module


class _Embedding:
    async def get_embeddings(self, texts):
        return [np.ones(4, dtype=np.float32) for _ in texts]


class _Index:
    def add(self, ids, vectors):
        pass

    def save(self):
        pass


def test_game_event_replay_is_stored_once_as_bot_experience(tmp_path):
    from astrbot_plugin_wave_memory.domain.scope import RuntimeScope, SessionRef
    from astrbot_plugin_wave_memory.engine.database import WaveMemoryDB
    from astrbot_plugin_wave_memory.services.game_events import game_event_payload
    from astrbot_plugin_wave_memory.services.message_writer import MemoryDedupPolicy, MessageWriter
    from astrbot_plugin_wave_memory.services.system_convergence_runtime import ProductionWriteGateway

    path = str(tmp_path / "game-event.sqlite3")
    db = WaveMemoryDB(path, dimension=4)
    gateway = ProductionWriteGateway(path)

    async def run():
        # 关掉内容级去重，只靠 event_id 幂等键，确认重放不会写出第二行。
        writer = MessageWriter(
            db=db,
            memory_index=_Index(),
            embedding_service=_Embedding(),
            noise_max_length=1,
            write_gateway=gateway,
            dedup_policy=MemoryDedupPolicy(enabled=False),
        )
        scope = RuntimeScope(
            "yushu", "group", SessionRef("bilibili:group:30000001", "bilibili", "group", "30000001"),
            subject_principal_id="bilibili:user:10000001",
        )
        observation = {
            "event_id": "cortico:42", "kind": "game_event", "game": "minecraft", "event_type": "death",
            "content": "[我的世界] 今天第 1 次死亡：被打死", "timestamp": 1790000000.0,
        }
        item = game_event_payload(observation, scope, bot_name="羽书")
        await writer._process_batch([dict(item)])
        await writer._process_batch([dict(item)])
        await gateway.shutdown()

    try:
        asyncio.run(run())
        rows = db.conn.execute(
            "SELECT source, sender_id, sender_name, content, session_id, visibility, timestamp, provenance FROM memories"
        ).fetchall()
    finally:
        db.close()
    assert len(rows) == 1
    source, sender_id, sender_name, content, session_id, visibility, timestamp, provenance = rows[0]
    assert (source, sender_id, sender_name) == ("game_event", "bot", "羽书")
    assert content.startswith("[我的世界] 今天第 1 次死亡")
    assert (session_id, visibility, timestamp) == ("bilibili:group:30000001", "group", 1790000000.0)
    meta = json.loads(provenance)["metadata"]
    assert meta["event_id"] == "game_event:cortico:42"
    assert meta["origin_kind"] == "runtime:game_event:minecraft:death"
