"""Shared-memory grant read expansion: default off, no foreign touch, no fanout copy."""

from __future__ import annotations

import asyncio
import sys
import types
import unittest
from pathlib import Path

import numpy as np

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.shared_memory_grants import ensure_shared_memory_grants_schema
from engine.db.memory_repo import MemoryRepo
from engine.db.shared_memory_grant_repo import SharedMemoryGrantRepository
from engine.recall_policy import RecallPolicy


def _scope(group: str, *, bot: str = "yushu") -> RuntimeScope:
    return RuntimeScope(
        bot_id=bot,
        visibility="group",
        session=SessionRef(
            id=f"qq:group:{group}",
            platform_id="qq",
            kind="group",
            conversation_id=group,
        ),
    )


def _seed_two_group_memories(cm: ConnectionManager) -> None:
    ensure_shared_memory_grants_schema(cm)
    with cm.write_transaction() as tx:
        tx.execute(
            """
            CREATE TABLE IF NOT EXISTS memories(
                id INTEGER PRIMARY KEY,
                content TEXT,
                group_id TEXT,
                bot_id TEXT,
                session_id TEXT,
                visibility TEXT,
                resolution_state TEXT,
                memory_type TEXT,
                quarantine INTEGER DEFAULT 0,
                sender_id TEXT,
                sender_name TEXT,
                timestamp REAL,
                importance REAL,
                access_count INTEGER,
                source TEXT,
                origin_fingerprint TEXT,
                provenance TEXT
            )
            """
        )
        for mid, gid in ((1, "g1"), (2, "g2")):
            tx.execute(
                """
                INSERT OR REPLACE INTO memories(
                    id, content, group_id, bot_id, session_id, visibility,
                    resolution_state, memory_type, quarantine, sender_id, sender_name,
                    timestamp, importance, access_count, source, origin_fingerprint, provenance
                ) VALUES (?,?,?,?,?,?, 'resolved', 'message', 0, 'u', 'U', 1.0, 1.0, 0, 'chat', '', '{}')
                """,
                (
                    mid,
                    f"content-{gid}",
                    gid,
                    "yushu",
                    f"qq:group:{gid}",
                    "group",
                ),
            )


class SharedGrantRecallTest(unittest.TestCase):
    def test_grant_allowlist_reads_foreign_id_without_full_cross_group(self):
        import tempfile

        path = Path(tempfile.mkdtemp()) / "g.db"
        cm = ConnectionManager(str(path))
        _seed_two_group_memories(cm)
        repo = MemoryRepo(cm)
        grants = SharedMemoryGrantRepository(cm)
        owner = {
            "bot_id": "yushu",
            "session_id": "qq:group:g2",
            "visibility": "group",
            "group_id": "g2",
        }
        consumer = {
            "bot_id": "yushu",
            "session_id": "qq:group:g1",
            "visibility": "group",
            "group_id": "g1",
        }
        grants.grant_read(owner_scope=owner, consumer_scope=consumer, memory_id=2, reason="t")

        scope = _scope("g1")
        # Without grant allow-list: only local
        only_local = repo.get_memories_by_ids([1, 2], scope=scope)
        self.assertEqual([m["id"] for m in only_local], [1])

        # With grant allow-list: local + granted foreign
        with_grant = repo.get_memories_by_ids(
            [1, 2],
            scope=scope,
            shared_grant_memory_ids=[2],
        )
        ids = sorted(m["id"] for m in with_grant)
        self.assertEqual(ids, [1, 2])
        foreign = next(m for m in with_grant if m["id"] == 2)
        self.assertTrue(foreign.get("_shared_grant"))
        self.assertEqual(foreign["group_id"], "g2")

        # Ungranted foreign still blocked even if passed as candidate
        blocked = repo.get_memories_by_ids([1, 2], scope=scope, shared_grant_memory_ids=[99])
        self.assertEqual([m["id"] for m in blocked], [1])

        # No extra memories rows
        n = cm.execute_read("SELECT COUNT(*) FROM memories").fetchone()[0]
        self.assertEqual(int(n), 2)
        cm.close()

    def test_touchable_ids_exclude_shared_grant_and_cross_group(self):
        scope = _scope("g1")
        policy = RecallPolicy(
            scope=scope,
            shared_grants_enabled=True,
            granted_memory_ids=(2,),
        )
        memories = [
            {"id": 1, "group_id": "g1", "bot_id": "yushu", "session_id": "qq:group:g1", "visibility": "group"},
            {
                "id": 2,
                "group_id": "g2",
                "bot_id": "yushu",
                "session_id": "qq:group:g2",
                "visibility": "group",
                "_shared_grant": True,
            },
            {"id": 3, "group_id": "g3", "bot_id": "yushu", "session_id": "qq:group:g3", "visibility": "group"},
        ]
        self.assertEqual(policy.touchable_ids(memories), [1])
        self.assertTrue(policy.is_shared_grant(memories[1]))
        self.assertTrue(policy.is_cross_group(memories[2]))

    def test_query_engine_default_off_and_opt_in_grant(self):
        if "astrbot.api" not in sys.modules:
            astrbot = types.ModuleType("astrbot")
            api = types.ModuleType("astrbot.api")
            api.logger = types.SimpleNamespace(
                debug=lambda *a, **k: None,
                warning=lambda *a, **k: None,
            )
            astrbot.api = api
            sys.modules["astrbot"] = astrbot
            sys.modules["astrbot.api"] = api

        from engine.query_engine import QueryEngine

        class Emb:
            async def get_embedding(self, text):
                return np.array([1.0, 0.0], dtype=np.float32)

        class Index:
            def search(self, vector, k):
                return [(1, 0.1), (2, 0.2)]

        class GrantRepo:
            def active_memory_ids_for_consumer(self, *, consumer_scope):
                return [2]

        class Db:
            def __init__(self):
                self.shared_memory_grants = GrantRepo()
                self.touched = []
                self.calls = []

            def get_memories_by_ids(self, ids, *, scope, allow_cross_group_recall=False, shared_grant_memory_ids=None):
                self.calls.append(
                    {
                        "ids": list(ids),
                        "cross": allow_cross_group_recall,
                        "grants": list(shared_grant_memory_ids or []),
                    }
                )
                out = [
                    {
                        "id": 1,
                        "group_id": "g1",
                        "content": "local",
                        "bot_id": "yushu",
                        "session_id": "qq:group:g1",
                        "visibility": "group",
                        "timestamp": 1,
                        "importance": 1.0,
                    }
                ]
                if shared_grant_memory_ids and 2 in {int(x) for x in shared_grant_memory_ids}:
                    out.append(
                        {
                            "id": 2,
                            "group_id": "g2",
                            "content": "granted",
                            "bot_id": "yushu",
                            "session_id": "qq:group:g2",
                            "visibility": "group",
                            "timestamp": 2,
                            "importance": 1.0,
                            "_shared_grant": True,
                        }
                    )
                return out

            def touch_memories(self, ids, importance_boost=0.01):
                assert importance_boost == 0.0, "召回不应再抬重要度"
                self.touched.append(list(ids))

            def get_memory_vectors(self, ids):
                return {}

        # default off
        db = Db()
        engine = QueryEngine(db, Index(), Emb(), {"min_similarity": 0.0})
        mems = asyncio.run(engine.query("q", scope=_scope("g1")))
        self.assertEqual([m["id"] for m in mems], [1])
        self.assertEqual(db.calls[-1]["grants"], [])
        self.assertEqual(db.touched, [[1]])

        # opt-in grants
        db2 = Db()
        engine2 = QueryEngine(
            db2,
            Index(),
            Emb(),
            {"min_similarity": 0.0, "shared_memory_grants_enabled": True},
        )
        mems2 = asyncio.run(engine2.query("q", scope=_scope("g1")))
        self.assertEqual(sorted(m["id"] for m in mems2), [1, 2])
        self.assertEqual(db2.calls[-1]["grants"], [2])
        self.assertFalse(db2.calls[-1]["cross"])
        # only local touchable
        self.assertEqual(db2.touched, [[1]])
        granted = next(m for m in mems2 if m["id"] == 2)
        self.assertTrue(granted.get("_shared_grant") or granted.get("_is_cross_group"))


if __name__ == "__main__":
    unittest.main()
