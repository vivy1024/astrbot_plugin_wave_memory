"""记忆召回按"Bot 是一个人"的方式工作。

- 只记得自己亲历的：另一个 Bot 看到的消息不属于当前 Bot；
- 私聊里记得对方本人在群里公开说过的话（跨群开关开启时）；
- 私聊内容与别人在群里的发言都不会因此进入私聊召回之外的地方。
"""

from __future__ import annotations

import pytest

from domain.scope import RuntimeScope, SessionRef, subject_local_id
from engine.db.connection import ConnectionManager
from engine.db.memory_repo import MemoryRepo
from engine.db.migrations.memories_v2 import ensure_memories_v2_schema
from engine.query_engine import QueryEngine
from engine.recall_policy import RecallPolicy


def _group(group_id: str, *, bot_id: str = "yushu", platform: str = "qq") -> RuntimeScope:
    return RuntimeScope(bot_id, "group", SessionRef(f"{platform}:group:{group_id}", platform, "group", group_id))


def _private(user_id: str = "u1", *, bot_id: str = "yushu") -> RuntimeScope:
    return RuntimeScope(
        bot_id, "private", SessionRef(f"qq:private:{user_id}", "qq", "private", user_id),
        subject_principal_id=f"qq:user:{user_id}",
    )


@pytest.fixture
def repo(tmp_path):
    cm = ConnectionManager(str(tmp_path / "recall.sqlite3"))
    repo = MemoryRepo(cm)
    ensure_memories_v2_schema(cm)
    try:
        yield repo
    finally:
        cm.close()


def test_subject_local_id_requires_subject_principal():
    assert subject_local_id(_private("u9")) == "u9"
    assert subject_local_id(_group("g1")) == ""


def test_private_chat_recalls_subject_group_messages_of_same_bot_only(repo):
    own_private = repo.add_memory("u1", "私聊里说的", sender_id="u1", scope=_private("u1"))
    other_private = repo.add_memory("u2", "别人的私聊", sender_id="u2", scope=_private("u2"))
    own_group = repo.add_memory("g1", "我在群里说过吉他", sender_id="u1", scope=_group("g1"))
    others_group = repo.add_memory("g1", "别人在群里说的", sender_id="u2", scope=_group("g1"))
    other_bot = repo.add_memory("g2", "另一个 Bot 看到的", sender_id="u1", scope=_group("g2", bot_id="baizz"))
    other_platform = repo.add_memory("g3", "别的平台", sender_id="u1", scope=_group("g3", platform="bilibili"))
    ids = [own_private, other_private, own_group, others_group, other_bot, other_platform]

    strict = repo.get_memories_by_ids(ids, scope=_private("u1"))
    widened = repo.get_memories_by_ids(ids, scope=_private("u1"), private_subject_group_recall=True)

    assert {row["id"] for row in strict} == {own_private}
    assert {row["id"] for row in widened} == {own_private, own_group}


def test_group_recall_never_includes_private_rows_even_when_subject_matches(repo):
    own_private = repo.add_memory("u1", "私聊里说的", sender_id="u1", scope=_private("u1"))
    own_group = repo.add_memory("g2", "我在另一个群说过", sender_id="u1", scope=_group("g2"))

    rows = repo.get_memories_by_ids(
        [own_private, own_group], scope=_group("g1"), allow_cross_group_recall=True,
    )

    assert {row["id"] for row in rows} == {own_group}


def test_recall_policy_enables_private_subject_lane_with_cross_group_switch():
    assert RecallPolicy.from_config(_private(), {"cross_group_enabled": True}).private_subject_group_recall
    assert not RecallPolicy.from_config(_private(), {"cross_group_enabled": False}).private_subject_group_recall
    group_policy = RecallPolicy.from_config(_group("g1"), {"cross_group_enabled": True})
    assert not group_policy.private_subject_group_recall


def test_candidate_filter_matches_repository_lanes():
    private_policy = RecallPolicy.from_config(_private(), {"cross_group_enabled": True})
    own_group = {"id": 1, "visibility": "group", "bot_id": "yushu", "session_id": "qq:group:g1",
                 "group_id": "g1", "sender_id": "u1"}
    others_group = dict(own_group, id=2, sender_id="u2")
    other_bot = dict(own_group, id=3, bot_id="baizz")
    assert QueryEngine._candidate_matches_memory_policy(own_group, private_policy)
    assert not QueryEngine._candidate_matches_memory_policy(others_group, private_policy)
    assert not QueryEngine._candidate_matches_memory_policy(other_bot, private_policy)

    group_policy = RecallPolicy.from_config(_group("g1"), {"cross_group_enabled": True})
    assert QueryEngine._candidate_matches_memory_policy(own_group, group_policy)
    assert not QueryEngine._candidate_matches_memory_policy(other_bot, group_policy)
    legacy = {"id": 4, "visibility": "", "bot_id": "", "group_id": "g9"}
    assert QueryEngine._candidate_matches_memory_policy(legacy, group_policy)
