"""共现传播核 V9.1：构建核、残差锚增益、软非回溯传播、快照与子进程。"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import sys
import types
from types import SimpleNamespace

import numpy as np
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

from engine.cooccurrence_v91 import V91Params, anchor_gain, build_kernel, compute_anchor_gains, fir_weights
from engine.database import WaveMemoryDB
from engine.directed_cooccurrence import CooccurrenceScheduler, DirectedCooccurrence
from engine.geodesic_rerank import GeodesicReranker
from engine.spike_routing import SpikeRouter
from services.pair_similarity import PairSimilarityService


# ─── 纯算法 ───

def test_kernel_rows_share_fixed_outbound_budget_and_hub_is_damped():
    params = V91Params(tension_threshold=99.0)  # 不产生虫洞，单看预算与枢纽校正
    # 标签 9 是被所有人指向的枢纽；1 的两个邻居证据相同
    evidence = {
        1: {2: 1.0, 9: 1.0},
        2: {1: 1.0, 9: 1.0},
        3: {9: 1.0, 4: 1.0},
        4: {3: 1.0, 9: 1.0},
        5: {9: 1.0, 6: 1.0},
        6: {5: 1.0, 9: 1.0},
        9: {1: 1.0},
    }
    kernel, wormholes = build_kernel(evidence, {}, params, max_neighbors=64)
    assert not wormholes
    for src, edges in kernel.items():
        assert sum(edges.values()) == pytest.approx(params.outbound_mass)
    # 同样证据下，指向枢纽的份额被压低
    assert kernel[1][9] < kernel[1][2]


def test_wormhole_edges_get_reserve_mass():
    params = V91Params(tension_threshold=1.0)
    evidence = {1: {2: 0.9, 3: 0.2}, 2: {1: 0.9}, 3: {1: 0.2}}
    kernel, wormholes = build_kernel(evidence, {2: 1.5, 1: 1.0, 3: 1.0}, params, max_neighbors=64)
    assert (1, 2) in wormholes and (1, 3) not in wormholes
    assert sum(kernel[1].values()) == pytest.approx(params.outbound_mass)
    assert kernel[1][2] > kernel[1][3]


def test_row_is_trimmed_before_normalisation():
    evidence = {1: {t: float(t) for t in range(2, 30)}}
    kernel, _ = build_kernel(evidence, {}, V91Params(tension_threshold=99.0), max_neighbors=5)
    assert len(kernel[1]) == 5
    assert sum(kernel[1].values()) == pytest.approx(0.95)


def test_anchor_gain_mapping_and_fir():
    params = V91Params()
    assert anchor_gain(0.0, params) == pytest.approx(0.75)
    assert anchor_gain(1.0, params) == pytest.approx(2.0)
    assert anchor_gain(5.0, params) == pytest.approx(2.0)
    weights = fir_weights(0.6, 4)
    assert sum(weights) == pytest.approx(1.0)
    assert weights[1] / weights[0] == pytest.approx(0.6)


def test_residual_is_low_when_tag_lies_in_neighbour_span():
    rng = np.random.default_rng(0)
    basis = rng.normal(size=(3, 16)).astype(np.float32)
    vectors = {i: basis[i % 3] + 0.01 * rng.normal(size=16).astype(np.float32) for i in range(1, 6)}
    vectors[10] = basis[0] + basis[1]           # 在邻域张成的子空间里
    vectors[11] = rng.normal(size=16).astype(np.float32)  # 与邻域无关
    evidence = {10: {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}, 11: {1: 1.0, 2: 1.0, 3: 1.0, 4: 1.0}, 5: {1: 1.0}}
    residuals, gains = compute_anchor_gains(evidence, vectors, V91Params())
    assert residuals[10] < 0.2 < residuals[11]
    assert 5 not in residuals  # 邻居不足 3 个不给值（按 1 处理）
    assert gains[11] > gains[10]


# ─── 构建与发布 ───

def _seed(db: WaveMemoryDB) -> dict[str, int]:
    conn = db.conn
    names = ["缺氧", "电力", "氧气", "羽书", "直播", "种田", "孤儿"]
    for name in names:
        conn.execute(
            "INSERT INTO scoped_tags(bot_id, session_id, visibility, name, tag_type, created_at, updated_at) "
            "VALUES ('yushu', 'qq:group:g1', 'group', ?, 'topic', 1.0, 1.0)",
            (name,),
        )
    conn.commit()
    tags = {name: tag_id for tag_id, name in conn.execute("SELECT id, name FROM scoped_tags").fetchall()}
    memories = [
        ["缺氧", "电力", "氧气"], ["缺氧", "电力"], ["缺氧", "氧气", "电力"],
        ["羽书", "直播"], ["直播", "羽书", "缺氧"], ["种田", "孤儿"],  # 种田-孤儿只共现一次
    ]
    for memory_id, names_in_memory in enumerate(memories, 1):
        conn.execute(
            "INSERT INTO memories(id, group_id, content, timestamp, bot_id, session_id, visibility) "
            "VALUES (?, 'g', '一条有标签的记忆', 1.0, 'yushu', 'qq:group:g1', 'group')",
            (memory_id,),
        )
        for position, name in enumerate(names_in_memory, 1):
            conn.execute(
                "INSERT INTO scoped_memory_tags(bot_id, session_id, visibility, memory_id, tag_id, position, relevance, created_at) "
                "VALUES ('yushu', 'qq:group:g1', 'group', ?, ?, ?, 1.0, 1.0)",
                (memory_id, tags[name], position),
            )
    conn.commit()
    return tags


@pytest.fixture()
def seeded(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "w.db"), dimension=4)
    tags = _seed(db)
    yield db, tags
    db.close()


def _v91(db, **kwargs):
    return DirectedCooccurrence(
        db, pair_sim_service=PairSimilarityService(db), kernel_version="v91", v91_params=V91Params(**kwargs)
    )


def _same(left, right) -> bool:
    return set(left) == set(right) and all(
        set(left[k]) == set(right[k]) and all(abs(left[k][t] - right[k][t]) < 1e-9 for t in left[k]) for k in left
    )


def test_v91_rebuild_filters_support_and_budgets_rows(seeded):
    db, tags = seeded
    matrix = _v91(db)
    matrix.rebuild()
    assert tags["种田"] not in matrix.forward and tags["孤儿"] not in matrix.forward  # 支持度 1 被滤掉
    assert {tags["缺氧"], tags["电力"], tags["氧气"], tags["羽书"], tags["直播"]} <= set(matrix.forward)
    for edges in matrix.forward.values():
        assert sum(edges.values()) <= 0.95 + 1e-9

    loose = _v91(db, min_support=1)
    loose.rebuild()
    assert tags["种田"] in loose.forward


def test_v91_subprocess_matches_thread_and_snapshot_checks_kernel(seeded, tmp_path, monkeypatch):
    db, _ = seeded
    expected = _v91(db)
    expected.rebuild()

    monkeypatch.setattr(DirectedCooccurrence, "rebuild", lambda self: (_ for _ in ()).throw(AssertionError("应走子进程")))
    live = _v91(db)
    path = str(tmp_path / "graph.json")
    scheduler = CooccurrenceScheduler(live, build_in_subprocess=True, snapshot_path=path)
    asyncio.run(scheduler.force_rebuild(reason="test"))
    assert _same(live.forward, expected.forward)
    assert live.wormhole_edges == expected.wormhole_edges
    assert live.anchor_gain == expected.anchor_gain
    assert json.load(open(path, encoding="utf-8"))["kernel_version"] == "v91"

    restored = _v91(db)
    assert restored.load_snapshot(path) is not None
    assert _same(restored.forward, live.forward) and restored.wormhole_edges == live.wormhole_edges
    legacy = DirectedCooccurrence(db)  # 换回 legacy 时不能沿用 v91 快照
    assert legacy.load_snapshot(path) is None


# ─── 传播 ───

class _Graph:
    def __init__(self, forward, wormholes=()):
        self.forward = forward
        self.wormhole_edges = frozenset(wormholes)
        self.kernel_version = "v91"
        self.v91_params = V91Params()

    @property
    def node_count(self):
        return len(self.forward)


def test_v91_propagation_suppresses_immediate_return_and_reaches_second_hop():
    # 1 → 2 → {1, 3}；回到 1 的质量只保留 15%
    graph = _Graph({1: {2: 0.95}, 2: {1: 0.475, 3: 0.475}, 3: {2: 0.95}}, wormholes={(1, 2), (2, 3)})
    router = SpikeRouter(graph, max_hops=4, firing_threshold=0.01)
    result = router.propagate([{"tag_id": 1, "weight": 1.0}])
    assert result["kernel"] == "v91"
    field = result["energy_field"]
    assert field[2] > field[3] > 0
    emergent = [item["tag_id"] for item in result["activated_tags"] if item["is_emergent"]]
    assert emergent[:2] == [2, 3]

    no_suppression = _Graph(graph.forward, graph.wormhole_edges)
    no_suppression.v91_params = V91Params(return_flow_factor=1.0)
    baseline = SpikeRouter(no_suppression, max_hops=4, firing_threshold=0.01).propagate([{"tag_id": 1, "weight": 1.0}])
    assert field[1] < baseline["energy_field"][1]


def test_legacy_kernel_keeps_old_propagation():
    graph = _Graph({1: {2: 1.0}})
    graph.kernel_version = "legacy"
    graph.get_neighbors = lambda tag_id, max_neighbors=20: sorted(graph.forward.get(tag_id, {}).items(), key=lambda x: -x[1])
    result = SpikeRouter(graph).propagate([{"tag_id": 1, "weight": 1.0}])
    assert "kernel" not in result


# ─── 测地重排读表 ───

def test_geodesic_reads_scoped_tags_when_enabled(seeded):
    db, tags = seeded
    reranker = GeodesicReranker(db)
    assert reranker._get_memory_tags([1]) == {}
    reranker.use_scoped_tags = True
    assert set(reranker._get_memory_tags([1])[1]) == {tags["缺氧"], tags["电力"], tags["氧气"]}
