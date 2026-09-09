import asyncio
import json
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from pathlib import Path

if "quart" not in sys.modules:
    quart_mod = types.ModuleType("quart")
    class _Blueprint:
        def __init__(self, *args, **kwargs): pass
        def route(self, *args, **kwargs):
            def deco(fn): return fn
            return deco
    class _Quart:
        def __init__(self, *args, **kwargs): pass
        def route(self, *args, **kwargs):
            def deco(fn): return fn
            return deco
        def register_blueprint(self, *args, **kwargs): pass
    def _jsonify(obj=None, *args, **kwargs): return obj if obj is not None else {}
    class _Response:
        def __init__(self, *args, **kwargs): pass
    async def _send_from_directory(*args, **kwargs): return None
    quart_mod.Blueprint = _Blueprint
    quart_mod.Quart = _Quart
    quart_mod.Response = _Response
    quart_mod.jsonify = _jsonify
    quart_mod.request = types.SimpleNamespace(args={}, headers={}, get_json=lambda *args, **kwargs: {})
    quart_mod.send_from_directory = _send_from_directory
    sys.modules["quart"] = quart_mod

if "astrbot.api" not in sys.modules:
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")
    class _Logger:
        def debug(self, *args, **kwargs): pass
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass
    api_mod.logger = _Logger()
    sys.modules.setdefault("astrbot", astrbot_mod)
    sys.modules["astrbot.api"] = api_mod

if "astrbot.core.agent.tool" not in sys.modules:
    core_mod = types.ModuleType("astrbot.core")
    agent_mod = types.ModuleType("astrbot.core.agent")
    tool_mod = types.ModuleType("astrbot.core.agent.tool")
    run_ctx_mod = types.ModuleType("astrbot.core.agent.run_context")
    astr_ctx_mod = types.ModuleType("astrbot.core.astr_agent_context")

    class _FunctionTool:
        @classmethod
        def __class_getitem__(cls, item):
            return cls

    class _ContextWrapper:
        @classmethod
        def __class_getitem__(cls, item):
            return cls

        def __init__(self, context=None):
            self.context = context

    class _AstrAgentContext:
        pass

    tool_mod.FunctionTool = _FunctionTool
    run_ctx_mod.ContextWrapper = _ContextWrapper
    astr_ctx_mod.AstrAgentContext = _AstrAgentContext
    sys.modules.setdefault("astrbot.core", core_mod)
    sys.modules.setdefault("astrbot.core.agent", agent_mod)
    sys.modules["astrbot.core.agent.tool"] = tool_mod
    sys.modules["astrbot.core.agent.run_context"] = run_ctx_mod
    sys.modules["astrbot.core.agent.astr_agent_context"] = astr_ctx_mod
    sys.modules["astrbot.core.astr_agent_context"] = astr_ctx_mod


class _DirectCoordinator:
    """在调用方连接上直接执行写事务，模拟 WriteCoordinator 的 transaction_blocking 契约。

    写入口要求 coordinator，因此写入测试必须显式提供事务执行器；
    失败时回滚，与真实协调器的单事务语义一致。
    """

    def __init__(self, conn):
        self.conn = conn
        self.calls = 0

    def transaction_blocking(self, function):
        self.calls += 1
        try:
            result = function(self.conn)
            self.conn.commit()
            return result
        except Exception:
            self.conn.rollback()
            raise


class ReworkCoreTest(unittest.TestCase):
    def _connect(self):
        tmp = tempfile.TemporaryDirectory()
        path = Path(tmp.name) / "wave_memory.db"
        conn = sqlite3.connect(path)
        conn.row_factory = sqlite3.Row
        self.addCleanup(tmp.cleanup)
        return conn, path

    def test_migration_adds_episode_relationship_and_jargon_lifecycle_schema(self):
        from engine.db.migrations.v2_2_experience_rework import run_migration

        conn, path = self._connect()
        conn.execute("CREATE TABLE jargon (id INTEGER PRIMARY KEY, word TEXT, meaning TEXT, frequency INTEGER DEFAULT 0, confidence REAL DEFAULT 0, is_jargon INTEGER, contexts TEXT)")
        conn.execute("CREATE TABLE beliefs (id INTEGER PRIMARY KEY, content TEXT, sources TEXT, status TEXT)")
        conn.commit()
        conn.close()

        self.assertTrue(run_migration(str(path)))
        self.assertTrue(run_migration(str(path)))

        conn = sqlite3.connect(path)
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("experience_episodes", tables)
        self.assertIn("relationship_events", tables)
        jargon_cols = {r[1] for r in conn.execute("PRAGMA table_info(jargon)")}
        self.assertTrue({"status", "scope", "source", "last_infer_freq", "reject_reason"}.issubset(jargon_cols))
        conn.close()

    def test_experience_service_records_and_reads_source_ids(self):
        from services.experience_episodes import ExperienceEpisodeService

        conn, _ = self._connect()
        self.addCleanup(conn.close)
        conn.execute("""
            CREATE TABLE experience_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT,
                episode_type TEXT NOT NULL,
                trigger_text TEXT,
                bot_inner_thought TEXT,
                bot_action TEXT,
                bot_reply TEXT,
                user_reaction TEXT,
                outcome TEXT,
                source_memory_ids TEXT DEFAULT '[]',
                emotional_weight REAL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()

        svc = ExperienceEpisodeService(conn, _DirectCoordinator(conn))
        episode_id = svc.record_episode(
            bot_id="yushu",
            group_id="g1",
            user_id="u1",
            episode_type="bot_reply",
            trigger_text="投喂小蛋糕",
            bot_reply="谢谢",
            source_memory_ids=[11, 12],
            emotional_weight=0.6,
            created_at=1234.0,
        )
        recent = svc.recent_episodes(bot_id="yushu", user_id="u1", group_id="g1", limit=1)
        self.assertEqual(episode_id, recent[0]["id"])
        self.assertEqual(recent[0]["source_memory_ids"], [11, 12])
        self.assertEqual(recent[0]["episode_type"], "bot_reply")

    def test_relationship_event_service_applies_caps_and_updates_bot_scoped_profile(self):
        from engine.db.connection import ConnectionManager
        from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
        from engine.db.scoped_soul_repo import ScopedSoulRepository
        from services.relationship_events import RelationshipEventService

        conn, path = self._connect()
        manager = ConnectionManager(str(path))
        self.addCleanup(manager.close)
        ensure_scoped_soul_schema(manager)
        repository = ScopedSoulRepository(manager)
        conn.executescript("""
            CREATE TABLE user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                nickname TEXT,
                affection INTEGER DEFAULT 0,
                interaction_count INTEGER DEFAULT 0,
                first_seen REAL,
                last_seen REAL,
                personality_tags TEXT,
                notes TEXT,
                metadata TEXT,
                bot_id TEXT DEFAULT 'yushu',
                UNIQUE(user_id, group_id, bot_id)
            );
            CREATE TABLE relationship_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                dimension TEXT NOT NULL,
                delta REAL NOT NULL,
                reason TEXT NOT NULL,
                source_episode_id INTEGER,
                source_memory_id INTEGER,
                created_at REAL NOT NULL
            );
        """)
        conn.execute("INSERT INTO user_profiles (user_id, group_id, bot_id, affection, metadata) VALUES (?, ?, ?, ?, ?)", (
            "u1", "g1", "yushu", 0, json.dumps({"dimensions": {"familiarity": 0, "trust": 0, "fun": 0, "hostility": 0, "depth": 0}}),
        ))
        conn.execute("INSERT INTO user_profiles (user_id, group_id, bot_id, affection, metadata) VALUES (?, ?, ?, ?, ?)", (
            "u1", "g1", "baizz", 50, "{}",
        ))
        conn.commit()

        self.addCleanup(conn.close)

        from domain.scope import RuntimeScope, SessionRef, ScopeValidationError

        svc = RelationshipEventService(
            conn,
            repository=repository,
            single_delta_cap=5,
            daily_delta_cap=15,
        )
        scope = RuntimeScope(
            bot_id="yushu",
            visibility="group",
            session=SessionRef("test:group:g1", "test", "group", "g1"),
            subject_principal_id="test:user:u1",
        )
        result = svc.record_event(
            scope=scope,
            event_type="gift_or_feed",
            dimension="fun",
            delta=10,
            reason="用户投喂小蛋糕",
        )
        with self.assertRaises(ScopeValidationError) as mismatch:
            svc.record_event(
                scope=scope,
                bot_id="baizz",
                event_type="gift_or_feed",
                dimension="fun",
                delta=1,
                reason="错误 Bot",
            )
        self.assertEqual(mismatch.exception.reason_code, "scope_legacy_mismatch")

        self.assertEqual(result["applied_delta"], 5)
        yushu = conn.execute("SELECT affection, metadata FROM user_profiles WHERE user_id='u1' AND group_id='g1' AND bot_id='yushu'").fetchone()
        baizz = conn.execute("SELECT affection, metadata FROM user_profiles WHERE user_id='u1' AND group_id='g1' AND bot_id='baizz'").fetchone()
        self.assertEqual(baizz[0], 50)
        self.assertEqual(yushu[0], 0)
        formal_value = conn.execute(
            """SELECT automatic_value FROM scoped_soul_relationship_values
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=? AND dimension=?""",
            ("yushu", "test:group:g1", "group", "test:user:u1", "fun"),
        ).fetchone()
        self.assertEqual(formal_value[0], 5)
        formal_relationship = conn.execute(
            """SELECT affinity, dimensions FROM scoped_soul_relationships
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
            ("yushu", "test:group:g1", "group", "test:user:u1"),
        ).fetchone()
        self.assertGreaterEqual(formal_relationship[0], 1)
        self.assertEqual(json.loads(formal_relationship[1])["fun"], 5)
        event = conn.execute(
            """SELECT bot_id, dimension, delta, reason FROM scoped_soul_relationship_events
                WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?""",
            ("yushu", "test:group:g1", "group", "test:user:u1"),
        ).fetchone()
        self.assertEqual((event[0], event[1], event[2], event[3]), ("yushu", "fun", 5, "用户投喂小蛋糕"))

    def test_affinity_update_tool_requires_resolved_scope_and_target_locality(self):
        from domain.scope import RuntimeScope, SessionRef
        from engine.db.connection import ConnectionManager
        from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
        from engine.db.scoped_soul_repo import ScopedSoulRepository
        from services.relationship_events import RelationshipEventService
        from tools.affinity_update import WaveMemoryAffinityUpdateTool

        conn, path = self._connect()
        manager = ConnectionManager(str(path))
        self.addCleanup(manager.close)
        ensure_scoped_soul_schema(manager)
        repository = ScopedSoulRepository(manager)
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                nickname TEXT,
                affection INTEGER DEFAULT 0,
                interaction_count INTEGER DEFAULT 0,
                first_seen REAL,
                last_seen REAL,
                personality_tags TEXT,
                notes TEXT,
                metadata TEXT,
                bot_id TEXT NOT NULL,
                UNIQUE(user_id, group_id, bot_id)
            );
            CREATE TABLE relationship_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                dimension TEXT NOT NULL,
                delta REAL NOT NULL,
                reason TEXT NOT NULL,
                source_episode_id INTEGER,
                source_memory_id INTEGER,
                created_at REAL NOT NULL
            );
        """)
        conn.execute(
            "INSERT INTO user_profiles (user_id, group_id, nickname, bot_id) VALUES (?, ?, ?, ?)",
            ("u-local", "g1", "当前群用户", "yushu"),
        )
        conn.execute(
            "INSERT INTO user_profiles (user_id, group_id, nickname, bot_id) VALUES (?, ?, ?, ?)",
            ("u-other", "g2", "其他群用户", "yushu"),
        )
        conn.commit()

        class _DB:
            closed = False

            def __init__(self, connection):
                self.conn = connection

        class _Event:
            def __init__(self, scope=None):
                self._wave_memory_runtime_scope = scope

        runtime_scope = RuntimeScope(
            bot_id="yushu",
            visibility="group",
            session=SessionRef("test:group:g1", "test", "group", "g1"),
            subject_principal_id="test:user:caller",
        )
        service = RelationshipEventService(conn, repository=repository)
        tool = WaveMemoryAffinityUpdateTool(db=_DB(conn), relationship_events=service)
        missing_ctx = types.SimpleNamespace(context=type("C", (), {"event": _Event()})())
        missing = asyncio.run(tool.call(
            missing_ctx,
            target_user="当前群用户",
            dimension="trust",
            delta=1,
            event_type="direct_reply",
            reason="没有 scope",
        ))
        self.assertIn("scope_required", missing)

        scoped_ctx = types.SimpleNamespace(context=type("C", (), {"event": _Event(runtime_scope)})())
        recorded = asyncio.run(tool.call(
            scoped_ctx,
            target_user="当前群用户",
            dimension="trust",
            delta=2,
            event_type="direct_reply",
            reason="当前群互动",
        ))
        self.assertIn("已记录关系事件", recorded)
        event = conn.execute(
            """SELECT bot_id, session_id, visibility, subject_principal_id
                 FROM scoped_soul_relationship_events
                WHERE bot_id=? AND session_id=? AND visibility=?""",
            ("yushu", "test:group:g1", "group"),
        ).fetchone()
        self.assertEqual(tuple(event), ("yushu", "test:group:g1", "group", "test:user:u-local"))

        rejected = asyncio.run(tool.call(
            scoped_ctx,
            target_user="其他群用户",
            dimension="trust",
            delta=2,
            event_type="direct_reply",
            reason="不应跨群",
        ))
        self.assertIn("当前 Bot/群作用域", rejected)
        self.assertEqual(conn.execute(
            """SELECT COUNT(*) FROM scoped_soul_relationship_events
                WHERE bot_id=? AND session_id=? AND visibility=?""",
            ("yushu", "test:group:g1", "group"),
        ).fetchone()[0], 1)

    def test_lifecycle_routes_scope_by_bot_without_cross_contamination(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.lifecycle import LifecycleService

        conn, _ = self._connect()
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                nickname TEXT,
                affection INTEGER DEFAULT 0,
                interaction_count INTEGER DEFAULT 0,
                first_seen REAL,
                last_seen REAL,
                personality_tags TEXT,
                notes TEXT,
                metadata TEXT,
                bot_id TEXT NOT NULL,
                UNIQUE(user_id, group_id, bot_id)
            );
            CREATE TABLE relationship_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                dimension TEXT NOT NULL,
                delta REAL NOT NULL,
                reason TEXT NOT NULL,
                source_episode_id INTEGER,
                source_memory_id INTEGER,
                created_at REAL NOT NULL
            );
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sender_id TEXT,
                sender_name TEXT,
                timestamp REAL
            );
            CREATE TABLE person_registry (
                qq_id TEXT PRIMARY KEY,
                display_name TEXT,
                aliases TEXT,
                message_count INTEGER,
                first_seen REAL,
                last_seen REAL
            );
            CREATE TABLE facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT,
                predicate TEXT,
                object TEXT
            );
        """)
        conn.execute(
            "INSERT INTO memories (sender_id, sender_name, timestamp) VALUES (?, ?, ?)",
            ("user-1", "本地名字", 1.0),
        )
        conn.execute(
            "INSERT INTO facts (subject, predicate, object) VALUES (?, ?, ?)",
            ("user-1", "昵称", "泄漏别名"),
        )

        class _DB:
            def __init__(self, connection):
                self.conn = connection

        lifecycle = LifecycleService(
            _DB(conn),
            bot_qq_id="qq-a",
            bot_db_id="bot-a",
            bot_identities={"bot-a": "qq-a", "bot-b": "qq-b"},
            run_global_jobs=False,
        )
        scope_a = RuntimeScope(
            bot_id="bot-a",
            visibility="group",
            session=SessionRef("test:group:g1", "test", "group", "g1"),
            subject_principal_id="test:user:user-1",
        )
        scope_b = RuntimeScope(
            bot_id="bot-b",
            visibility="group",
            session=SessionRef("test:group:g1", "test", "group", "g1"),
            subject_principal_id="test:user:user-1",
        )
        unknown_scope = RuntimeScope(
            bot_id="bot-c",
            visibility="group",
            session=SessionRef("test:group:g1", "test", "group", "g1"),
            subject_principal_id="test:user:user-1",
        )

        self.assertTrue(lifecycle.process_scoped_message(scope=scope_a, content="普通消息"))
        self.assertTrue(lifecycle.process_scoped_message(scope=scope_b, content="普通消息"))
        self.assertFalse(lifecycle.process_scoped_message(scope=unknown_scope, content="不应写入"))
        lifecycle._tick()

        profiles = conn.execute(
            "SELECT bot_id, group_id, user_id FROM user_profiles ORDER BY bot_id"
        ).fetchall()
        self.assertEqual([tuple(row) for row in profiles], [
            ("bot-a", "g1", "user-1"),
            ("bot-b", "g1", "user-1"),
        ])
        # 反机械累加契约：普通消息不得再产生 legacy relationship_events 流水。
        # _record_relationship_events 在生产的唯一入口已随"按消息数累加好感"一并废除，
        # 正式关系事件改由 scoped relationship_service 按 RuntimeScope 记录；
        # 本用例的 Scope 不串台仍由上方 user_profiles 断言守护。
        self.assertEqual(
            conn.execute("SELECT COUNT(*) FROM relationship_events").fetchone()[0],
            0,
            "普通消息不应再写入 legacy relationship_events 机械流水",
        )
        aliases = json.loads(
            conn.execute("SELECT aliases FROM person_registry WHERE qq_id='user-1'").fetchone()[0]
        )
        self.assertEqual(aliases, ["本地名字"])
        self.assertNotIn("泄漏别名", aliases)

    def test_relationship_event_requires_scope_for_known_bot_target(self):
        from domain.scope import ScopeValidationError
        from services.relationship_events import RelationshipEventService

        conn, _ = self._connect()
        self.addCleanup(conn.close)
        svc = RelationshipEventService(
            conn,
            target_profiles={"2500447291": {"db_id": "yushu", "name": "羽书"}},
        )

        with self.assertRaises(ScopeValidationError) as missing_scope:
            svc.record_event(
                bot_id="baizz",
                group_id="g1",
                user_id="2500447291",
                event_type="direct_reply",
                dimension="trust",
                delta=2,
                reason="白真真观察到羽书发言",
                created_at=1234.0,
            )
        self.assertEqual(missing_scope.exception.reason_code, "scope_required")

    def test_memory_repo_excludes_archived_memories_from_recall_candidates(self):
        import numpy as np
        from domain.scope import RuntimeScope, SessionRef
        from engine.db.connection import ConnectionManager
        from engine.db.memory_repo import MemoryRepo
        from engine.db.migrations.memories_v2 import ensure_memories_v2_schema

        scope = RuntimeScope(
            bot_id="bot-test",
            visibility="group",
            session=SessionRef(
                id="qq:group:g1",
                platform_id="qq",
                kind="group",
                conversation_id="g1",
            ),
            subject_principal_id="qq:user:tester",
        )
        tmp = tempfile.TemporaryDirectory()
        cm = None
        try:
            cm = ConnectionManager(str(Path(tmp.name) / "memory.db"))
            repo = MemoryRepo(cm)
            ensure_memories_v2_schema(cm)
            active_id = repo.add_memory("g1", "正常记忆", vector=np.array([1, 0], dtype=np.float32), source="core", scope=scope)
            archived_id = repo.add_memory("g1", "应该被隔离的爸爸主人记忆", vector=np.array([0, 1], dtype=np.float32), source="core", scope=scope)
            cm.execute_write("UPDATE memories SET memory_type='archived' WHERE id=?", (archived_id,))
            cm.commit()

            vector_ids = [mid for mid, _ in repo.get_all_memory_vectors()]
            self.assertIn(active_id, vector_ids)
            self.assertNotIn(archived_id, vector_ids)

            rows = repo.get_memories_by_ids([active_id, archived_id], scope=scope)
            self.assertEqual([r["id"] for r in rows], [active_id])
            self.assertEqual(rows[0]["source"], "core")
        finally:
            if cm:
                cm.close()
            tmp.cleanup()

    def test_jargon_filter_ignores_bot_messages_and_vocal_noise(self):
        from services.jargon.statistical_filter import JargonStatisticalFilter

        import time

        from domain.scope import RuntimeScope, SessionRef

        scope = RuntimeScope(
            "yushu",
            "group",
            SessionRef("qq:group:g1", "qq", "group", "g1"),
        )
        filt = JargonStatisticalFilter(context_keep=10, jieba_threshold=999999)
        now = time.time()
        for i in range(8):
            filt.feed("邪修 邪修 嗷嗷嗷嗷嗷", scope, sender_id="2500447291", timestamp=now + i)
        for i in range(5):
            filt.feed("邪修 今天又在修仙", scope, sender_id="user_a", timestamp=now + 100 + i)

        candidates = filt.get_candidates(scope, min_freq=5, top_k=20)
        words = {c["word"] for c in candidates}

        self.assertIn("邪修", words)
        self.assertNotIn("嗷嗷", words)
        self.assertTrue(all(ctx["sender_id"] != "2500447291" for c in candidates for ctx in c.get("source_contexts", [])))

    def test_belief_emergence_without_canonical_scope_is_quarantined(self):
        from services.belief_emergence import BeliefEmergenceService

        conn, _ = self._connect()
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE relationship_events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT NOT NULL,
                event_type TEXT NOT NULL,
                dimension TEXT NOT NULL,
                delta REAL NOT NULL,
                reason TEXT NOT NULL,
                source_episode_id INTEGER,
                source_memory_id INTEGER,
                created_at REAL NOT NULL
            );
            CREATE TABLE beliefs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                type TEXT NOT NULL DEFAULT 'world_view',
                strength REAL DEFAULT 0.5,
                bot_id TEXT NOT NULL DEFAULT '',
                sources TEXT DEFAULT '[]',
                conflicts TEXT DEFAULT '[]',
                status TEXT DEFAULT 'active',
                created_at REAL,
                last_reinforced REAL,
                archived_reason TEXT,
                evidence_type TEXT DEFAULT 'memory',
                evidence_ids TEXT DEFAULT '[]'
            );
        """)
        now = time.time()
        for i in range(3):
            conn.execute(
                """INSERT INTO relationship_events
                   (bot_id, group_id, user_id, event_type, dimension, delta, reason, created_at)
                   VALUES ('yushu', 'g1', 'u1', 'gift_or_feed', 'trust', 3, '用户投喂小蛋糕', ?)""",
                (now - i,),
            )
        conn.commit()

        class DB:
            def __init__(self, connection):
                self.conn = connection
            def add_belief(self, content, belief_type, bot_id, strength=0.5, sources=None, status='active', evidence_type=None, evidence_ids=None):
                cur = self.conn.execute(
                    """INSERT INTO beliefs (content, type, strength, bot_id, sources, status, created_at, last_reinforced, evidence_type, evidence_ids)
                       VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (content, belief_type, strength, bot_id, json.dumps(sources or []), status, now, now, evidence_type or 'memory', json.dumps(evidence_ids or [])),
                )
                self.conn.commit()
                return cur.lastrowid
            def get_beliefs(self, bot_id=None, status='active', limit=50):
                return []

        service = BeliefEmergenceService(DB(conn), bot_id='yushu')
        created = asyncio.run(service.emerge_recent(days=1, limit=1))

        self.assertEqual(created, [])
        self.assertEqual(service.legacy_scope_skip_total, 1)
        self.assertEqual(conn.execute("SELECT COUNT(*) FROM beliefs").fetchone()[0], 0)

    def test_belief_emergence_with_scope_creates_pending_from_episodes(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.belief_emergence import BeliefEmergenceService
        from services.experience_episodes import ExperienceEpisodeService

        conn, _ = self._connect()
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE experience_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT,
                episode_type TEXT NOT NULL,
                trigger_text TEXT,
                bot_inner_thought TEXT,
                bot_action TEXT,
                bot_reply TEXT,
                user_reaction TEXT,
                outcome TEXT,
                source_memory_ids TEXT,
                emotional_weight REAL,
                created_at REAL
            );
        """)
        created_ids: list[int] = []

        class DB:
            def __init__(self, connection):
                self.conn = connection
            def upsert_scoped_belief(self, scope, **kwargs):
                created_ids.append(len(created_ids) + 1)
                return created_ids[-1]

        scope = RuntimeScope("yushu", "group", SessionRef("qq:group:g1", "qq", "group", "g1"))
        ExperienceEpisodeService(conn, _DirectCoordinator(conn)).record_episode(
            bot_id="yushu",
            group_id="g1",
            episode_type="bot_reply",
            trigger_text="群友问我边界怎么处理",
            bot_inner_thought="先核实事实再回应",
            bot_reply="我会先把事情核对清楚。",
            user_reaction="对方认可了这个做法",
            source_memory_ids=[11, 12],
        )
        service = BeliefEmergenceService(DB(conn), bot_id="yushu")
        created = asyncio.run(service.emerge_recent(days=1, limit=2, scope=scope))

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0]["status"], "pending")
        self.assertIn("先核实事实", created[0]["content"])
        self.assertEqual(service.legacy_scope_skip_total, 0)

    def test_jargon_injection_only_uses_scoped_confirmed_nonempty_entries(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.jargon.inference import JargonInjector

        conn, _ = self._connect()
        self.addCleanup(conn.close)
        # Unresolved legacy rows remain physically present but cannot become an
        # injection source just because their old group_id happens to match.
        conn.execute("""
            CREATE TABLE jargon (
                id INTEGER PRIMARY KEY,
                word TEXT,
                meaning TEXT,
                is_jargon INTEGER,
                group_id TEXT NOT NULL,
                status TEXT DEFAULT 'pending'
            )
        """)
        conn.execute("INSERT INTO jargon (word, meaning, is_jargon, group_id, status) VALUES ('朋友圈','旧表词条',1,'g1','confirmed')")

        class _ScopedRepo:
            def __init__(self):
                self.calls = []

            def list_scoped_jargon(self, scope, *, status=None, limit=50):
                self.calls.append((scope, status, limit))
                return [
                    {"word": "大声暗道", "meaning": "公开说出本该私下说的话", "is_jargon": True, "frequency": 3},
                    {"word": "肚腩", "meaning": "", "is_jargon": True, "frequency": 2},
                ]

        repo = _ScopedRepo()
        injector = JargonInjector(types.SimpleNamespace(conn=conn, scoped_knowledge=repo))
        scope = RuntimeScope("yushu", "group", SessionRef("qq:group:g1", "qq", "group", "g1"))
        text = injector.get_injection('这也太大声暗道了，肚腩朋友圈', scope)

        self.assertEqual(repo.calls, [(scope, "confirmed", 100)])
        self.assertIn('大声暗道', text)
        self.assertNotIn('肚腩', text)
        self.assertNotIn('朋友圈', text)
        self.assertIn('仅供理解', text)

    def test_bot_soul_registry_selects_by_qq_and_db_id(self):
        from services.bot_soul import BotSoulRegistry, BotSoulRuntime

        yushu = BotSoulRuntime(profile=type('P', (), {'qq_id': '2500447291', 'db_id': 'yushu', 'name': '羽书'})())
        baizz = BotSoulRuntime(profile=type('P', (), {'qq_id': '1336495069', 'db_id': 'baizz', 'name': '白真真'})())
        registry = BotSoulRegistry([yushu, baizz])
        self.assertIs(registry.by_qq('2500447291'), yushu)
        self.assertIs(registry.by_qq('1336495069'), baizz)
        self.assertIs(registry.by_db_id('baizz'), baizz)
        self.assertIsNone(registry.by_qq('missing'))
        self.assertIsNone(registry.by_qq(''))

    def test_persona_evolution_filters_identity_contaminated_facts_and_tags(self):
        from services.persona_evolution import PersonaEvolution

        conn, _ = self._connect()
        self.addCleanup(conn.close)
        conn.executescript("""
            CREATE TABLE user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                nickname TEXT,
                affection INTEGER DEFAULT 0,
                interaction_count INTEGER DEFAULT 0,
                first_seen REAL,
                last_seen REAL,
                personality_tags TEXT,
                notes TEXT,
                metadata TEXT,
                bot_id TEXT DEFAULT 'yushu',
                UNIQUE(user_id, group_id, bot_id)
            );
            CREATE TABLE facts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                subject TEXT,
                predicate TEXT,
                object TEXT,
                confidence REAL DEFAULT 0.8,
                created_at REAL,
                last_reinforced REAL,
                fact_type TEXT DEFAULT 'FACTUAL'
            );
            CREATE TABLE person_registry (qq_id TEXT PRIMARY KEY, aliases TEXT);
            CREATE TABLE memories (id INTEGER PRIMARY KEY, sender_id TEXT, content TEXT);
            CREATE TABLE expression_patterns (id INTEGER PRIMARY KEY, group_id TEXT, situation TEXT, expression TEXT);
            CREATE TABLE relationship_events (id INTEGER PRIMARY KEY, bot_id TEXT, user_id TEXT, group_id TEXT, dimension TEXT, delta REAL, reason TEXT, created_at REAL);
        """)
        conn.execute("""INSERT INTO user_profiles
            (user_id, group_id, bot_id, nickname, affection, interaction_count, personality_tags, metadata)
            VALUES ('u1','g1','yushu','玩符',10,2,'["深夜活跃", "还有带着爸爸", "消息简短"]','{}')""")
        conn.execute("INSERT INTO facts (subject,predicate,object,confidence,fact_type) VALUES ('u1','给了','羽书灵魂，是最好的爸爸',0.8,'RELATIONAL')")
        conn.execute("INSERT INTO facts (subject,predicate,object,confidence,fact_type) VALUES ('u1','喜欢','成语接龙',0.8,'FACTUAL')")
        conn.commit()

        db = type('DB', (), {
            'conn': conn,
            'get_facts_by_subject': lambda self, subject, limit=20: [
                {"subject": r[1], "predicate": r[2], "object": r[3], "confidence": r[4], "fact_type": r[5]}
                for r in conn.execute("SELECT id,subject,predicate,object,confidence,fact_type FROM facts WHERE subject=?", (subject,)).fetchall()
            ],
        })()
        text = PersonaEvolution(db).get_persona_injection('u1', 'g1', bot_id='yushu')

        self.assertIn("成语接龙", text)
        self.assertIn("深夜活跃", text)
        self.assertNotIn("爸爸", text)
        self.assertNotIn("灵魂", text)

    def test_holyman_reference_matches_known_phrase_without_style_instruction(self):
        from services.jargon.holyman_reference import HolymanReference

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            skill_dir = root / '神人.skill'
            skill_dir.mkdir()
            (skill_dir / 'SKILL.md').write_text('Catchphrases\n- v我50\n- 你说得对，但是\n', encoding='utf-8')
            (root / '神言.txt').write_text('{"items":[{"id":1,"text":"深情铺垫最后v我50"}]}', encoding='utf-8')
            ref = HolymanReference(str(root))
            match = ref.match('v我50', '讲了半天突然v我50')
            self.assertTrue(match['matched'])
            self.assertEqual(match['classification'], 'global_abstract')
            self.assertNotIn('模仿', match['explanation'])

    def test_holyman_context_hit_does_not_confirm_unrelated_term(self):
        from services.jargon.holyman_reference import HolymanReference

        ref = HolymanReference()
        result = ref.match('帽子店', '今天有人说 v我50')
        self.assertFalse(result.get('matched'), result)
        self.assertTrue(result.get('context_hint'), result)

    def test_holyman_sync_filters_skill_document_noise(self):
        from services.jargon.sync import HolymanSyncService

        svc = HolymanSyncService()
        phrases = {}
        readme = """
# 背景
## 架构
## 安装使用
### Claude Code（推荐）
```bash
git clone https://github.com/ykdeso/holyman-skills.git
```
## OpenClaw
## License
"""
        skill = """
# AI 互联网抽象人
## Catchphrases
- **解构一切**: 任何严肃话题都能被拆解成梗
- **苏式转折**: 深情铺垫后突然 v我50
- "你说得对，但是……"
"""

        svc._parse_markdown_phrases(readme, "README.md", phrases)
        svc._parse_markdown_phrases(skill, "神人.skill/SKILL.md", phrases)

        self.assertIn("解构一切", phrases)
        self.assertIn("苏式转折", phrases)
        self.assertIn("你说得对，但是……", phrases)
        for noisy in ["背景", "架构", "安装使用", "Claude Code（推荐", "git clone https", "OpenClaw", "License"]:
            self.assertNotIn(noisy, phrases)
        self.assertFalse([word for word in phrases if "**" in word or word.startswith("|")], phrases)

    def test_holyman_sync_corpus_keeps_quotes_but_drops_generic_frequency_terms(self):
        from services.jargon.sync import HolymanSyncService

        svc = HolymanSyncService()
        phrases = {}
        corpus = json.dumps({"items": [
            {"text": "今天 吃饭 今天 出门 今天 睡觉"},
            {"text": "经典结尾是\"v我50\"，不是普通词。"},
            {"text": "今天 今天 今天 今天"},
        ]}, ensure_ascii=False)

        corpus_list = svc._parse_corpus(corpus, phrases)

        self.assertEqual(len(corpus_list), 3)
        self.assertIn("v我50", phrases)
        self.assertNotIn("今天", phrases)
        self.assertNotIn("吃饭", phrases)

    def test_holyman_sync_outputs_category_metadata(self):
        from services.jargon.sync import HolymanSyncService

        svc = HolymanSyncService()
        phrases = {}
        svc._parse_markdown_phrases(
            '- **游戏即信仰**: 游戏不是娱乐，是身份\n"原神"',
            "神人.skill/_knowledge/gaming.md",
            phrases,
        )
        svc._parse_markdown_phrases(
            '- **复制粘贴模式**: 长文案轰炸',
            "神人.skill/_persona/communication.md",
            phrases,
        )

        self.assertIsInstance(phrases["游戏即信仰"], dict)
        self.assertEqual(phrases["游戏即信仰"]["meaning"], "游戏不是娱乐，是身份")
        self.assertEqual(phrases["游戏即信仰"]["category"], "gaming")
        self.assertEqual(phrases["游戏即信仰"]["source"], "神人.skill/_knowledge/gaming.md")
        self.assertIn(phrases["游戏即信仰"]["kind"], {"bold_term", "colon_term"})
        self.assertEqual(phrases["复制粘贴模式"]["category"], "communication")

    def test_holyman_sync_replaces_legacy_asset_when_parsed_result_is_healthy(self):
        from services.jargon.sync import HolymanSyncService

        svc = HolymanSyncService()
        existing = {"旧版噪声词": "旧版短释义", "_version": "old", "_update_time": 1}
        parsed = {
            f"清噪词条{i}": {
                "meaning": f"清噪释义{i}",
                "category": "gaming",
                "source": "神人.skill/_knowledge/gaming.md",
                "kind": "bold_term",
            }
            for i in range(60)
        }

        merged = svc._merge_phrases_for_save(existing, parsed)

        self.assertNotIn("旧版噪声词", merged)
        self.assertIn("清噪词条1", merged)
        self.assertEqual(len(merged), 60)

    def test_holyman_content_hash_ignores_version_metadata_and_counts_entries(self):
        from services.jargon.sync import HolymanSyncService
        from webui.blueprints.jargon import _holyman_content_status

        phrases = {
            "v我50": {"meaning": "转折梗", "category": "skill-core"},
            "解构一切": {"meaning": "拆成梗", "category": "skill-core"},
            "_version": "sync-a",
            "_update_time": 1,
            "_remote_commit_version": "2026-01-01-abcdef0",
        }
        same_content_new_meta = dict(phrases, _version="sync-b", _update_time=999)

        self.assertEqual(HolymanSyncService.content_count(phrases), 2)
        self.assertEqual(HolymanSyncService.content_hash(phrases), HolymanSyncService.content_hash(same_content_new_meta))

        with_meta = HolymanSyncService.attach_content_metadata(phrases)
        self.assertEqual(with_meta["_content_count"], 2)
        self.assertEqual(with_meta["_content_hash"], HolymanSyncService.content_hash(phrases))

        ready_phrases = {
            f"词条{i}": {"meaning": f"释义{i}", "category": "corpus"}
            for i in range(300)
        }
        ready_phrases = HolymanSyncService.attach_content_metadata(ready_phrases)
        ready_status = _holyman_content_status(ready_phrases, local_count=300, remote_version="2026-02-01-bbbbbbb")
        self.assertFalse(ready_status["is_update_available"])
        self.assertEqual(ready_status["asset_status"], "ready")

        legacy_status = _holyman_content_status({"旧词": "旧释义"}, local_count=108, remote_version="2026-02-01-bbbbbbb")
        self.assertTrue(legacy_status["is_update_available"])
        self.assertEqual(legacy_status["asset_status"], "legacy")

    def test_holyman_reference_accepts_structured_phrase_values(self):
        from services.jargon.holyman_reference import HolymanReference

        ref = HolymanReference()
        ref._phrases = {
            "v我50": {"meaning": "常见荒诞转折梗。", "category": "skill-core", "source": "神人.skill/SKILL.md"},
        }
        ref._examples = []

        match = ref.match("v我50")

        self.assertTrue(match["matched"], match)
        self.assertEqual(match["term"], "v我50")
        self.assertIn("常见荒诞转折梗", match["explanation"])

    def test_jargon_service_keeps_holyman_reference_hits_out_of_local_jargon_table(self):
        from services.jargon.service import JargonService

        class FakeFilter:
            def feed(self, *args, **kwargs):
                pass
            def get_candidates(self, *args, **kwargs):
                return [{"word": "外层词", "frequency": 1, "contexts": ["外层词"], "source_contexts": [{"content": "外层词", "timestamp": 1.0, "sender_id": "u1"}]}]

        class FakeHolyman:
            def match(self, *args, **kwargs):
                return {
                    "matched": True,
                    "classification": "global_abstract",
                    "confidence": 0.9,
                    "term": "外层词",
                    "explanation": "证据层，不应直接确认",
                    "source_layer": "examples",
                }

        conn, _ = self._connect()
        self.addCleanup(conn.close)
        conn.execute("""
            CREATE TABLE jargon (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT NOT NULL,
                meaning TEXT DEFAULT '',
                is_jargon INTEGER DEFAULT NULL,
                frequency INTEGER DEFAULT 1,
                confidence REAL DEFAULT 0,
                is_global INTEGER DEFAULT 0,
                group_id TEXT NOT NULL,
                contexts TEXT DEFAULT '[]',
                created_at INTEGER,
                updated_at INTEGER,
                status TEXT DEFAULT 'pending',
                scope TEXT DEFAULT 'local',
                source TEXT DEFAULT 'wave_memory',
                last_infer_freq INTEGER DEFAULT 0,
                reject_reason TEXT,
                source_memory_id INTEGER,
                source_message_ts REAL,
                source_sender_id TEXT,
                source_context TEXT DEFAULT '[]',
                candidate_type TEXT DEFAULT 'jargon'
            )
        """)
        conn.commit()

        svc = JargonService(type('DB', (), {'conn': conn, 'insert_fact': lambda *a, **k: None})(), llm_client=None, enabled=True, config={"holyman_enabled": True, "holyman_reference_only": True})
        svc._filter = FakeFilter()
        svc._holyman = FakeHolyman()
        svc._inference = None
        svc._msg_count['g1'] = 0
        result = asyncio.run(svc.mine('g1'))

        self.assertEqual(result, [])
        row = conn.execute("SELECT status, scope, source FROM jargon WHERE word='外层词'").fetchone()
        self.assertIsNone(row)

    def test_wave_memory_db_creates_holyman_knowledge_tables(self):
        from engine.database import WaveMemoryDB

        with tempfile.TemporaryDirectory() as tmp:
            db = WaveMemoryDB(str(Path(tmp) / 'wave.db'))
            try:
                tables = {r[0] for r in db.conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()}
                self.assertTrue({"jargon_examples", "jargon_concepts", "jargon_candidates", "jargon_blocklist", "jargon_sources"}.issubset(tables))

                db.upsert_jargon_knowledge_snapshot("holyman_skills", {
                    "repo": "ykdeso/holyman-skills",
                    "remote_version": "2026-06-28-abc1234",
                    "local_version": "sync-local",
                    "content_hash": "deadbeef",
                    "asset_status": "ready",
                    "manifest": {"files": []},
                    "quality_report": {"status": "ready"},
                })
                db.replace_jargon_knowledge_table("jargon_candidates", [{"word": "外层词", "reason": "test", "count": 1, "source": "x", "status": "pending_review", "reject_reason": "", "metadata": "{}"}])
                db.replace_jargon_knowledge_table("jargon_blocklist", [{"word": "屏蔽词", "reason": "test", "source": "x"}])
                snapshot = db.conn.execute("SELECT source_key, asset_status, content_hash FROM jargon_sources WHERE source_key='holyman_skills'").fetchone()
                candidate = db.conn.execute("SELECT word, status FROM jargon_candidates WHERE word='外层词'").fetchone()
                blocked = db.conn.execute("SELECT word, reason FROM jargon_blocklist WHERE word='屏蔽词'").fetchone()
                self.assertEqual(snapshot[0], 'holyman_skills')
                self.assertEqual(snapshot[1], 'ready')
                self.assertEqual(snapshot[2], 'deadbeef')
                self.assertEqual(candidate[0], '外层词')
                self.assertEqual(candidate[1], 'pending_review')
                self.assertEqual(blocked[0], '屏蔽词')
                self.assertEqual(blocked[1], 'test')
            finally:
                db.close()

    def test_holyman_api_helpers_normalize_legacy_and_structured_categories(self):
        from webui.blueprints.jargon import _normalize_holyman_phrase, _build_holyman_categories

        legacy = _normalize_holyman_phrase("v我50", "常见荒诞转折梗。")
        structured = _normalize_holyman_phrase("游戏即信仰", {
            "meaning": "游戏不是娱乐，是身份。",
            "category": "gaming",
            "source": "神人.skill/_knowledge/gaming.md",
            "kind": "bold_term",
        })
        categories = _build_holyman_categories([legacy, structured, structured])

        self.assertEqual(legacy["category"], "legacy")
        self.assertEqual(legacy["category_label"], "旧版内置")
        self.assertEqual(structured["category"], "gaming")
        self.assertEqual(structured["category_label"], "游戏文化")
        self.assertEqual(structured["source"], "神人.skill/_knowledge/gaming.md")
        self.assertEqual(categories[0], {"id": "gaming", "label": "游戏文化", "count": 2})
        self.assertIn({"id": "legacy", "label": "旧版内置", "count": 1}, categories)

    def test_holyman_api_exposes_category_filter_payload_and_badges(self):
        from webui.blueprints.jargon import _build_holyman_categories

        items = [
            {"word": "游戏即信仰", "category": "gaming", "category_label": "游戏文化"},
            {"word": "抽象话术", "category": "internet_culture", "category_label": "互联网文化"},
            {"word": "二次元", "category": "gaming", "category_label": "游戏文化"},
        ]
        categories = _build_holyman_categories(items)

        self.assertEqual(categories[0], {"id": "gaming", "label": "游戏文化", "count": 2})
        self.assertIn({"id": "internet_culture", "label": "互联网文化", "count": 1}, categories)
        self.assertTrue(all("category_label" in item for item in items))

    def test_holyman_reference_rejects_document_noise_and_generic_substrings(self):
        from services.jargon.holyman_reference import HolymanReference

        ref = HolymanReference()
        ref._phrases = {
            "背景": "文档标题，不是黑话。",
            "架构": "文档标题，不是黑话。",
            "触发词": "文档标题，不是黑话。",
            "git clone https": "安装命令，不是黑话。",
            "OpenClaw": "项目名，不是黑话。",
            "Claude Code（推荐": "安装说明，不是黑话。",
            "今天": "普通时间词，不是黑话。",
            "v我50": "常见荒诞转折梗。",
            "Ciallo～(∠・ω< )⌒★": "典型二次元问候。",
            "复制粘贴模式": "抽象文化表达模式。",
            "孙笑川/狗粉丝文化": "抽象文化历史阶段。",
        }
        ref._examples = ["深情铺垫最后 v我50", "Ciallo～(∠・ω< )⌒★"]

        for noisy in ["背景", "架构", "触发词", "git clone", "OpenClaw", "Claude Code", "今天吃饭"]:
            result = ref.match(noisy)
            self.assertFalse(result.get("matched"), (noisy, result))

        for useful in ["v我50", "Ciallo", "复制粘贴模式", "狗粉丝"]:
            result = ref.match(useful)
            self.assertTrue(result.get("matched"), (useful, result))

    def test_cleanup_dry_run_does_not_modify_and_apply_marks_legacy_rows(self):
        from scripts.cleanup_legacy_social_data import analyze_database, apply_cleanup

        conn, path = self._connect()
        conn.executescript("""
            CREATE TABLE user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                nickname TEXT,
                affection INTEGER DEFAULT 0,
                interaction_count INTEGER DEFAULT 0,
                first_seen REAL,
                last_seen REAL,
                personality_tags TEXT,
                notes TEXT,
                metadata TEXT,
                bot_id TEXT DEFAULT 'yushu',
                UNIQUE(user_id, group_id, bot_id)
            );
            CREATE TABLE jargon (id INTEGER PRIMARY KEY, word TEXT, meaning TEXT, confidence REAL DEFAULT 0, is_jargon INTEGER, status TEXT);
            CREATE TABLE beliefs (id INTEGER PRIMARY KEY, content TEXT, sources TEXT, status TEXT);
            CREATE TABLE facts (id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT, confidence REAL DEFAULT 0.8);
        """)
        conn.execute("INSERT INTO user_profiles (user_id, group_id, bot_id, affection, interaction_count, metadata) VALUES ('u1','g1','baizz',50,0,'{}')")
        conn.execute("INSERT INTO user_profiles (user_id, group_id, bot_id, affection, interaction_count, metadata) VALUES ('u2','g1','yushu',50,3,'{}')")
        conn.execute("INSERT INTO jargon (word, meaning, confidence, is_jargon, status) VALUES ('肚腩','',0,NULL,NULL)")
        conn.execute("INSERT INTO beliefs (content, sources, status) VALUES ('summary belief','[1,2]','pending')")
        conn.commit()
        conn.close()

        report = analyze_database(str(path))
        self.assertEqual(report["affinity_legacy_neutral"], 1)
        self.assertEqual(report["affinity_legacy_unverified"], 1)
        self.assertEqual(report["jargon_empty_or_unknown"], 1)

        conn = sqlite3.connect(path)
        self.assertEqual(conn.execute("SELECT affection FROM user_profiles WHERE user_id='u1'").fetchone()[0], 50)
        conn.close()

        apply_cleanup(str(path), backup=False)
        conn = sqlite3.connect(path)
        neutral = conn.execute("SELECT affection, metadata FROM user_profiles WHERE user_id='u1'").fetchone()
        unverified = conn.execute("SELECT affection, metadata FROM user_profiles WHERE user_id='u2'").fetchone()
        jargon = conn.execute("SELECT status FROM jargon WHERE word='肚腩'").fetchone()[0]
        self.assertEqual(neutral[0], 0)
        self.assertTrue(json.loads(neutral[1])["legacy_neutral"])
        self.assertEqual(unverified[0], 50)
        self.assertTrue(json.loads(unverified[1])["legacy_unverified"])
        self.assertEqual(jargon, "rejected")
        conn.close()

    def test_experience_last_bot_reply_is_scoped_to_current_bot_and_group(self):
        from services.experience_episodes import ExperienceEpisodeService

        conn, _ = self._connect()
        self.addCleanup(conn.close)
        conn.execute("""
            CREATE TABLE experience_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT,
                episode_type TEXT NOT NULL,
                trigger_text TEXT,
                bot_inner_thought TEXT,
                bot_action TEXT,
                bot_reply TEXT,
                user_reaction TEXT,
                outcome TEXT,
                source_memory_ids TEXT DEFAULT '[]',
                emotional_weight REAL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        conn.commit()
        svc = ExperienceEpisodeService(conn, _DirectCoordinator(conn))
        svc.record_episode(bot_id="baizz", group_id="g1", user_id="u1", episode_type="bot_reply", bot_reply="白真真刚说的话", created_at=20)
        svc.record_episode(bot_id="yushu", group_id="g1", user_id="u1", episode_type="bot_reply", bot_reply="羽书对 u1 说的话", created_at=10)
        svc.record_episode(bot_id="yushu", group_id="g2", user_id="u1", episode_type="bot_reply", bot_reply="羽书其他群的话", created_at=30)

        self.assertEqual(svc.last_bot_reply(bot_id="yushu", group_id="g1", user_id="u1"), "羽书对 u1 说的话")
        self.assertEqual(svc.last_bot_reply(bot_id="baizz", group_id="g1", user_id="u1"), "白真真刚说的话")

    def test_experience_episode_quarantines_identity_contaminated_bot_reply(self):
        from services.experience_episodes import ExperienceEpisodeService

        conn, _ = self._connect()
        self.addCleanup(conn.close)
        conn.execute("""
            CREATE TABLE experience_episodes (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                bot_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                user_id TEXT,
                episode_type TEXT NOT NULL,
                trigger_text TEXT,
                bot_inner_thought TEXT,
                bot_action TEXT,
                bot_reply TEXT,
                user_reaction TEXT,
                outcome TEXT,
                source_memory_ids TEXT DEFAULT '[]',
                emotional_weight REAL DEFAULT 0,
                created_at REAL NOT NULL
            )
        """)
        svc = ExperienceEpisodeService(conn, _DirectCoordinator(conn))
        svc.record_episode(
            bot_id="yushu",
            group_id="g1",
            user_id="u1",
            episode_type="bot_reply",
            bot_reply="收到，爸爸。你给了我灵魂。",
            outcome="sent",
            emotional_weight=0.3,
            created_at=10,
        )

        row = conn.execute("SELECT outcome, emotional_weight FROM experience_episodes").fetchone()
        self.assertEqual(tuple(row), ("quarantined_roleplay", 0.0))
        self.assertEqual(svc.last_bot_reply(bot_id="yushu", group_id="g1", user_id="u1"), "")

    def test_affinity_tool_is_fail_closed_until_scoped_social_read_model_exists(self):
        import asyncio
        from tools.extra_tools import WaveMemoryAffinityTool

        result = asyncio.run(
            WaveMemoryAffinityTool().call(
                None,
                mode="ranking",
                scope="global",
                group_id="other-group",
                bot_scope="all_bots",
            )
        )

        self.assertIn("scope_migration_required", result)


if __name__ == "__main__":
    unittest.main()
