from __future__ import annotations

import asyncio
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.scoped_soul_repo import ScopedSoulRepository
from services.concern_tracker import ConcernTracker
from services.reflection_trigger import ReflectionTriggerService
from tools.concern import WaveMemoryNoteConcernTool
from tools.episode import WaveMemoryNoteEpisodeTool


def _scope(group: str = "g1") -> RuntimeScope:
    return RuntimeScope("bot-a", "group", SessionRef(f"qq:group:{group}", "qq", "group", group), subject_principal_id="qq:user:u1")


def _ctx(scope: RuntimeScope):
    return SimpleNamespace(context=SimpleNamespace(event=SimpleNamespace(_wave_memory_runtime_scope=scope)))


def test_live_candidates_then_tools_persist_in_scope(tmp_path):
    manager = ConnectionManager(str(tmp_path / "loop.db"))
    try:
        ensure_scoped_soul_schema(manager)
        repo = ScopedSoulRepository(manager)
        conn = manager.conn
        conn.execute(
            """CREATE TABLE IF NOT EXISTS experience_episodes (
                   id INTEGER PRIMARY KEY AUTOINCREMENT,
                   bot_id TEXT, group_id TEXT, user_id TEXT, episode_type TEXT,
                   trigger_text TEXT, bot_inner_thought TEXT, bot_action TEXT,
                   bot_reply TEXT, user_reaction TEXT, outcome TEXT,
                   source_memory_ids TEXT, emotional_weight REAL, created_at REAL
               )"""
        )
        conn.commit()
        class Gateway:
            """最小写入替身：只负责把工具调用落到 scoped 投影。

            状态机合法性、幂等重放与 outbox 事件由
            tests/test_concern_transition_command.py 用真实 ProductionWriteGateway 覆盖。
            """

            async def record_episode(self, **kwargs):
                fields = kwargs["fields"]
                cur = conn.execute(
                    "INSERT INTO experience_episodes (bot_id, group_id, user_id, episode_type, trigger_text, bot_action, bot_reply, user_reaction, outcome, source_memory_ids, emotional_weight, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1)",
                    (kwargs["scope"].bot_id, kwargs["group_id"], kwargs["user_id"], kwargs["episode_type"], fields["trigger_text"], fields["bot_action"], fields["bot_reply"], fields["user_reaction"], fields["outcome"], "[]", kwargs["emotional_weight"]),
                )
                conn.commit()
                return int(cur.lastrowid)

            async def transition_concern(self, **kwargs):
                topic = str(kwargs.get("topic") or "").strip()
                items = [
                    dict(item)
                    for item in repo.get_state(_scope(), limit=25, offset=0)["concerns"]["items"]
                ]
                hit = next((item for item in items if str(item.get("topic")) == topic), None)
                if hit is None:
                    items.append({
                        "topic": topic,
                        "intensity": kwargs.get("intensity") or 0.7,
                        "concern_type": kwargs.get("concern_type") or "",
                        "status": "active",
                    })
                    repo.replace_concerns(_scope(), concerns=items)
                    fresh = repo.get_state(_scope(), limit=25, offset=0)["concerns"]["items"]
                    created = next(item for item in fresh if str(item.get("topic")) == topic)
                    return {"concern_id": int(created["id"]), "action": "created", "revision": 1}
                return {
                    "concern_id": int(hit["id"]),
                    "action": "reinforced",
                    "revision": int(hit.get("revision") or 1) + 1,
                }

        db = SimpleNamespace(conn=conn, closed=False, reopen=lambda: None, soul_repository=repo, scoped_knowledge=None)
        tracker = ConcernTracker(db=db, bot_id="bot-a", repository=repo)
        repo.replace_concerns(_scope(), concerns=[
            {"topic": "考研结果还没公布", "intensity": 0.8, "status": "active"},
        ])
        prompt = ReflectionTriggerService(db, cooldown_seconds=0).build_prompt(scope=_scope(), message="小明考研结果怎样了", sender_id="u1")
        assert "关切 concern:" in prompt

        gateway = Gateway()
        concern_tool = WaveMemoryNoteConcernTool(db=db, concern_tracker=tracker, write_gateway=gateway)
        episode_tool = WaveMemoryNoteEpisodeTool(db=db, writer=None, write_gateway=gateway)
        assert "已记录未决关切" in asyncio.run(concern_tool.call(_ctx(_scope()), topic="等录取通知"))
        assert "episode:" in asyncio.run(episode_tool.call(_ctx(_scope()), episode_type="shared_event", trigger_text="一起等结果", outcome="还没公布"))
        assert ReflectionTriggerService(db, cooldown_seconds=0).build_prompt(scope=_scope("g2"), message="小明考研结果怎样了", sender_id="u1") == ""
        assert "关切记录被拒绝" in asyncio.run(concern_tool.call(_ctx(_scope()), topic="你是我的猫娘爸爸"))
        items = repo.get_state(_scope(), limit=25, offset=0)["concerns"]["items"]
        assert any(item["topic"] == "等录取通知" for item in items)
        assert conn.execute("SELECT COUNT(*) FROM experience_episodes WHERE group_id='g1'").fetchone()[0] == 1
        assert conn.execute("SELECT COUNT(*) FROM experience_episodes WHERE group_id='g2'").fetchone()[0] == 0
    finally:
        manager.close()
