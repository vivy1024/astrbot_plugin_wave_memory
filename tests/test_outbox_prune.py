"""outbox 历史清理：只删已归档且投递完成的旧事件，保留最新写序号。"""

from __future__ import annotations

import sqlite3
import time

from engine.db.outbox_repo import OutboxRepository


def _db(tmp_path):
    conn = sqlite3.connect(tmp_path / "o.db")
    conn.execute("PRAGMA foreign_keys=ON")
    OutboxRepository.migrate(conn)
    return conn


def _op(conn, seq, ts, *, archived=True, delivery="completed"):
    op = f"op{seq}"
    conn.execute(
        "INSERT INTO write_operations VALUES (?,?,?,?,?,?,?,?,?,?)",
        (op, f"key{seq}", "h", "cmd", "{}", "committed", "{}", seq, ts, ts),
    )
    conn.execute(
        "INSERT INTO domain_outbox VALUES (?,?,?,?,?,?,?,?,?,?)",
        (f"ev{seq}", op, "memory", str(seq), 1, "memory.created", 1, "{}", ts, ts if archived else None),
    )
    conn.execute(
        "INSERT INTO outbox_deliveries(event_id, consumer_name, state, available_at) VALUES (?,?,?,?)",
        (f"ev{seq}", "memory_index", delivery, ts),
    )


def test_prune_only_old_completed_history(tmp_path):
    conn = _db(tmp_path)
    old = time.time() - 60 * 86400
    _op(conn, 1, old)
    _op(conn, 2, old, delivery="pending")      # 还没投递完：保留
    _op(conn, 3, old, archived=False)          # 未归档：保留
    _op(conn, 4, time.time())                  # 新的：保留
    conn.commit()
    result = OutboxRepository.prune_history(conn, before=time.time() - 30 * 86400)
    conn.commit()
    assert result == {"events": 1, "deliveries": 1, "operations": 1, "projections": 0}
    assert [r[0] for r in conn.execute("SELECT event_id FROM domain_outbox ORDER BY 1")] == ["ev2", "ev3", "ev4"]
    assert OutboxRepository.next_write_sequence(conn) == 5


def test_latest_operation_is_never_deleted(tmp_path):
    conn = _db(tmp_path)
    old = time.time() - 90 * 86400
    _op(conn, 1, old)
    _op(conn, 2, old)
    conn.commit()
    OutboxRepository.prune_history(conn, before=time.time())
    conn.commit()
    assert conn.execute("SELECT COUNT(*) FROM domain_outbox").fetchone()[0] == 0
    assert [r[0] for r in conn.execute("SELECT write_sequence FROM write_operations")] == [2]
    assert OutboxRepository.next_write_sequence(conn) == 3


def _projection(conn, consumer, aggregate_id, updated_at):
    conn.execute(
        "INSERT INTO derived_projection_state VALUES (?,?,?,?,?,?,?)",
        (consumer, "memory", aggregate_id, 1, 1, "{}", updated_at),
    )


def test_old_projection_state_is_pruned_unless_aggregate_still_pending(tmp_path):
    conn = _db(tmp_path)
    old = time.time() - 60 * 86400
    _op(conn, 1, old, delivery="pending")      # 聚合 "1" 在 memory_index 上还有未完成投递
    _projection(conn, "memory_index", "1", old)  # 保留：乱序保护还要用
    _projection(conn, "tag_index", "1", old)     # 另一个消费者没有未完成投递：删
    _projection(conn, "memory_index", "2", old)  # 删
    _projection(conn, "memory_index", "3", time.time())  # 新的：保留
    conn.commit()
    result = OutboxRepository.prune_history(conn, before=time.time() - 30 * 86400)
    conn.commit()
    assert result["projections"] == 2
    assert sorted(conn.execute("SELECT consumer_name, aggregate_id FROM derived_projection_state")) == [
        ("memory_index", "1"), ("memory_index", "3"),
    ]
