"""共现图：落盘快照、子进程构建与线程构建一致、写锁重试。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import types
from types import SimpleNamespace

import pytest

if "astrbot.api" not in sys.modules:
    api = types.ModuleType("astrbot.api")
    api.logger = SimpleNamespace(
        info=lambda *args, **kwargs: None,
        warning=lambda *args, **kwargs: None,
        debug=lambda *args, **kwargs: None,
        error=lambda *args, **kwargs: None,
    )
    astrbot = types.ModuleType("astrbot")
    astrbot.api = api
    sys.modules["astrbot"] = astrbot
    sys.modules["astrbot.api"] = api

from engine.database import WaveMemoryDB
from engine.directed_cooccurrence import CooccurrenceScheduler, DirectedCooccurrence
from engine.write_coordinator import WriteCoordinator
from services.pair_similarity import PairSimilarityService


def _seed(db: WaveMemoryDB) -> None:
    """两个会话、若干多标签记忆，外加语义相似度表。"""
    conn = db.conn
    for session in ("qq:group:g1", "qq:group:g2"):
        for name in ("缺氧", "电力", "氧气", "羽书", "直播", "种田"):
            conn.execute(
                "INSERT INTO scoped_tags(bot_id, session_id, visibility, name, tag_type, created_at, updated_at) "
                "VALUES ('yushu', ?, 'group', ?, 'topic', 1.0, 1.0)",
                (session, name),
            )
    conn.commit()
    tags = {
        (session, name): tag_id
        for tag_id, session, name in conn.execute("SELECT id, session_id, name FROM scoped_tags").fetchall()
    }
    memories = [
        ("qq:group:g1", ["缺氧", "电力", "氧气"]),
        ("qq:group:g1", ["缺氧", "电力"]),
        ("qq:group:g1", ["羽书", "直播", "缺氧"]),
        ("qq:group:g2", ["种田", "羽书"]),
        ("qq:group:g2", ["直播", "羽书", "种田", "电力"]),
    ]
    for memory_id, (session, names) in enumerate(memories, 1):
        conn.execute(
            "INSERT INTO memories(id, group_id, content, timestamp, bot_id, session_id, visibility) "
            "VALUES (?, 'g', '一条有标签的记忆', 1.0, 'yushu', ?, 'group')",
            (memory_id, session),
        )
        for position, name in enumerate(names, 1):
            conn.execute(
                "INSERT INTO scoped_memory_tags(bot_id, session_id, visibility, memory_id, tag_id, position, relevance, created_at) "
                "VALUES ('yushu', ?, 'group', ?, ?, ?, 1.0, 1.0)",
                (session, memory_id, tags[(session, name)], position),
            )
    a, b = sorted((tags[("qq:group:g1", "缺氧")], tags[("qq:group:g1", "电力")]))
    conn.execute("INSERT INTO tag_pair_similarity(tag_id_a, tag_id_b, similarity) VALUES (?, ?, 0.5)", (a, b))
    conn.commit()


@pytest.fixture()
def seeded_db(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "w.db"), dimension=4)
    _seed(db)
    yield db
    db.close()


def _matrix(db, **kwargs):
    return DirectedCooccurrence(
        db,
        pair_sim_service=PairSimilarityService(db),
        residual_map={1: 0.9, 3: 0.1},
        max_neighbors_per_tag=kwargs.get("bound", 64),
    )


def test_subprocess_build_matches_in_thread_rebuild(seeded_db, monkeypatch):
    expected = _matrix(seeded_db)
    expected.rebuild()
    assert expected.forward

    def _no_in_process_rebuild(self):
        raise AssertionError("应在子进程里构建，不应退回线程")

    monkeypatch.setattr(DirectedCooccurrence, "rebuild", _no_in_process_rebuild)

    live = _matrix(seeded_db)
    scheduler = CooccurrenceScheduler(live, build_in_subprocess=True)
    request = scheduler._subprocess_request()
    assert request is not None and request["pair_similarity"] is True
    asyncio.run(scheduler.force_rebuild(reason="test"))

    assert _same_graph(live.forward, expected.forward)
    assert _same_graph(live.backward, expected.backward)
    assert live._tag_count == expected._tag_count


def _same_graph(left, right) -> bool:
    if set(left) != set(right):
        return False
    return all(
        set(left[k]) == set(right[k]) and all(abs(left[k][t] - right[k][t]) < 1e-9 for t in left[k])
        for k in left
    )


def test_custom_pair_similarity_keeps_in_thread_build(seeded_db):
    live = DirectedCooccurrence(seeded_db, pair_sim_service=SimpleNamespace(get_similarity=lambda a, b: 0.3))
    assert CooccurrenceScheduler(live, build_in_subprocess=True)._subprocess_request() is None


def test_snapshot_roundtrip_and_scheduler_saves_after_rebuild(seeded_db, tmp_path):
    path = str(tmp_path / "graph.json")
    live = _matrix(seeded_db)
    scheduler = CooccurrenceScheduler(live, snapshot_path=path)
    asyncio.run(scheduler.force_rebuild(reason="test"))
    saved = json.load(open(path, encoding="utf-8"))
    assert saved["version"] == 1 and saved["forward"]

    restored = _matrix(seeded_db)
    restarted = CooccurrenceScheduler(restored, snapshot_path=path, cooldown_sec=1800)
    built_at = restarted.load_snapshot()
    assert built_at == pytest.approx(saved["built_at"])
    assert _same_graph(restored.forward, {k: dict(v) for k, v in live.forward.items()})
    assert _same_graph(restored.backward, live.backward)
    assert restarted._last_rebuild_ts > 0  # 加载即视为刚重建：冷却期内不会立刻全量重建


def test_snapshot_bound_is_reapplied_and_bad_files_are_ignored(seeded_db, tmp_path):
    path = tmp_path / "graph.json"
    wide = {"1": {str(t): 1.0 / t for t in range(2, 12)}}
    path.write_text(json.dumps({"version": 1, "built_at": 5.0, "tag_count": 12, "forward": wide}), encoding="utf-8")
    narrow = DirectedCooccurrence(seeded_db, max_neighbors_per_tag=3)
    assert narrow.load_snapshot(str(path)) == 5.0
    assert sorted(narrow.forward[1]) == [2, 3, 4]
    assert all(len(v) <= 3 for v in narrow.backward.values())

    fresh = DirectedCooccurrence(seeded_db)
    for content in ("not json", json.dumps({"version": 99, "forward": wide}), json.dumps({"version": 1, "forward": {}})):
        path.write_text(content, encoding="utf-8")
        assert fresh.load_snapshot(str(path)) is None
        assert not fresh.forward
    assert fresh.load_snapshot(str(tmp_path / "missing.json")) is None


class _FlakyConnection:
    def __init__(self, failures: int, message: str = "database is locked"):
        self.failures = failures
        self.message = message
        self.calls = 0

    def execute(self, sql):
        self.calls += 1
        if self.calls <= self.failures:
            raise sqlite3.OperationalError(self.message)


def test_begin_immediate_retries_lock_then_gives_up():
    coordinator = WriteCoordinator.__new__(WriteCoordinator)
    ok = _FlakyConnection(failures=2)
    coordinator._begin_immediate(ok)
    assert ok.calls == 3

    stuck = _FlakyConnection(failures=10)
    with pytest.raises(sqlite3.OperationalError):
        coordinator._begin_immediate(stuck)
    assert stuck.calls == WriteCoordinator.BEGIN_LOCK_RETRIES + 1

    other = _FlakyConnection(failures=1, message="no such table: x")
    with pytest.raises(sqlite3.OperationalError):
        coordinator._begin_immediate(other)
    assert other.calls == 1
