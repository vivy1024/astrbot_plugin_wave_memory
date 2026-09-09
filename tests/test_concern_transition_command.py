"""Concern 生命周期必须经真实 ProductionWriteGateway → WriteCoordinator → domain 表 + outbox。

覆盖阶段 2 验收：单行推进不互相覆盖、幂等重放、非法跳转拒绝、Scope 隔离、
结案记录保留、以及"再次提及不得悄悄复活已结案关切"。
"""

from __future__ import annotations

import asyncio
import json

import pytest

from domain.commands import CommandRejectedError
from domain.scope import RuntimeScope, SessionRef
from engine.db.connection import ConnectionManager
from engine.db.migrations.scoped_soul import ensure_scoped_soul_schema
from services.system_convergence_runtime import ProductionWriteGateway


def _scope(bot_id: str = "bot-a", group: str = "g1") -> RuntimeScope:
    return RuntimeScope(
        bot_id=bot_id,
        visibility="group",
        session=SessionRef(f"qq:group:{group}", "qq", "group", group),
    )


def _run(tmp_path, body):
    async def main():
        path = str(tmp_path / "concern-command.sqlite3")
        manager = ConnectionManager(path)
        ensure_scoped_soul_schema(manager)
        manager.close()
        gateway = ProductionWriteGateway(path)
        try:
            await body(gateway)
        finally:
            await gateway.shutdown()

    asyncio.run(main())


async def _concerns(gateway):
    return await gateway.coordinator.read(
        lambda conn: conn.execute(
            """SELECT id, topic, status, intensity, revision, resolution_note, concern_type
                 FROM scoped_soul_concerns ORDER BY id"""
        ).fetchall()
    )


async def _outbox_events(gateway):
    return await gateway.coordinator.read(
        lambda conn: conn.execute(
            """SELECT event_type, aggregate_id, aggregate_version, payload_json
                 FROM domain_outbox WHERE aggregate_kind='soul_concern' ORDER BY event_id"""
        ).fetchall()
    )


# ---- 创建与强化 ------------------------------------------------------------


def test_note_creates_single_row_and_emits_outbox(tmp_path):
    async def body(gateway):
        result = await gateway.transition_concern(
            scope=_scope(),
            action="note",
            topic="考研结果还没公布",
            intensity=0.8,
            concern_type="follow_up",
            idempotency_hint="turn-1",
        )
        assert result["action"] == "created"
        rows = await _concerns(gateway)
        assert len(rows) == 1
        assert rows[0][1] == "考研结果还没公布"
        assert rows[0][2] == "active"
        assert rows[0][4] == 1
        events = await _outbox_events(gateway)
        assert [e[0] for e in events] == ["soul_concern.created"]
        payload = json.loads(events[0][3])
        assert payload["to_status"] == "active"
        assert payload["scope"]["bot_id"] == "bot-a"

    _run(tmp_path, body)


def test_repeated_mention_reinforces_without_new_row(tmp_path):
    """跨轮再次提及：强化既有关切，且不得新增行。"""

    async def body(gateway):
        first = await gateway.transition_concern(
            scope=_scope(), action="note", topic="考研结果", intensity=0.5, idempotency_hint="turn-1"
        )
        before = await _concerns(gateway)
        second = await gateway.transition_concern(
            scope=_scope(), action="note", topic="考研结果", intensity=0.5, idempotency_hint="turn-2"
        )
        after = await _concerns(gateway)
        assert first["concern_id"] == second["concern_id"]
        assert second["action"] == "reinforced"
        assert len(after) == 1, "同 topic 不得建立第二条关切"
        assert after[0][3] > before[0][3], "再次提及必须提升强度"
        assert after[0][4] == before[0][4] + 1

    _run(tmp_path, body)


def test_same_idempotency_hint_replays_without_mutation(tmp_path):
    """同一轮重试必须被协调器重放：行数与 revision 均不变。"""

    async def body(gateway):
        await gateway.transition_concern(
            scope=_scope(), action="note", topic="等录取通知", idempotency_hint="turn-9"
        )
        snapshot = await _concerns(gateway)
        again = await gateway.transition_concern(
            scope=_scope(), action="note", topic="等录取通知", idempotency_hint="turn-9"
        )
        after = await _concerns(gateway)
        assert len(after) == 1
        assert after[0][4] == snapshot[0][4], "重放不得再次递增 revision"
        events = await _outbox_events(gateway)
        assert len(events) == 1, "重放不得产生第二条 outbox 事件"

    _run(tmp_path, body)


# ---- 生命周期 --------------------------------------------------------------


def test_progress_resolve_and_record_resolution_note(tmp_path):
    async def body(gateway):
        created = await gateway.transition_concern(
            scope=_scope(), action="note", topic="考研结果", idempotency_hint="t1"
        )
        cid = created["concern_id"]
        progressed = await gateway.transition_concern(
            scope=_scope(), action="progress", concern_id=cid, idempotency_hint="t2"
        )
        assert progressed["action"] == "progress"
        row = (await _concerns(gateway))[0]
        assert row[2] == "progressing"
        assert row[5] == ""

        resolved = await gateway.transition_concern(
            scope=_scope(),
            action="resolve",
            concern_id=cid,
            note="已确认上岸",
            idempotency_hint="t3",
        )
        assert resolved["action"] == "resolve"
        row = (await _concerns(gateway))[0]
        assert row[2] == "resolved"
        assert row[5] == "已确认上岸", "结案说明必须持久化"

    _run(tmp_path, body)


def test_closed_concern_is_not_silently_revived_by_note(tmp_path):
    """已结案关切被再次提及时不得悄悄复活，必须显式 reopen。"""

    async def body(gateway):
        created = await gateway.transition_concern(
            scope=_scope(), action="note", topic="复试安排", idempotency_hint="t1"
        )
        cid = created["concern_id"]
        await gateway.transition_concern(
            scope=_scope(), action="resolve", concern_id=cid, note="已敲定", idempotency_hint="t2"
        )
        ignored = await gateway.transition_concern(
            scope=_scope(), action="note", topic="复试安排", idempotency_hint="t3"
        )
        assert ignored["action"] == "note_ignored_closed"
        assert (await _concerns(gateway))[0][2] == "resolved", "状态必须保持已结案"
        assert len(await _concerns(gateway)) == 1

        reopened = await gateway.transition_concern(
            scope=_scope(), action="reopen", concern_id=cid, idempotency_hint="t4"
        )
        assert reopened["action"] == "reopen"
        assert (await _concerns(gateway))[0][2] == "active"

    _run(tmp_path, body)


def test_archived_is_terminal(tmp_path):
    async def body(gateway):
        created = await gateway.transition_concern(
            scope=_scope(), action="note", topic="旧挂念", idempotency_hint="t1"
        )
        cid = created["concern_id"]
        await gateway.transition_concern(
            scope=_scope(), action="resolve", concern_id=cid, note="结束", idempotency_hint="t2"
        )
        archived = await gateway.transition_concern(
            scope=_scope(), action="archive", concern_id=cid, idempotency_hint="t3"
        )
        assert archived["action"] == "archive"
        assert (await _concerns(gateway))[0][2] == "archived"
        with pytest.raises(CommandRejectedError) as exc:
            await gateway.transition_concern(
                scope=_scope(), action="reopen", concern_id=cid, idempotency_hint="t4"
            )
        assert exc.value.code == "invalid_concern_transition"

    _run(tmp_path, body)


def test_unsupported_action_is_rejected(tmp_path):
    async def body(gateway):
        with pytest.raises(ValueError):
            await gateway.transition_concern(
                scope=_scope(), action="delete", topic="x", idempotency_hint="t1"
            )

    _run(tmp_path, body)


# ---- 并发与 Scope ----------------------------------------------------------


def test_two_concerns_do_not_overwrite_each_other(tmp_path):
    """旧全量替换会让并发写互相覆盖；单行命令必须各自保留。"""

    async def body(gateway):
        await gateway.transition_concern(
            scope=_scope(), action="note", topic="关切甲", idempotency_hint="a1"
        )
        await gateway.transition_concern(
            scope=_scope(), action="note", topic="关切乙", idempotency_hint="b1"
        )
        await gateway.transition_concern(
            scope=_scope(), action="resolve", topic="关切甲", note="甲完成", idempotency_hint="a2"
        )
        rows = {(row[1]): row[2] for row in await _concerns(gateway)}
        assert rows == {"关切甲": "resolved", "关切乙": "active"}, "推进甲不得抹掉乙"

    _run(tmp_path, body)


def test_concerns_are_scope_isolated(tmp_path):
    async def body(gateway):
        await gateway.transition_concern(
            scope=_scope(bot_id="bot-a"), action="note", topic="专属挂念", idempotency_hint="t1"
        )
        # 另一个 Bot 看不到，也不能推进它
        other = _scope(bot_id="bot-b")
        owned = await gateway.coordinator.read(
            lambda conn: conn.execute(
                "SELECT COUNT(*) FROM scoped_soul_concerns WHERE bot_id='bot-b'"
            ).fetchone()[0]
        )
        assert owned == 0
        with pytest.raises(CommandRejectedError) as exc:
            await gateway.transition_concern(
                scope=other, action="resolve", topic="专属挂念", note="越权", idempotency_hint="t2"
            )
        assert exc.value.code == "concern_not_found_in_scope"

    _run(tmp_path, body)


def test_private_scope_is_rejected_for_group_concerns(tmp_path):
    async def body(gateway):
        private = RuntimeScope(
            bot_id="bot-a",
            visibility="private",
            session=SessionRef("qq:private:u1", "qq", "private", "u1"),
            subject_principal_id="qq:user:u1",
        )
        with pytest.raises(ValueError):
            await gateway.transition_concern(
                scope=private, action="note", topic="不应写入", idempotency_hint="t1"
            )

    _run(tmp_path, body)
