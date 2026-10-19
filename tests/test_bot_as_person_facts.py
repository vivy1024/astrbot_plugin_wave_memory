"""事实按"Bot 是一个人"的方式召回。

- 事实属于 Bot 对人与世界的认识：同一 Bot 在哪个群得知都成立，私聊里也认得；
- 其他 Bot 记下的事实不属于当前 Bot；
- 私聊里得知的事实只在同一私聊中使用，绝不注入群聊。
"""

from __future__ import annotations

import asyncio
import time

import numpy as np

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from services.injection.channels.facts import FactsChannel
from services.injection.context import InjectionContext


def _group(group_id: str, bot_id: str = "bot-a") -> RuntimeScope:
    return RuntimeScope(bot_id, "group", SessionRef(f"qq:group:{group_id}", "qq", "group", group_id))


def _private(user_id: str = "u1", bot_id: str = "bot-a") -> RuntimeScope:
    return RuntimeScope(
        bot_id, "private", SessionRef(f"qq:private:{user_id}", "qq", "private", user_id),
        subject_principal_id=f"qq:user:{user_id}",
    )


def _ctx(scope: RuntimeScope, message: str) -> InjectionContext:
    return InjectionContext(
        event="event", req=object(), message=message,
        group_id=scope.session.conversation_id, sender_id="u1", sender_name="雨沐",
        bot_id=scope.bot_id, bot_profile_id=scope.bot_id, scope=scope,
        config={"channels": {"facts": {"max_items": 5, "token_budget": 400}}},
        now=time.time(), trace_id="trace-bot-as-person",
    )


def _build(db: WaveMemoryDB, scope: RuntimeScope, message: str):
    return asyncio.run(FactsChannel(db=db).build(_ctx(scope, message)))


def test_scoped_facts_follow_the_bot_across_groups_and_into_private(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "facts.sqlite"), dimension=3)
    try:
        db.upsert_scoped_fact(_group("g2"), subject="雨沐", predicate="喜欢", object="手冲咖啡",
                              confidence=0.9, status="approved")
        db.upsert_scoped_fact(_group("g2", bot_id="bot-b"), subject="雨沐", predicate="讨厌", object="速溶咖啡",
                              confidence=0.9, status="approved")

        in_other_group = _build(db, _group("g1"), "咖啡")
        in_private = _build(db, _private(), "咖啡")

        for result in (in_other_group, in_private):
            assert result.status == "hit"
            assert "雨沐 喜欢 手冲咖啡" in result.text
            assert "讨厌" not in result.text
    finally:
        db.close()



def test_rare_person_name_is_recognized_even_when_jieba_splits_it(tmp_path):
    """jieba 会把"雨沐"切成单字；已知实体名要按原样在消息里认出来。"""
    from services.injection.channels.facts import _keywords

    assert "雨沐" not in _keywords("雨沐 最近怎么样")
    db = WaveMemoryDB(str(tmp_path / "names.sqlite"), dimension=3)
    try:
        db.upsert_scoped_fact(_group("g1"), subject="雨沐", predicate="在准备", object="考研",
                              confidence=0.9, status="approved")
        result = _build(db, _group("g1"), "雨沐 最近怎么样")
        assert result.status == "hit"
        assert "雨沐 在准备 考研" in result.text
    finally:
        db.close()


def test_approved_private_fact_is_injected_only_in_its_private_chat(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "private-facts.sqlite"), dimension=3)
    try:
        db.upsert_scoped_fact(_private("u1"), subject="雨沐", predicate="在准备", object="出国留学",
                              confidence=0.9, status="active")
        assert "出国留学" in _build(db, _private("u1"), "雨沐 最近").text
        assert "出国留学" not in (_build(db, _group("g1"), "雨沐 最近").text or "")
        assert "出国留学" not in (_build(db, _private("u2"), "雨沐 最近").text or "")
    finally:
        db.close()
