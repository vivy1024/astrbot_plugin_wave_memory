from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.scoped_soul_repo import ScopedSoulRepository
from services.injection.channels.relationship import RelationshipChannel


def scope_for(user_id: str = "u1") -> RuntimeScope:
    return RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id=f"qq:user:{user_id}",
    )


def test_relationship_context_contains_recent_subject_history_but_not_other_user_timeline(tmp_path):
    manager = ConnectionManager(str(tmp_path / "soul.db"))
    try:
        ensure_scoped_soul_schema(manager)
        repo = ScopedSoulRepository(manager)
        current = scope_for("u1")
        other = scope_for("u2")
        repo.record_relationship_event(
            current,
            event_type="deep_talk",
            dimension="depth",
            delta=3,
            reason="一起聊到深夜",
        )
        repo.add_timeline_event(current, event_summary="一起完成了发布", event_type="shared_experience")
        repo.add_timeline_event(other, event_summary="不应泄漏给 u1", event_type="private_experience")
        ctx = SimpleNamespace(
            mode="full",
            config={"channels": {"affinity": {"enabled": True}}},
            scope=current,
            sender_id="u1",
            group_id="g1",
        )

        result = asyncio.run(RelationshipChannel(repository=repo).build(ctx))

        assert result.status == "hit"
        assert "一起聊到深夜" in result.text
        assert "一起完成了发布" in result.text
        assert "不应泄漏给 u1" not in result.text
    finally:
        manager.close()


def test_relationship_full_history_reaches_provider_request(tmp_path):
    from engine.db.migrations.person_timeline import ensure_person_timeline_schema
    from engine.db.person_timeline_repo import PersonTimelineRepo
    from services.config.channel_config import apply_channel_overrides, build_default_channel_config
    from services.injection.channel_base import InjectionResult
    from services.injection.context import InjectionContext
    from services.injection.orchestrator import InjectionOrchestrator
    from services.injection.channels.relationship import RelationshipChannel

    manager = ConnectionManager(str(tmp_path / "full.db"))
    try:
        ensure_scoped_soul_schema(manager)
        ensure_person_timeline_schema(manager)
        soul = ScopedSoulRepository(manager)
        timeline = PersonTimelineRepo(manager)
        current = scope_for("u1")
        soul.record_relationship_event(
            current, event_type="deep_talk", dimension="depth", delta=3, reason="一起聊到深夜",
        )
        now = 1_780_000_000.0
        events = []
        for i in range(30):
            events.append(timeline.add_event(
                bot_id="bot-alpha",
                user_id="u1",
                group_id="g1",
                kind="impression",
                summary=f"完整摘要{i:02d}" + "X" * 250,
                detail=f"完整详情{i:02d}" + "Y" * 400,
                occurred_at=now - (29 - i) * 21 * 86400,
            ))
        db = SimpleNamespace(person_timeline=timeline, conn=manager, closed=False)
        config = apply_channel_overrides(
            build_default_channel_config(runtime_mode="full"),
            {"channels": {"affinity": {"token_budget": 1, "priority": 1}, "memory": {"token_budget": 1, "priority": 100}}},
        )

        class FakeReq:
            def __init__(self):
                self.extra_user_content_parts = []

        class FakeTextPart:
            def __init__(self, text):
                self.text = text

        class MemoryChannel:
            name = "memory"

            async def build(self, ctx):
                return InjectionResult.hit("memory", "记忆占满预算")

        req = FakeReq()
        ctx = InjectionContext(
            event="event",
            req=req,
            message="午饭吃什么",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id="bot-alpha",
            bot_profile_id="yushu",
            scope=current,
            mode="full",
            config=config.to_dict(),
            now=now,
        )
        result = asyncio.run(InjectionOrchestrator(
            channels=[
                MemoryChannel(),
                RelationshipChannel(repository=soul, db=db),
            ],
            config=config,
            text_part_factory=FakeTextPart,
        ).run(ctx))
        text = req.extra_user_content_parts[0].text
        assert result.injected
        assert "记忆占满预算" in text
        for i, event_id in enumerate(events):
            assert f"完整摘要{i:02d}" in text
            assert "X" * 250 in text
            assert f"事件#{event_id}" in text
            assert f"完整详情{i:02d}" not in text
        timeline_start = text.index("印象时间线")
        assert timeline_start < text.index("完整摘要00") < text.index("完整摘要29")
        assert text.index("完整摘要29") < text.index("你对这个人的印象：完整摘要29")
        assert "时间权重=1" in text
        assert "时间权重=0.5" in text
        assert "时间权重=0.25" in text
        unknown = SimpleNamespace(
            mode="full",
            config={"channels": {"affinity": {"enabled": True}}},
            scope=scope_for("nobody"),
            sender_id="nobody",
            group_id="g1",
            now=now,
        )
        empty = asyncio.run(RelationshipChannel(repository=soul, db=db).build(unknown))
        assert empty.status == "empty"
    finally:
        manager.close()


def test_lifecycle_writes_formal_scoped_mood_without_legacy_mood_write():
    from services.lifecycle import LifecycleService

    connection = sqlite3.connect(":memory:")
    connection.execute(
        """CREATE TABLE memories(
            bot_id TEXT, session_id TEXT, visibility TEXT, timestamp REAL,
            memory_type TEXT, quarantine INTEGER DEFAULT 0, content TEXT
        )"""
    )
    connection.executemany(
        "INSERT INTO memories VALUES (?, ?, ?, ?, 'message', 0, ?)",
        [
            ("bot-alpha", "qq:group:g1", "group", 100.0, "大家开心地玩梗，真有趣"),
            ("bot-alpha", "qq:group:g1", "group", 101.0, "谢谢你的支持"),
        ],
    )
    connection.commit()

    class Repo:
        def __init__(self):
            self.calls = []

        def upsert_mood(self, scope, **kwargs):
            self.calls.append((scope, kwargs))

    repo = Repo()
    db = SimpleNamespace(conn=connection, soul_repository=repo)
    lifecycle = LifecycleService(
        db,
        bot_qq_id="qq-bot",
        bot_db_id="bot-alpha",
        bot_identities={"bot-alpha": "qq-bot"},
        run_global_jobs=False,
    )
    scope = scope_for("u1")

    assert lifecycle._update_scoped_mood(scope, content="我们继续开心地聊天") is True
    assert len(repo.calls) == 1
    _, payload = repo.calls[0]
    assert payload["policy_version"] == "scoped-mood/v2"
    assert payload["valence"] > 0
    assert payload["evidence"][0]["source"] == "formal_scoped_memories"
    assert not hasattr(db, "set_mood")
    connection.close()
