import sqlite3
import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.memory_repo import MemoryRepo
from engine.db.migrations.memories_v2 import ensure_memories_v2_schema
from engine.db.migrations.scoped_derived_knowledge import ensure_scoped_derived_knowledge_schema
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from engine.db.migrations.person_timeline import ensure_person_timeline_schema
from engine.db.migrations.scoped_learning_projections import ensure_scoped_learning_projection_schema
from engine.db.migrations.v2_2_experience_rework import run_migration as run_experience_migration
from tools.diary_episode import WaveMemoryRecordDiaryEpisodeTool


class MockEvent:
    def __init__(self, scope):
        self._wave_memory_runtime_scope = scope


class MockInnerContext:
    def __init__(self, event):
        self.event = event


class MockContext:
    def __init__(self, scope):
        self.context = MockInnerContext(MockEvent(scope))


@pytest.fixture
def db_conn(tmp_path):
    db_file = str(tmp_path / "test_diary.db")
    cm = ConnectionManager(db_file)
    MemoryRepo(cm)
    ensure_memories_v2_schema(cm)
    ensure_scoped_derived_knowledge_schema(cm)
    ensure_scoped_soul_schema(cm)
    ensure_person_timeline_schema(cm)
    ensure_scoped_learning_projection_schema(cm)
    cm.close()
    run_experience_migration(db_file)
    
    conn = sqlite3.connect(db_file)
    conn.row_factory = sqlite3.Row
    return conn, db_file


@pytest.mark.asyncio
async def test_record_diary_episode_slender(db_conn):
    conn, db_file = db_conn
    scope = RuntimeScope(
        bot_id="yushu",
        session=SessionRef(id="qq:group:1015727706", platform_id="qq", kind="group", conversation_id="1015727706"),
        visibility="group",
        subject_principal_id="qq:user:1765563156",
    )
    ctx = MockContext(scope)

    db_mock = type("MockDB", (), {"conn": conn})()
    tool = WaveMemoryRecordDiaryEpisodeTool(db=db_mock)

    res = await tool.call(
        ctx,
        diary_title="关于AI开发与深夜闲聊的随笔",
        diary_content="今天在群里和大家聊了关于健身软件开发的架构，探讨了代码闭环。整体氛围融洽。",
        episode_summary="深入交流AI软件开发，澄清常识",
        emotional_weight=8.5,
    )

    assert "✅" in res
    assert "Episode #" in res
    assert "关于AI开发与深夜闲聊的随笔" in res
    assert "scoped_soul_timeline" in res

    # 1. 验证 experience_episodes
    ep = conn.execute("SELECT * FROM experience_episodes WHERE episode_type='daily_diary'").fetchone()
    assert ep is not None
    assert ep["trigger_text"] == "关于AI开发与深夜闲聊的随笔"
    assert "健身软件" in ep["bot_reply"]
    assert ep["emotional_weight"] == 8.5

    # 2. 验证 scoped_soul_timeline
    tl = conn.execute("SELECT * FROM scoped_soul_timeline WHERE event_type='episode'").fetchone()
    assert tl is not None
    assert "【日记】关于AI开发与深夜闲聊的随笔" in tl["event_summary"]
    assert tl["emotional_weight"] == 8.5


@pytest.mark.asyncio
async def test_record_diary_episode_rejects_contamination(db_conn):
    conn, _ = db_conn
    scope = RuntimeScope(
        bot_id="yushu",
        session=SessionRef(id="qq:group:1", platform_id="qq", kind="group", conversation_id="1"),
        visibility="group",
    )
    ctx = MockContext(scope)
    tool = WaveMemoryRecordDiaryEpisodeTool(db=type("MockDB", (), {"conn": conn})())

    res = await tool.call(
        ctx,
        diary_title="我是你的主人专属日记",
        diary_content="今天主人命令我做什么我就做什么",
        episode_summary="听从主人命令",
    )
    assert "角色扮演污染" in res
