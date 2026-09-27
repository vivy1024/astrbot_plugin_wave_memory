"""势场重排：可信度退回、证据分级封顶加分、枢纽特异性。"""

from __future__ import annotations

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

from engine.potential_field_rerank import PotentialFieldParams, hub_specificity_base, rerank
from engine.spike_routing import EnergyField

V = {
    1: np.array([1, 0, 0, 0], np.float32),
    2: np.array([0.9, 0.3, 0, 0], np.float32),
    3: np.array([0.8, 0.5, 0.1, 0], np.float32),
    4: np.array([0.7, 0.6, 0.2, 0.1], np.float32),
    9: np.array([0, 0, 0, 1], np.float32),     # 与查询无关的枢纽
}
MEM = {10: np.array([0.95, 0.3, 0.05, 0], np.float32), 11: np.array([0.95, 0.3, 0.05, 0], np.float32),
       12: np.array([0, 0.1, 0, 1], np.float32)}


def _run(candidates, field, chains, *, seeds=(1,), specificity=None, params=None):
    return rerank(
        candidates, EnergyField(field, seed_ids=seeds), seed_ids=frozenset(seeds), tag_chains=chains,
        tag_vectors=V, memory_vectors=MEM, specificity_base=specificity or {}, anchor_gain={},
        params=params or PotentialFieldParams(),
    )


FIELD = {1: 1.0, 2: 0.6, 3: 0.4, 4: 0.3, 9: 0.5}


def test_small_or_peaked_field_leaves_order_untouched():
    candidates = [{"id": 10, "score": 0.5}, {"id": 11, "score": 0.6}]
    out, diag = _run(candidates, {1: 1.0, 2: 0.5}, {10: [(1, 1)], 11: [(2, 1)]})
    assert out is candidates and diag["reason"] == "field_too_small"
    out, diag = _run(candidates, {1: 1.0, 2: 1e-6, 3: 1e-6, 4: 1e-6}, {10: [(1, 1)]})
    assert out is candidates and diag["reason"] == "field_entropy_low"


def test_direct_evidence_bonus_is_capped_and_added_to_vector_score():
    candidates = [{"id": 11, "score": 0.70}, {"id": 10, "score": 0.60}]
    chains = {10: [(1, 1), (2, 2), (3, 3)], 11: []}
    out, diag = _run(candidates, FIELD, chains)
    assert diag["applied"]
    by_id = {c["id"]: c for c in out}
    assert by_id[10]["geo_class"] == "direct"
    assert 0 < by_id[10]["geo_bonus"] <= PotentialFieldParams().direct_bonus_cap
    assert by_id[10]["score"] == pytest.approx(0.60 + by_id[10]["geo_bonus"])
    assert by_id[11]["geo_bonus"] == 0
    # 加分有上限：向量分差距超过封顶值时不能逆转
    far = [{"id": 11, "score": 0.95}, {"id": 10, "score": 0.60}]
    out, _ = _run(far, FIELD, chains)
    assert out[0]["id"] == 11


def test_hub_tag_contributes_less_than_specific_tag():
    kernel = {s: {9: 0.5, 1: 0.1} for s in range(20, 40)}  # 9 被大量指向
    spec = hub_specificity_base(kernel)
    assert spec[9] < spec[1]
    candidates = [{"id": 10, "score": 0.6}, {"id": 12, "score": 0.6}]
    chains = {10: [(2, 1), (3, 2)], 12: [(9, 1), (4, 2)]}
    out, _ = _run(candidates, FIELD, chains, seeds=(), specificity=spec)
    by_id = {c["id"]: c for c in out}
    assert by_id[10]["geo_bonus"] >= by_id[12]["geo_bonus"]


def test_geodesic_reranker_uses_potential_field_in_row_budget_mode(tmp_path):
    from engine.database import WaveMemoryDB
    from engine.geodesic_rerank import GeodesicReranker

    db = WaveMemoryDB(str(tmp_path / "g.db"), dimension=4)
    try:
        conn = db.conn
        for tag_id in (1, 2, 3, 4):
            conn.execute(
                "INSERT INTO tag_catalog(id, normalized_name, display_name, tag_type, embedding, status, created_at, updated_at) "
                "VALUES (?, ?, ?, 'topic', ?, 'active', 1.0, 1.0)",
                (tag_id, f"t{tag_id}", f"t{tag_id}", V[tag_id].tobytes()),
            )
            conn.execute(
                "INSERT INTO scoped_tags(id, catalog_id, bot_id, session_id, visibility, name, tag_type, created_at, updated_at) "
                "VALUES (?, ?, 'b', 's', 'group', ?, 'topic', 1.0, 1.0)",
                (tag_id, tag_id, f"t{tag_id}"),
            )
        for memory_id in (10, 11):
            conn.execute(
                "INSERT INTO memories(id, group_id, content, timestamp, bot_id, session_id, visibility, vector) "
                "VALUES (?, 'g', 'x', 1.0, 'b', 's', 'group', ?)",
                (memory_id, MEM[memory_id].tobytes()),
            )
        for position, tag_id in enumerate((1, 2, 3), 1):
            conn.execute(
                "INSERT INTO scoped_memory_tags(bot_id, session_id, visibility, memory_id, tag_id, position, relevance, created_at) "
                "VALUES ('b', 's', 'group', 10, ?, ?, 1.0, 1.0)",
                (tag_id, position),
            )
        conn.commit()
        reranker = GeodesicReranker(db)
        reranker.use_scoped_tags = True
        reranker.cooccurrence = SimpleNamespace(forward={1: {2: 0.5}}, anchor_gain={})
        out = reranker.rerank([{"id": 11, "score": 0.65}, {"id": 10, "score": 0.60}], EnergyField(FIELD, seed_ids={1}))
        assert reranker.last_diagnostics["applied"]
        assert out[0]["id"] == 10 and out[0]["geo_class"] == "direct"
    finally:
        db.close()
