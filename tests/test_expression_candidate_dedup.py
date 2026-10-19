"""表达候选内容级去重的迁移：加列、来源表、触发器与回填。"""

from __future__ import annotations

import copy
import sqlite3

import pytest

from engine.db.migrations.expression_candidate_dedup import (
    ensure_expression_candidate_dedup_schema,
    merge_source,
    primary_artifact,
    source_rows,
)
from engine.db.migrations.reply_feedback_learning import ensure_reply_feedback_learning_schema


def _store() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    ensure_reply_feedback_learning_schema(conn)
    return conn


def _add_candidate(conn, ident, content, *, speaker="u1", source_id=None, session="qq:group:1", bot="bot-a"):
    conn.execute(
        """INSERT INTO reply_expression_candidates
           (id,bot_id,session_id,visibility,speaker_id,source_memory_id,content,situation,
            source_hash,artifact_json,created_at)
           VALUES (?,?,?,'group',?,?,?,'humor','sha256:x','{"id":"memory:1"}',1.0)""",
        (ident, bot, session, speaker, source_id if source_id is not None else ident, content),
    )
    conn.commit()


def test_migration_is_additive_and_idempotent():
    conn = _store()
    _add_candidate(conn, 1, "难绷")
    before = conn.execute("SELECT * FROM reply_expression_candidates").fetchall()
    ensure_expression_candidate_dedup_schema(conn)
    ensure_expression_candidate_dedup_schema(conn)
    after = conn.execute("SELECT * FROM reply_expression_candidates").fetchall()
    assert len(before) == len(after) == 1
    # 既有行的数据不动，只多出两列
    assert after[0][:len(before[0])] == before[0]
    assert conn.execute("SELECT occurrence_count,speaker_count FROM reply_expression_candidates").fetchone() == (1, 1)


def test_backfill_creates_one_source_row_per_existing_candidate():
    conn = _store()
    _add_candidate(conn, 7, "有一说一")
    stats = ensure_expression_candidate_dedup_schema(conn)
    assert stats["sources_backfilled"] == 1
    rows = source_rows(conn, 7)
    assert len(rows) == 1
    assert rows[0]["source_memory_id"] == 7
    assert rows[0]["speaker_id"] == "u1"
    # 再跑一次不重复回填
    assert ensure_expression_candidate_dedup_schema(conn)["sources_backfilled"] == 0
    assert len(source_rows(conn, 7)) == 1


def test_trigger_keeps_counts_in_sync_without_app_layer_update():
    """计数由触发器维护：应用层只插来源行，计数列自动跟上（并发下不会丢更新）。"""
    conn = _store()
    _add_candidate(conn, 1, "何意味", speaker="u1")
    ensure_expression_candidate_dedup_schema(conn)
    assert merge_source(conn, example_id=1, bot_id="bot-a", session_id="qq:group:1", visibility="group",
                        source_memory_id=2, speaker_id="u2", situation="humor",
                        artifact_json='{"id":"memory:2"}', created_at=2.0) is True
    conn.commit()
    assert conn.execute("SELECT occurrence_count,speaker_count FROM reply_expression_candidates WHERE id=1").fetchone() == (2, 2)
    # 同一来源重复合并是幂等的
    assert merge_source(conn, example_id=1, bot_id="bot-a", session_id="qq:group:1", visibility="group",
                        source_memory_id=2, speaker_id="u2", situation="humor",
                        artifact_json='{"id":"memory:2"}', created_at=2.0) is False
    assert conn.execute("SELECT occurrence_count,speaker_count FROM reply_expression_candidates WHERE id=1").fetchone() == (2, 2)
    # 同一个人说第三次：次数涨，人数不涨
    merge_source(conn, example_id=1, bot_id="bot-a", session_id="qq:group:1", visibility="group",
                 source_memory_id=3, speaker_id="u2", situation="humor",
                 artifact_json='{"id":"memory:3"}', created_at=3.0)
    conn.commit()
    assert conn.execute("SELECT occurrence_count,speaker_count FROM reply_expression_candidates WHERE id=1").fetchone() == (3, 2)


def test_delete_of_source_lowers_counts():
    conn = _store()
    _add_candidate(conn, 1, "难绷")
    ensure_expression_candidate_dedup_schema(conn)
    merge_source(conn, example_id=1, bot_id="bot-a", session_id="qq:group:1", visibility="group",
                 source_memory_id=2, speaker_id="u2", situation="humor",
                 artifact_json='{"id":"memory:2"}', created_at=2.0)
    conn.commit()
    assert conn.execute("SELECT occurrence_count FROM reply_expression_candidates WHERE id=1").fetchone() == (2,)
    conn.execute("DELETE FROM reply_expression_candidate_sources WHERE example_id=1 AND source_memory_id=2")
    conn.commit()
    assert conn.execute("SELECT occurrence_count,speaker_count FROM reply_expression_candidates WHERE id=1").fetchone() == (1, 1)


def test_merge_source_rejects_unknown_candidate_via_foreign_key():
    conn = _store()
    conn.execute("PRAGMA foreign_keys=ON")
    _add_candidate(conn, 1, "难绷")
    ensure_expression_candidate_dedup_schema(conn)
    with pytest.raises(sqlite3.IntegrityError):
        merge_source(conn, example_id=999, bot_id="bot-a", session_id="qq:group:1", visibility="group",
                     source_memory_id=1, speaker_id="u1", situation="humor",
                     artifact_json="{}", created_at=1.0)


def test_primary_artifact_returns_first_source_artifact():
    conn = _store()
    _add_candidate(conn, 1, "难绷")
    ensure_expression_candidate_dedup_schema(conn)
    merge_source(conn, example_id=1, bot_id="bot-a", session_id="qq:group:1", visibility="group",
                 source_memory_id=2, speaker_id="u2", situation="humor",
                 artifact_json='{"id":"memory:2"}', created_at=2.0)
    conn.commit()
    assert primary_artifact(conn, 1) == '{"id":"memory:1"}'
    assert primary_artifact(conn, 999) is None


def test_backfill_is_consistent_with_existing_counts():
    """回填后计数必须与来源行一致，不能把老库的默认 1 留下当真相。"""
    conn = _store()
    _add_candidate(conn, 1, "难绷")
    _add_candidate(conn, 2, "有一说一")
    stats = ensure_expression_candidate_dedup_schema(conn)
    assert stats["sources_backfilled"] == 2
    counts = conn.execute("SELECT id,occurrence_count,speaker_count FROM reply_expression_candidates ORDER BY id").fetchall()
    assert counts == [(1, 1, 1), (2, 1, 1)]
    for ident, _, _ in counts:
        assert len(source_rows(conn, ident)) == 1


def test_column_added_to_legacy_table_without_counts():
    """老库缺列时必须补上，且既有行拿到默认计数 1（不是 NULL）。"""
    conn = sqlite3.connect(":memory:")
    conn.execute("""CREATE TABLE reply_expression_candidates (
        id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, session_id TEXT NOT NULL,
        visibility TEXT NOT NULL CHECK(visibility='group'), speaker_id TEXT NOT NULL,
        source_memory_id INTEGER NOT NULL, content TEXT NOT NULL, situation TEXT NOT NULL,
        source_hash TEXT NOT NULL, artifact_json TEXT NOT NULL,
        evidence_refs_json TEXT NOT NULL DEFAULT '[]', evidence_bindings_json TEXT NOT NULL DEFAULT '[]',
        status TEXT NOT NULL DEFAULT 'pending', revision INTEGER NOT NULL DEFAULT 1,
        reviewer TEXT NOT NULL DEFAULT '', review_note TEXT NOT NULL DEFAULT '', reviewed_at REAL,
        created_at REAL NOT NULL,
        UNIQUE(bot_id, session_id, visibility, source_memory_id))""")
    _add_candidate(conn, 1, "难绷")
    ensure_expression_candidate_dedup_schema(conn)
    assert conn.execute("SELECT occurrence_count,speaker_count FROM reply_expression_candidates").fetchone() == (1, 1)


# ---- 重复候选归并 -----------------------------------------------------------


def test_duplicates_are_merged_into_newest_row():
    """同群同内容的重复候选：来源归并到最新一行，旧行被吸收而不是被否决。"""
    conn = _store()
    _add_candidate(conn, 1, "难绷", speaker="u1", source_id=1)
    _add_candidate(conn, 2, "难绷", speaker="u2", source_id=2)
    _add_candidate(conn, 3, "有一说一", speaker="u1", source_id=3)
    stats = ensure_expression_candidate_dedup_schema(conn)
    assert stats["absorbed"] == 1
    assert stats["moved_sources"] == 1
    rows = conn.execute(
        "SELECT id,status,occurrence_count,speaker_count,review_note FROM reply_expression_candidates ORDER BY id"
    ).fetchall()
    assert rows[0][0] == 1 and rows[0][1] == "rejected"
    assert rows[0][4] == "content_duplicate_merged:superseded_by=2"
    assert rows[1] == (2, "pending", 2, 2, "")
    assert rows[2] == (3, "pending", 1, 1, "")
    # 保留行拿到了两条来源，且没有残留的来源行挂在被吸收的行上
    # 顺序是插入顺序（先回填保留行自己的来源，再并入旧行的来源）
    assert sorted(r["source_memory_id"] for r in source_rows(conn, 2)) == [1, 2]
    assert source_rows(conn, 1) == []


def test_merge_does_not_write_review_audit():
    """去重不是人工审核，不得在 reviews 表里冒充一次否决。"""
    conn = _store()
    _add_candidate(conn, 1, "何意味", speaker="u1", source_id=1)
    _add_candidate(conn, 2, "何意味", speaker="u2", source_id=2)
    ensure_expression_candidate_dedup_schema(conn)
    assert conn.execute("SELECT COUNT(*) FROM reply_expression_reviews").fetchone() == (0,)


def test_absorbed_row_rejects_stale_revision_approval():
    """被吸收的行 revision 已 +1：拿着旧 revision 点批准必须失败，不能静默通过。"""
    conn = _store()
    _add_candidate(conn, 1, "真的假的", speaker="u1", source_id=1)
    _add_candidate(conn, 2, "真的假的", speaker="u2", source_id=2)
    ensure_expression_candidate_dedup_schema(conn)
    revision = conn.execute("SELECT revision FROM reply_expression_candidates WHERE id=1").fetchone()[0]
    assert revision == 2


def test_dedupe_does_not_cross_groups_or_bots():
    """不同群 / 不同 Bot 的同一句话不合并：否则一个群的证据会去背书另一个群的召回。"""
    conn = _store()
    _add_candidate(conn, 1, "难绷", session="qq:group:1")
    _add_candidate(conn, 2, "难绷", session="qq:group:2")
    _add_candidate(conn, 3, "难绷", bot="bot-b")
    stats = ensure_expression_candidate_dedup_schema(conn)
    assert stats["absorbed"] == 0
    assert conn.execute("SELECT COUNT(*) FROM reply_expression_candidates WHERE status='pending'").fetchone() == (3,)


def test_dedupe_is_idempotent():
    conn = _store()
    _add_candidate(conn, 1, "难绷", speaker="u1", source_id=1)
    _add_candidate(conn, 2, "难绷", speaker="u2", source_id=2)
    first = ensure_expression_candidate_dedup_schema(conn)
    second = ensure_expression_candidate_dedup_schema(conn)
    assert first["absorbed"] == 1
    assert second["absorbed"] == 0 and second["moved_sources"] == 0
    assert source_rows(conn, 2) and len(source_rows(conn, 2)) == 2


def test_dedupe_ignores_already_reviewed_rows():
    """已被人审过的行不参与归并，避免覆盖人工结论。"""
    conn = _store()
    _add_candidate(conn, 1, "有一说一", speaker="u1", source_id=1)
    _add_candidate(conn, 2, "有一说一", speaker="u2", source_id=2)
    conn.execute("UPDATE reply_expression_candidates SET status='approved' WHERE id=1")
    conn.commit()
    stats = ensure_expression_candidate_dedup_schema(conn)
    assert stats["absorbed"] == 0
    assert conn.execute("SELECT status FROM reply_expression_candidates WHERE id=1").fetchone() == ("approved",)


def test_all_sources_keep_evidence_reachable():
    """归并后保留行的来源里必须有本群的来源消息，召回才拿得到本群证据。"""
    conn = _store()
    _add_candidate(conn, 1, "我去,不早说", speaker="u1", source_id=11)
    _add_candidate(conn, 2, "我去,不早说", speaker="u2", source_id=12)
    _add_candidate(conn, 3, "我去,不早说", speaker="u3", source_id=13)
    ensure_expression_candidate_dedup_schema(conn)
    kept = source_rows(conn, 3)
    assert sorted(row["source_memory_id"] for row in kept) == [11, 12, 13]
    assert {row["speaker_id"] for row in kept} == {"u1", "u2", "u3"}
    assert conn.execute(
        "SELECT occurrence_count,speaker_count FROM reply_expression_candidates WHERE id=3"
    ).fetchone() == (3, 3)


# ---- 频次门槛与列表 ---------------------------------------------------------


def _seed_two_speakers(conn):
    _add_candidate(conn, 1, "有一说一", speaker="u1", source_id=1)
    _add_candidate(conn, 2, "有一说一", speaker="u2", source_id=2)
    _add_candidate(conn, 3, "阎王夸你好身体", speaker="u1", source_id=3)
    ensure_expression_candidate_dedup_schema(conn)


def test_qualified_flag_marks_cross_speaker_expressions():
    from services.expression_policy import ExpressionPolicy
    conn = _store()
    _seed_two_speakers(conn)
    from domain.scope import RuntimeScope, SessionRef
    scope = RuntimeScope("bot-a", "group", SessionRef("qq:group:1", "qq", "group", "1"))
    policy = ExpressionPolicy(conn, enabled=True)
    items, total = policy.list_candidates(scope, qualified=True)
    assert total == 1
    assert items[0]["content"] == "有一说一"
    assert items[0]["qualified"] is True
    assert items[0]["occurrence_count"] == 2 and items[0]["speaker_count"] == 2
    # 不带门槛时两条都在，且按频次倒序
    items, total = policy.list_candidates(scope)
    assert total == 2
    assert [item["qualified"] for item in items] == [True, False]


def test_threshold_only_filters_the_review_list_not_the_store():
    """门槛只决定「先看哪些」，未达标的候选必须留在库里。"""
    from services.expression_policy import ExpressionPolicy
    conn = _store()
    _seed_two_speakers(conn)
    from domain.scope import RuntimeScope, SessionRef
    scope = RuntimeScope("bot-a", "group", SessionRef("qq:group:1", "qq", "group", "1"))
    policy = ExpressionPolicy(conn, enabled=True)
    assert conn.execute("SELECT COUNT(*) FROM reply_expression_candidates WHERE status='pending'").fetchone() == (2,)
    policy.list_candidates(scope, qualified=True)
    assert conn.execute("SELECT COUNT(*) FROM reply_expression_candidates WHERE status='pending'").fetchone() == (2,)


def test_single_speaker_twice_is_not_qualified():
    """同一个人说两次还不算本群表达：要么跨两个人，要么同人说三次。"""
    from services.expression_policy import ExpressionPolicy
    conn = _store()
    _add_candidate(conn, 1, "宁可少发,也不能乱发", speaker="u1", source_id=1)
    _add_candidate(conn, 2, "宁可少发,也不能乱发", speaker="u1", source_id=2)
    ensure_expression_candidate_dedup_schema(conn)
    from domain.scope import RuntimeScope, SessionRef
    scope = RuntimeScope("bot-a", "group", SessionRef("qq:group:1", "qq", "group", "1"))
    policy = ExpressionPolicy(conn, enabled=True)
    items, total = policy.list_candidates(scope, qualified=True)
    assert total == 0 and items == []
    assert conn.execute("SELECT occurrence_count,speaker_count FROM reply_expression_candidates WHERE id=2").fetchone() == (2, 1)


def test_observe_merges_same_content_into_one_candidate():
    """采集时同群同内容不再新建候选行，只把来源并进去。"""
    from services.expression_policy import ExpressionPolicy
    from services.reply_feedback import ReplyFeedbackService
    from tests.test_reply_feedback_learning import payload, scope as scope_of
    import tests.test_reply_feedback_learning as helper
    conn = _store()
    conn.execute("""CREATE TABLE memories (
        id INTEGER PRIMARY KEY,bot_id TEXT,session_id TEXT,visibility TEXT,sender_id TEXT,sender_name TEXT,
        content TEXT,timestamp REAL,quarantine INTEGER,resolution_state TEXT,memory_type TEXT,importance REAL)""")
    conn.commit()
    scope = scope_of()
    for ident, speaker in ((1, "visitor-a"), (2, "bot"), (3, "visitor-b")):
        helper.add_message(conn, ident, {"visitor-a": "慢慢来，一步一步走", "bot": "可以先做一小步", "visitor-b": "慢慢来，一步一步走"}[speaker], speaker=speaker, owner=scope)
    policy = ExpressionPolicy(conn, enabled=True)
    feedback = ReplyFeedbackService(conn, enabled=False, expression_policy=policy)
    data = payload()
    feedback.record_bookkeeping(scope, data, source_message_ids=(1, 2, 3))
    assert conn.execute("SELECT COUNT(*) FROM reply_expression_candidates").fetchone() == (1,)
    # 同一句被第二个人说过：并进同一条候选，人数 +1
    data2 = copy.deepcopy(payload())
    data2["expressions"][0]["source_memory_id"] = 3
    data2["expressions"][0]["speaker_id"] = "visitor-b"
    feedback.record_bookkeeping(scope, data2, source_message_ids=(3,))
    assert conn.execute("SELECT COUNT(*) FROM reply_expression_candidates").fetchone() == (1,)
