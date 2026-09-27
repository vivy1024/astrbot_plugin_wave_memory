import asyncio
import importlib
import sys
import types
import unittest

import numpy as np


if "quart" not in sys.modules:
    quart_mod = types.ModuleType("quart")

    class _Blueprint:
        def __init__(self, *args, **kwargs):
            pass

        def route(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

    quart_mod.Blueprint = _Blueprint
    quart_mod.jsonify = lambda payload=None, **kwargs: payload if payload is not None else kwargs
    quart_mod.request = types.SimpleNamespace(args={}, get_json=lambda *args, **kwargs: {})
    quart_mod.Response = lambda *args, **kwargs: (args, kwargs)
    sys.modules["quart"] = quart_mod


class _EmbeddingService:
    async def get_embedding(self, text):
        return [0.1, 0.2, 0.3]


class _MemoryIndex:
    count = 100

    def search(self, query_vec, k=20):
        return [(101, 0.2), (102, 0.4)]


class _TagIndex:
    count = 100

    def search(self, query_vec, k=5):
        return [("tag-a", 0.2), ("tag-b", 0.8)]


class _DB:
    def __init__(self):
        self.touched = []

    def get_memories_by_ids(self, ids, *, scope):
        return [
            {
                "id": memory_id,
                "content": f"memory-{memory_id}",
                "tags": [],
                "timestamp": 1,
                "importance": 1.0,
                "source": "live",
            }
            for memory_id in ids
        ]

    def get_tag_vectors_by_ids(self, ids):
        vectors = {
            "tag-a": np.array([1.0, 0.0, 0.0], dtype=np.float32),
            "tag-b": np.array([0.0, 1.0, 0.0], dtype=np.float32),
            "tag-c": np.array([0.0, 0.0, 1.0], dtype=np.float32),
        }
        return {item: vectors[item] for item in ids if item in vectors}

    def touch_memories(self, ids):
        self.touched.append(list(ids))


class _Cooccurrence:
    node_count = 3


class _Container:
    def __init__(self):
        from engine.query_engine import QueryEngine

        self.embedding_service = _EmbeddingService()
        self.memory_index = _MemoryIndex()
        self.tag_index = _TagIndex()
        self.spike_router = None
        self.geodesic = None
        self.epa = None
        self.residual_pyramid = None
        self.cooccurrence = _Cooccurrence()
        self.db = _DB()
        self.query_engine = QueryEngine(
            self.db,
            self.memory_index,
            self.embedding_service,
            {"min_similarity": 0.0},
            tag_index=self.tag_index,
            cooccurrence=self.cooccurrence,
            spike_router=self.spike_router,
            residual_pyramid=self.residual_pyramid,
            epa=self.epa,
            geodesic=self.geodesic,
        )

    def refresh_query_engine(self):
        self.query_engine.spike_router = self.spike_router
        self.query_engine.residual_pyramid = self.residual_pyramid
        self.query_engine.epa = self.epa
        self.query_engine.geodesic = self.geodesic


class _EPA:
    initialized = True

    def analyze(self, query_vec):
        return {"logic_depth": 0.75, "entropy": 0.25, "dominant_axis": 2}


class _ResidualPyramid:
    max_levels = 3
    top_k = 10

    def __init__(self):
        self.max_levels = 3
        self.top_k = 10
        self.seen = None

    def analyze(self, query_vec):
        self.seen = {"max_levels": self.max_levels, "top_k": self.top_k}
        return {
            "levels": [[
                {"tag_id": "tag-a", "similarity": 0.91, "level": 0},
                {"tag_id": "tag-b", "similarity": 0.82, "level": 0},
            ]],
            "all_tag_ids": ["tag-a", "tag-b"],
            "coverage": 0.64,
            "final_residual": [0.01, 0.02, 0.03],
        }


class _SpikeRouter:
    max_hops = 4
    firing_threshold = 0.1

    def __init__(self):
        self.max_hops = 4
        self.firing_threshold = 0.1
        self.seen = None

    def propagate(self, seed_tags, epa_result=None):
        self.seen = {"max_hops": self.max_hops, "firing_threshold": self.firing_threshold}
        return {
            "activated_tags": [
                {"tag_id": seed_tags[0]["tag_id"], "energy": 1.0, "is_emergent": False},
                {"tag_id": "tag-c", "energy": 0.73, "is_emergent": True},
            ],
            "energy_field": {seed_tags[0]["tag_id"]: 1.0, "tag-c": 0.73},
        }


class _Geodesic:
    alpha = 0.3

    def __init__(self):
        self.alpha = 0.3
        self.seen = None

    def rerank(self, candidates, energy_field):
        self.seen = {"alpha": self.alpha}
        reranked = list(reversed(candidates))
        for idx, item in enumerate(reranked):
            item["score"] = 0.95 - idx * 0.1
            item["geo_score"] = 0.88 - idx * 0.2
        return reranked


class _FullAdvancedContainer(_Container):
    def __init__(self):
        super().__init__()
        self.epa = _EPA()
        self.residual_pyramid = _ResidualPyramid()
        self.spike_router = _SpikeRouter()
        self.geodesic = _Geodesic()
        self.refresh_query_engine()


class V440QueryDebugContractTest(unittest.TestCase):
    @staticmethod
    def _scope():
        from domain.scope import RuntimeScope, SessionRef

        return RuntimeScope(
            bot_id="debugger-bot",
            visibility="group",
            session=SessionRef(
                id="qq:group:debug-group",
                platform_id="qq",
                kind="group",
                conversation_id="debug-group",
            ),
        )

    def test_query_debug_returns_available_stage_details_and_highlights(self):
        memories = importlib.import_module("webui.blueprints.memories")

        async def _get_json(silent=True):
            return {
                "text": "苹果派是什么",
                "top_k": 2,
                "debug": True,
                "stages": {
                    "epa": True,
                    "pyramid": True,
                    "spike": True,
                    "geodesic": True,
                },
            }

        old_container = memories.get_container
        old_scope = memories._request_scope
        old_request = memories.request
        old_jsonify = memories.jsonify
        memories.get_container = lambda: _FullAdvancedContainer()
        memories._request_scope = lambda: self._scope()
        memories.request = types.SimpleNamespace(get_json=_get_json)
        memories.jsonify = lambda payload: payload
        try:
            payload = asyncio.run(memories.query_test.__wrapped__())
        finally:
            memories.get_container = old_container
            memories._request_scope = old_scope
            memories.request = old_request
            memories.jsonify = old_jsonify

        self.assertEqual([item["id"] for item in payload["results"]], [102, 101])
        debug = payload["debug"]
        self.assertNotIn("result", debug["epa"])
        self.assertEqual(debug["epa"]["logic_depth"], 0.75)
        self.assertEqual(debug["epa"]["entropy"], 0.25)
        self.assertEqual(debug["epa"]["dominant_axis"], 2)
        self.assertEqual(debug["epa"]["interpretation"], "focused")
        self.assertEqual(debug["vector_search"]["used_vector"], "wave_boosted")
        self.assertEqual(debug["vector_search"]["top_candidates"][0]["memory_id"], 101)
        self.assertEqual(debug["vector_search"]["top_candidates"][0]["similarity"], 0.8)
        self.assertEqual(debug["pyramid"]["levels"][0][0]["tag_id"], "tag-a")
        self.assertEqual(debug["pyramid"]["coverage"], 0.64)
        self.assertEqual(debug["spike"]["seed_tags"][0]["tag_id"], "tag-a")
        self.assertEqual(debug["spike"]["activated_tags"][1]["tag_id"], "tag-c")
        self.assertTrue(debug["spike"]["activated_tags"][1]["is_emergent"])
        self.assertEqual(debug["spike"]["energy_field_size"], 2)
        self.assertEqual(debug["spike"]["energy_field_top"][0]["tag_id"], "tag-a")
        self.assertEqual(debug["geodesic"]["before_ids"], [101, 102])
        self.assertEqual(debug["geodesic"]["after_ids"], [102, 101])
        self.assertTrue(debug["geodesic"]["available"])
        self.assertEqual(debug["geodesic"]["reranked"][0]["memory_id"], 102)
        self.assertEqual(debug["geodesic"]["reranked"][0]["rank_before"], 2)
        self.assertEqual(debug["geodesic"]["reranked"][0]["rank_after"], 1)
        self.assertEqual(debug["geodesic"]["reranked"][0]["geo_score"], 0.88)
        self.assertEqual(debug["final"]["score_breakdown"][0]["memory_id"], 102)
        self.assertIn("similarity", debug["final"]["score_breakdown"][0])
        self.assertIn("score_before_geodesic", debug["final"]["score_breakdown"][0])
        self.assertIn("geo_score", debug["final"]["score_breakdown"][0])
        self.assertNotIn("score_breakdown", payload["results"][0])
        self.assertEqual(debug["highlights"]["pyramid_tags"][0]["tag_id"], "tag-a")
        self.assertEqual(debug["highlights"]["seed_tags"][0]["tag_id"], "tag-a")
        self.assertEqual(debug["highlights"]["emergent_tags"][0]["tag_id"], "tag-c")
        self.assertEqual(debug["highlights"]["geodesic_memory_ids"], [102, 101])
        self.assertEqual(debug["highlights"]["final_memory_ids"], [102, 101])

    def test_query_debug_applies_stage_params_without_persisting_mutation(self):
        memories = importlib.import_module("webui.blueprints.memories")
        container = _FullAdvancedContainer()
        container.epa = _EPA()
        container.residual_pyramid = _ResidualPyramid()
        container.spike_router = _SpikeRouter()
        container.geodesic = _Geodesic()

        async def _get_json(silent=True):
            return {
                "text": "苹果派是什么",
                "top_k": 2,
                "debug": True,
                "stages": {
                    "epa": True,
                    "pyramid": True,
                    "spike": True,
                    "geodesic": True,
                },
                "params": {
                    "pyramid_max_levels": 2,
                    "pyramid_top_k": 1,
                    "spike_max_hops": 7,
                    "spike_firing_threshold": 0.42,
                    "geodesic_alpha": 0.7,
                },
            }

        old_container = memories.get_container
        old_scope = memories._request_scope
        old_request = memories.request
        old_jsonify = memories.jsonify
        memories.get_container = lambda: container
        memories._request_scope = lambda: self._scope()
        memories.request = types.SimpleNamespace(get_json=_get_json)
        memories.jsonify = lambda payload: payload
        try:
            payload = asyncio.run(memories.query_test.__wrapped__())
        finally:
            memories.get_container = old_container
            memories._request_scope = old_scope
            memories.request = old_request
            memories.jsonify = old_jsonify

        debug = payload["debug"]
        self.assertEqual(debug["query"]["params"]["pyramid_max_levels"], 2)
        self.assertEqual(debug["query"]["params"]["pyramid_top_k"], 1)
        self.assertEqual(debug["query"]["params"]["spike_max_hops"], 7)
        self.assertEqual(debug["query"]["params"]["spike_firing_threshold"], 0.42)
        self.assertEqual(debug["query"]["params"]["geodesic_alpha"], 0.7)
        self.assertIsNone(container.residual_pyramid.seen)
        self.assertIsNone(container.spike_router.seen)
        self.assertIsNone(container.geodesic.seen)
        self.assertEqual(debug["pyramid"]["params"], {"max_levels": 2, "top_k": 1})
        self.assertEqual(debug["spike"]["params"]["max_hops"], 7)
        self.assertEqual(debug["spike"]["params"]["firing_threshold"], 0.42)
        self.assertEqual(debug["geodesic"]["params"], {"alpha": 0.7})
        self.assertEqual(container.residual_pyramid.max_levels, 3)
        self.assertEqual(container.residual_pyramid.top_k, 10)
        self.assertEqual(container.spike_router.max_hops, 4)
        self.assertEqual(container.spike_router.firing_threshold, 0.1)
        self.assertEqual(container.geodesic.alpha, 0.3)

    def test_query_debug_requires_canonical_group_scope(self):
        memories = importlib.import_module("webui.blueprints.memories")

        async def _get_json(silent=True):
            return {"text": "苹果派是什么", "debug": True}

        old_container = memories.get_container
        old_scope = memories._request_scope
        old_request = memories.request
        old_jsonify = memories.jsonify
        memories.get_container = lambda: _Container()
        memories._request_scope = lambda: None
        memories.request = types.SimpleNamespace(get_json=_get_json)
        memories.jsonify = lambda payload: payload
        try:
            payload, status = asyncio.run(memories.query_test.__wrapped__())
        finally:
            memories.get_container = old_container
            memories._request_scope = old_scope
            memories.request = old_request
            memories.jsonify = old_jsonify

        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "canonical_group_scope_required")

    def test_query_debug_returns_stage_envelope_and_degraded_reasons(self):
        memories = importlib.import_module("webui.blueprints.memories")

        async def _get_json(silent=True):
            return {
                "text": "苹果派是什么",
                "top_k": 2,
                "debug": True,
                "stages": {
                    "epa": True,
                    "pyramid": True,
                    "spike": False,
                    "geodesic": True,
                },
            }

        old_container = memories.get_container
        old_scope = memories._request_scope
        old_request = memories.request
        old_jsonify = memories.jsonify
        memories.get_container = lambda: _Container()
        memories._request_scope = lambda: self._scope()
        memories.request = types.SimpleNamespace(get_json=_get_json)
        memories.jsonify = lambda payload: payload
        try:
            payload = asyncio.run(memories.query_test.__wrapped__())
        finally:
            memories.get_container = old_container
            memories._request_scope = old_scope
            memories.request = old_request
            memories.jsonify = old_jsonify

        self.assertEqual([item["id"] for item in payload["results"]], [101, 102])
        self.assertIn("debug", payload)
        debug = payload["debug"]
        for key in ("query", "embedding", "epa", "pyramid", "spike", "vector_search", "scoring", "geodesic", "final", "warnings"):
            self.assertIn(key, debug)

        self.assertEqual(debug["query"]["text"], "[REDACTED]")
        self.assertEqual(debug["query"]["text_length"], 6)
        self.assertEqual(debug["embedding"]["dimension"], 3)
        self.assertEqual(debug["vector_search"]["candidate_count"], 2)
        self.assertIs(debug["epa"]["enabled"], True)
        self.assertIs(debug["epa"]["available"], False)
        self.assertEqual(debug["epa"]["reason_code"], "epa_unavailable")
        self.assertIs(debug["pyramid"]["enabled"], True)
        self.assertIs(debug["pyramid"]["available"], False)
        self.assertIs(debug["spike"]["enabled"], False)
        self.assertEqual(debug["spike"]["reason_code"], "disabled_by_request")
        self.assertIs(debug["geodesic"]["enabled"], True)
        self.assertIs(debug["geodesic"]["available"], False)
        self.assertGreaterEqual(len(debug["warnings"]), 3)
        self.assertEqual(debug["final"]["result_count"], 2)


if __name__ == "__main__":
    unittest.main()
