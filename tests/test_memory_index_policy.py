from __future__ import annotations

import sqlite3
import struct

from services.memory_index_policy import (
    MemoryIndexPolicy,
    decode_vector,
    evaluate_memory_admission,
    memory_index_policy_from_settings,
    select_hot_memory_candidates,
)


NOW = 1_800_000_000.0
DIMENSION = 3


def _vector(*values: float) -> bytes:
    return struct.pack(f"{len(values)}f", *values)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            vector BLOB,
            bot_id TEXT,
            session_id TEXT,
            visibility TEXT,
            group_id TEXT,
            resolution_state TEXT,
            quarantine INTEGER,
            source TEXT,
            memory_type TEXT,
            importance REAL,
            access_count INTEGER,
            timestamp REAL
        );
        CREATE TABLE scoped_tags (
            id INTEGER PRIMARY KEY,
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            name TEXT NOT NULL,
            status TEXT DEFAULT 'active'
        );
        CREATE TABLE scoped_memory_tags (
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            memory_id INTEGER NOT NULL,
            tag_id INTEGER NOT NULL,
            position INTEGER DEFAULT 0,
            relevance REAL DEFAULT 1.0
        );
        """
    )
    return connection


def _insert_memory(
    connection: sqlite3.Connection,
    memory_id: int,
    *,
    group_id: str = "g1",
    bot_id: str = "bot-a",
    session_id: str | None = None,
    visibility: str = "group",
    resolution_state: str = "resolved",
    quarantine: int = 0,
    source: str = "chat",
    memory_type: str = "message",
    importance: float = 1.0,
    access_count: int = 0,
    timestamp: float = NOW,
    vector: bytes | None = None,
) -> None:
    connection.execute(
        """INSERT INTO memories(
               id, vector, bot_id, session_id, visibility, group_id, resolution_state,
               quarantine, source, memory_type, importance, access_count, timestamp)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            memory_id, vector if vector is not None else _vector(0.1, 0.2, 0.3),
            bot_id, session_id or f"qq:group:{group_id}", visibility, group_id,
            resolution_state, quarantine, source, memory_type, importance, access_count,
            timestamp,
        ),
    )


def _tag(connection: sqlite3.Connection, memory_id: int, *, group_id: str = "g1", tag_id: int | None = None,
         relevance: float = 1.0, session_id: str | None = None) -> None:
    tag_id = memory_id if tag_id is None else tag_id
    session_id = session_id or f"qq:group:{group_id}"
    connection.execute(
        "INSERT INTO scoped_tags(id, bot_id, session_id, visibility, name) VALUES (?, 'bot-a', ?, 'group', ?)",
        (tag_id, session_id, f"tag-{tag_id}"),
    )
    connection.execute(
        """INSERT INTO scoped_memory_tags(bot_id, session_id, visibility, memory_id, tag_id, position, relevance)
           VALUES ('bot-a', ?, 'group', ?, ?, 1, ?)""",
        (session_id, memory_id, tag_id, relevance),
    )


def _ids(connection: sqlite3.Connection, policy: MemoryIndexPolicy = MemoryIndexPolicy()) -> list[int]:
    return [candidate.memory_id for candidate in select_hot_memory_candidates(
        connection, policy, DIMENSION, now=NOW,
    )]


def test_effective_tag_fallback_admits_tagged_memory_without_materialized_projection():
    connection = _connection()
    try:
        _insert_memory(connection, 1)
        _tag(connection, 1, relevance=0.7)

        # The materialized effective-tag table intentionally does not exist.  The
        # policy must read the automatic scoped baseline through effective_tag_rows.
        candidates = select_hot_memory_candidates(connection, MemoryIndexPolicy(), DIMENSION, now=NOW)

        assert [candidate.memory_id for candidate in candidates] == [1]
        assert candidates[0].tag_count == 1
        assert candidates[0].tag_relevance == 0.7
        assert decode_vector(_vector(1.0, 2.0, 3.0), DIMENSION).tolist() == [1.0, 2.0, 3.0]
        assert decode_vector(_vector(1.0, 2.0), DIMENSION) is None
    finally:
        connection.close()


def test_private_active_vector_is_hot_without_tags_and_private_legacy_is_not_group_legacy():
    connection = _connection()
    try:
        _insert_memory(
            connection,
            1,
            group_id="user:user-1",
            session_id="qq:private:user:user-1",
            visibility="private",
            timestamp=NOW - 365 * 86_400,
            importance=2.0,
        )
        _insert_memory(
            connection,
            2,
            group_id="private:user:user-2",
            bot_id="",
            session_id="",
            visibility="",
        )
        connection.execute(
            "UPDATE memories SET bot_id='', session_id='', visibility='' WHERE id=2"
        )
        connection.commit()
        candidates = select_hot_memory_candidates(
            connection, MemoryIndexPolicy(max_vectors=10), DIMENSION, now=NOW
        )
        assert [candidate.memory_id for candidate in candidates] == [1]
        assert candidates[0].visibility == "private"
        assert candidates[0].tag_count == 0
        assert candidates[0].scope_key == ("bot-a", "qq:private:user:user-1", "private", "user:user-1")
    finally:
        connection.close()


def test_scope_noise_and_inactive_rows_are_excluded():
    connection = _connection()
    try:
        _insert_memory(connection, 1)
        _tag(connection, 1)
        _insert_memory(connection, 2, session_id="qq:group:other")  # group/session mismatch
        _tag(connection, 2, session_id="qq:group:other")
        _insert_memory(connection, 3, source="noise")
        _tag(connection, 3)
        _insert_memory(connection, 4, resolution_state="pending")
        _tag(connection, 4)
        _insert_memory(connection, 5, quarantine=1)
        _tag(connection, 5)
        _insert_memory(connection, 6, memory_type="archived")
        _tag(connection, 6)
        _insert_memory(connection, 7, memory_type="deleted")
        _tag(connection, 7)
        _insert_memory(connection, 8, vector=_vector(1.0, 2.0))
        _tag(connection, 8)

        assert _ids(connection) == [1]
    finally:
        connection.close()


def test_durable_sources_and_types_outrank_expired_core_chat():
    """chat_hot_days is a score half-life now: stale chat stays eligible but
    ranks strictly below durable knowledge instead of being hard-excluded."""
    from domain import bot_identity

    connection = _connection()
    # 第一人称经历来源不再写死：由 Bot 资料的 persona.experience_source 登记（这里模拟某个 Bot 登记了它）
    bot_identity.publish(experience_sources=["demo_experience"])
    try:
        expired = NOW - 31 * 86_400
        _insert_memory(connection, 1, source="core", timestamp=expired)
        _tag(connection, 1)
        _insert_memory(connection, 2, source="demo_experience", timestamp=expired)
        _tag(connection, 2)
        _insert_memory(connection, 3, source="core", memory_type="knowledge", timestamp=expired)
        _tag(connection, 3)
        _insert_memory(connection, 4, source="book_lore", timestamp=expired)
        # Durable rows still require a tag.

        candidates = select_hot_memory_candidates(connection, MemoryIndexPolicy(chat_hot_days=30), DIMENSION, now=NOW)

        by_id = {candidate.memory_id: candidate for candidate in candidates}
        assert set(by_id) == {1, 2, 3}
        assert by_id[2].durable and by_id[3].durable
        assert not by_id[1].durable
        # Decayed non-durable chat must rank strictly below both durable rows.
        assert by_id[1].score < min(by_id[2].score, by_id[3].score)
        # 31 days at a 30-day half-life leaves less than 55% of the base score.
        fresh = _score_like(by_id[1], now=NOW, timestamp=NOW)
        assert by_id[1].score < fresh * 0.55
    finally:
        bot_identity.publish()
        connection.close()


def _score_like(candidate, *, now, timestamp):
    from services.memory_index_policy import _score

    return _score(
        durable=candidate.durable,
        importance=1.0,
        tag_relevance=candidate.tag_relevance,
        tag_count=candidate.tag_count,
        access_count=0.0,
        timestamp=timestamp,
        now=now,
        stale_decay_days=0,
    )


def test_default_policy_admits_one_scope_until_global_capacity():
    connection = _connection()
    try:
        for memory_id, importance in ((1, 5.0), (2, 4.0), (3, 3.0)):
            _insert_memory(connection, memory_id, importance=importance)
            _tag(connection, memory_id)
        for memory_id, importance in ((4, 2.0), (5, 1.0)):
            _insert_memory(connection, memory_id, group_id="g2", importance=importance)
            _tag(connection, memory_id, group_id="g2")

        policy = MemoryIndexPolicy(max_vectors=4, per_scope_max_vectors=1, candidate_limit=128)

        assert _ids(connection, policy) == [1, 2, 3, 4]
        assert evaluate_memory_admission(connection, 3, policy, DIMENSION, now=NOW).memory_id == 3
    finally:
        connection.close()


def test_opt_in_scope_quota_then_global_quota_and_single_memory_evaluation():
    connection = _connection()
    try:
        for memory_id, importance in ((1, 5.0), (2, 4.0), (3, 3.0)):
            _insert_memory(connection, memory_id, importance=importance)
            _tag(connection, memory_id)
        for memory_id, importance in ((4, 2.0), (5, 1.0)):
            _insert_memory(connection, memory_id, group_id="g2", importance=importance)
            _tag(connection, memory_id, group_id="g2")

        policy = MemoryIndexPolicy(
            max_vectors=3,
            per_scope_max_vectors=2,
            enforce_scope_hot_quota=True,
            candidate_limit=128,
        )

        assert _ids(connection, policy) == [1, 2, 4]
        assert evaluate_memory_admission(connection, 2, policy, DIMENSION, now=NOW).memory_id == 2
        assert evaluate_memory_admission(connection, 3, policy, DIMENSION, now=NOW) is None
        assert evaluate_memory_admission(connection, 5, policy, DIMENSION, now=NOW) is None
    finally:
        connection.close()


def test_scope_hot_quota_configuration_is_explicitly_opt_in():
    default = memory_index_policy_from_settings({"per_scope_max_vectors": 1})
    explicit_false = memory_index_policy_from_settings({"enforce_scope_hot_quota": False})
    disabled = memory_index_policy_from_settings({"enforce_scope_hot_quota": "false"})
    enabled = memory_index_policy_from_settings({
        "enforce_scope_hot_quota": "true",
        "per_scope_max_vectors": "7",
    })

    assert default.enforce_scope_hot_quota is False
    assert explicit_false.enforce_scope_hot_quota is False
    assert disabled.enforce_scope_hot_quota is False
    assert enabled.enforce_scope_hot_quota is True
    assert enabled.per_scope_max_vectors == 7


def test_cold_candidate_limit_does_not_truncate_hot_rebuild_selection():
    connection = _connection()
    try:
        for memory_id in (1, 2):
            _insert_memory(connection, memory_id)
            _tag(connection, memory_id)

        # This bound belongs to request-time cold reranking, not to the hot
        # rebuild.  A historical bug made it silently cap HNSW at 128 rows.
        policy = MemoryIndexPolicy(max_vectors=200, per_scope_max_vectors=200, candidate_limit=1)

        assert _ids(connection, policy) == [1, 2]
    finally:
        connection.close()


def _legacy_tag_schema(connection: sqlite3.Connection, *, relevance: bool = True) -> None:
    connection.execute("CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, vector BLOB)")
    relevance_column = ", relevance REAL" if relevance else ""
    connection.execute(
        "CREATE TABLE memory_tags (memory_id INTEGER NOT NULL, tag_id INTEGER NOT NULL" + relevance_column + ")"
    )


def _legacy_tag(connection: sqlite3.Connection, memory_id: int, tag_id: int, *, relevance: float = 1.0) -> None:
    connection.execute(
        "INSERT INTO tags (id, name, vector) VALUES (?, ?, ?)",
        (tag_id, f"legacy-{tag_id}", _vector(0.1, 0.2, 0.3)),
    )
    connection.execute(
        "INSERT INTO memory_tags (memory_id, tag_id, relevance) VALUES (?, ?, ?)",
        (memory_id, tag_id, relevance),
    )


def test_legacy_schema_rows_with_legacy_tags_are_admitted_without_fabricated_scope():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY, vector BLOB, group_id TEXT, source TEXT,
                memory_type TEXT, importance REAL, access_count INTEGER, timestamp REAL
            );
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, vector BLOB);
            CREATE TABLE memory_tags (memory_id INTEGER NOT NULL, tag_id INTEGER NOT NULL);
            """
        )
        # Active legacy message (not evicted): hot index must match SQL read filters.
        connection.execute(
            "INSERT INTO memories VALUES (1, ?, 'g1', 'chat', 'message', 1.0, 0, ?)",
            (_vector(0.1, 0.2, 0.3), NOW - 365 * 86400),
        )
        connection.execute("INSERT INTO tags VALUES (11, '历史标签', ?)", (_vector(0.1, 0.2, 0.3),))
        connection.execute("INSERT INTO memory_tags VALUES (1, 11)")

        candidates = select_hot_memory_candidates(
            connection,
            MemoryIndexPolicy(max_vectors=10, per_scope_max_vectors=1),
            DIMENSION,
            now=NOW,
        )

        assert [candidate.memory_id for candidate in candidates] == [1]
        assert candidates[0].recall_visibility == "legacy_group"
        assert candidates[0].scope_key is None
        assert candidates[0].tag_relevance == 1.0
    finally:
        connection.close()


def test_legacy_lane_excludes_evicted_like_sql_read_path():
    connection = sqlite3.connect(":memory:")
    try:
        connection.executescript(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY, vector BLOB, group_id TEXT, source TEXT,
                memory_type TEXT, importance REAL, access_count INTEGER, timestamp REAL
            );
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT, vector BLOB);
            CREATE TABLE memory_tags (memory_id INTEGER NOT NULL, tag_id INTEGER NOT NULL);
            """
        )
        connection.execute(
            "INSERT INTO memories VALUES (1, ?, 'g1', 'chat', 'evicted', 1.0, 0, ?)",
            (_vector(0.1, 0.2, 0.3), NOW - 10 * 86400),
        )
        connection.execute("INSERT INTO tags VALUES (11, '历史标签', ?)", (_vector(0.1, 0.2, 0.3),))
        connection.execute("INSERT INTO memory_tags VALUES (1, 11)")
        candidates = select_hot_memory_candidates(
            connection,
            MemoryIndexPolicy(max_vectors=10),
            DIMENSION,
            now=NOW,
        )
        assert candidates == []
    finally:
        connection.close()


def test_legacy_lane_excludes_bad_states_and_partial_modern_scope():
    connection = _connection()
    try:
        _legacy_tag_schema(connection)
        for memory_id in range(1, 6):
            _insert_memory(connection, memory_id, importance=10 - memory_id)
            connection.execute(
                "UPDATE memories SET bot_id='', session_id='', visibility='' WHERE id=?",
                (memory_id,),
            )
            _legacy_tag(connection, memory_id, 100 + memory_id)
        connection.execute("UPDATE memories SET source='noise' WHERE id=2")
        connection.execute("UPDATE memories SET resolution_state='pending' WHERE id=3")
        connection.execute("UPDATE memories SET quarantine=1 WHERE id=4")
        connection.execute("UPDATE memories SET bot_id='partial' WHERE id=5")

        assert _ids(connection) == [1]
    finally:
        connection.close()


def test_scoped_reservation_protects_formal_lane_while_legacy_bypasses_scope_quota():
    connection = _connection()
    try:
        _legacy_tag_schema(connection)
        _insert_memory(connection, 1, importance=0.1)
        _tag(connection, 1)
        for memory_id in (2, 3, 4):
            _insert_memory(connection, memory_id, importance=float(10 - memory_id))
            connection.execute(
                "UPDATE memories SET bot_id='', session_id='', visibility='' WHERE id=?",
                (memory_id,),
            )
            _legacy_tag(connection, memory_id, 100 + memory_id)

        reserved = MemoryIndexPolicy(
            max_vectors=4,
            per_scope_max_vectors=1,
            scoped_reserved_vectors=1,
        )
        assert set(_ids(connection, reserved)) == {1, 2, 3, 4}

        no_reservation = MemoryIndexPolicy(
            max_vectors=2,
            per_scope_max_vectors=1,
            scoped_reserved_vectors=0,
        )
        assert _ids(connection, no_reservation) == [2, 3]
    finally:
        connection.close()
