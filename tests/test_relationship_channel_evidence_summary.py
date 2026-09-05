"""RelationshipChannel injects historical_audit_summary when present."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from services.injection.channels.relationship import RelationshipChannel


class _Repo:
    def __init__(self, state):
        self._state = state

    def get_state(self, scope, subject_principal_id=None, limit=25, offset=0):
        return self._state


def _scope():
    return RuntimeScope(
        bot_id="yushu",
        visibility="group",
        session=SessionRef("qq:group:g1", "qq", "group", "g1"),
        subject_principal_id="qq:user:1",
    )


def test_channel_appends_evidence_summary():
    state = {
        "relationship": {
            "affinity": 12,
            "state": "neutral",
            "dimensions": {"familiarity": 10},
            "values": {},
            "revision": 3,
            "evidence": [
                {"relationship_event_id": 1},
                {
                    "kind": "historical_audit_summary",
                    "summary": "历史审计事件 3786 条；类型：direct_reply×3786",
                    "affects_affinity": False,
                },
            ],
        },
        "relationship_history": {"items": []},
        "timeline": {"items": []},
        "revision": 3,
    }
    ch = RelationshipChannel(repository=_Repo(state))
    ctx = SimpleNamespace(
        mode="full",
        config={"channels": {"affinity": {"enabled": True}}},
        scope=_scope(),
    )
    result = asyncio.run(ch.build(ctx))
    assert result.status == "hit"
    assert "历史关系摘要" in result.text
    assert "3786" in result.text
    assert "好感度" not in result.text or "综合值=12" in result.text


def test_channel_without_summary_still_works():
    state = {
        "relationship": {
            "affinity": 1,
            "state": "neutral",
            "dimensions": {},
            "values": {},
            "revision": 1,
            "evidence": [{"relationship_event_id": 9}],
        },
        "relationship_history": {"items": []},
        "timeline": {"items": []},
        "concerns": {
            "items": [
                {"topic": "排查考研数学复习进度", "intensity": 0.8},
                {"topic": "低频杂音", "intensity": 0.2},
            ]
        },
        "revision": 1,
    }
    ch = RelationshipChannel(repository=_Repo(state))
    ctx = SimpleNamespace(
        mode="full",
        config={"channels": {"affinity": {"enabled": True}}},
        scope=_scope(),
    )
    result = asyncio.run(ch.build(ctx))
    assert result.status == "hit"
    assert "历史关系摘要" not in result.text
    assert "当前前情关切" in result.text
    assert "排查考研数学复习进度" in result.text
    assert "低频杂音" not in result.text


def test_channel_puts_impression_before_status_and_skips_message_seen_noise():
    class _Conn:
        def execute(self, sql, params=None):
            class _Row:
                def fetchone(self_inner):
                    return ('{"impression":"愿意核对事实","impression_event":{"event_type":"deep_talk","reason":"深夜长谈"}}',)
            return _Row()

    class _ImpressionRepo:
        def __init__(self, state):
            self._state = state
            self.cm = SimpleNamespace(conn=_Conn())

        def get_state(self, scope, subject_principal_id=None, limit=25, offset=0):
            return self._state

    state = {
        "relationship": {
            "affinity": 12,
            "state": "neutral",
            "dimensions": {"familiarity": 10},
            "values": {},
            "revision": 3,
            "evidence": [],
        },
        "relationship_history": {
            "items": [
                {"event_type": "message_seen", "reason": "看见一条群友消息"},
                {"event_type": "deep_talk", "reason": "深夜长谈"},
            ]
        },
        "timeline": {"items": []},
    }
    ch = RelationshipChannel(repository=_ImpressionRepo(state))
    ctx = SimpleNamespace(
        mode="full",
        config={"channels": {"affinity": {"enabled": True}}},
        scope=_scope(),
        sender_id="1",
        group_id="g1",
    )
    result = asyncio.run(ch.build(ctx))
    assert result.status == "hit"
    assert result.text.startswith("你对这个人的印象：愿意核对事实")
    assert "最近关系线索：deep_talk：深夜长谈" in result.text
    assert "看见一条群友消息" not in result.text
    assert result.text.index("你对这个人的印象") < result.text.index("综合值=12")
