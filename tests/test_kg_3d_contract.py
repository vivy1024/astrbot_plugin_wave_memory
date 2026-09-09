import asyncio
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest.mock import patch


REPO_ROOT = Path(__file__).resolve().parents[1]


if "astrbot.api" not in sys.modules:
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")

    class _Logger:
        def debug(self, *args, **kwargs): pass
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass

    api_mod.logger = _Logger()
    sys.modules.setdefault("astrbot", astrbot_mod)
    sys.modules["astrbot.api"] = api_mod


if "quart" not in sys.modules:
    quart_mod = types.ModuleType("quart")

    class _Blueprint:
        def __init__(self, name, import_name, url_prefix=None):
            self.name = name
            self.import_name = import_name
            self.url_prefix = url_prefix

        def route(self, *args, **kwargs):
            def decorator(func):
                return func
            return decorator

    class _Request:
        headers = {}
        args = {}

        async def get_json(self, silent=False):
            return {}

    def _jsonify(obj=None, *args, **kwargs):
        return {} if obj is None else obj

    quart_mod.Blueprint = _Blueprint
    quart_mod.request = _Request()
    quart_mod.jsonify = _jsonify
    sys.modules["quart"] = quart_mod


_SCOPE_ARGS = {
    "bot_id": "bot-alpha",
    "session_id": "qq:group:g1",
    "visibility": "group",
}


class NeuroGalaxy3DFrontendContractTest(unittest.TestCase):
    def test_explore_product_entry_is_retained_and_scope_is_explicit(self):
        html_path = REPO_ROOT / "webui" / "static" / "explore.html"
        self.assertTrue(html_path.exists())
        html = html_path.read_text(encoding="utf-8")
        self.assertIn("galaxy-container", html)
        self.assertIn("OrbitControls", html)
        self.assertIn("scope-bot-id", html)
        self.assertIn("scope-session-id", html)
        self.assertIn("scope-visibility", html)
        self.assertIn("installScopedApiFetch", html)
        self.assertIn("只读", html)

    def test_kg_js_keeps_3d_read_runtime(self):
        js = (REPO_ROOT / "webui" / "static" / "kg.js").read_text(encoding="utf-8")
        for marker in (
            "new THREE.Scene", "new THREE.Raycaster", "graphState", "nodes: new Map",
            "edges: new Map", "appendGraphData", "doQuery", "doPathFind",
            "loadPersonGraph", "focusPerson", "focusNode", "loadTimeline",
        ):
            self.assertIn(marker, js)
        self.assertNotIn("new Sigma", js)
        self.assertNotIn("graphology.Graph", js)

    def test_multilayer_threejs_contract_is_explicit_and_read_only(self):
        html = (REPO_ROOT / "webui" / "static" / "explore.html").read_text(encoding="utf-8")
        js = (REPO_ROOT / "webui" / "static" / "kg.js").read_text(encoding="utf-8")
        config = (REPO_ROOT / "webui" / "static" / "kg-config.js").read_text(encoding="utf-8")
        for layer in (
            "facts", "memories", "beliefs", "jargon", "concerns", "mood",
            "timeline", "affinity", "few_shot", "book_lore", "communities",
        ):
            self.assertIn(f'data-layer="{layer}"', html)
        for marker in (
            "_kgFullNodes", "data.nodes", "cfg-memory-limit", "cfg-similarity-k",
            "cfg-similarity-threshold", "buildProjectionMetadataHtml",
        ):
            self.assertIn(marker, html + js + config)
        self.assertNotIn("fact-edit-dialog", html)
        self.assertNotIn("relation-edit-dialog", html)
        self.assertNotIn("method: 'DELETE'", js)
        self.assertNotIn("method: 'PUT'", js)
        self.assertNotIn("/api/kg/add-fact", js)

    def test_explore_blueprint_has_auth_on_every_route(self):
        source = (REPO_ROOT / "webui" / "blueprints" / "explore.py").read_text(encoding="utf-8")
        route_count = source.count("@explore_bp.route")
        self.assertGreater(route_count, 0)
        self.assertEqual(route_count, source.count("@require_auth"))
        for legacy_query in ("FROM person_registry", "JOIN memory_tags ", "FROM tags ", "FROM facts ", "FROM tag_relations "):
            self.assertNotIn(legacy_query, source)


class ScopedExploreKgContractTest(unittest.TestCase):
    def setUp(self):
        from webui.container import ServiceContainer, get_container
        from webui.blueprints import explore, kg

        ServiceContainer.reset()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.conn = sqlite3.connect(Path(self.tmp.name) / "wave_memory.db")
        self.addCleanup(self.conn.close)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY, sender_id TEXT, sender_name TEXT, group_id TEXT,
                content TEXT, timestamp REAL, bot_id TEXT, session_id TEXT, visibility TEXT,
                resolution_state TEXT, quarantine INTEGER, version INTEGER
            );
            CREATE TABLE scoped_facts (
                id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
                subject TEXT, predicate TEXT, object TEXT, confidence REAL, status TEXT,
                source_memory_id INTEGER, provenance TEXT, valid_from REAL, valid_until REAL,
                created_at REAL, updated_at REAL, revision INTEGER
            );
            CREATE TABLE scoped_tags (
                id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
                name TEXT, tag_type TEXT, description TEXT, confidence REAL, metadata TEXT,
                created_at REAL, updated_at REAL
            );
            CREATE TABLE scoped_memory_tags (
                bot_id TEXT, session_id TEXT, visibility TEXT, memory_id INTEGER, tag_id INTEGER,
                position INTEGER, relevance REAL, created_at REAL
            );
            CREATE TABLE scoped_tag_relations (
                id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
                source_tag_id INTEGER, target_tag_id INTEGER, relation_type TEXT,
                weight REAL, confidence REAL, metadata TEXT, status TEXT, valid_until REAL,
                revision INTEGER, created_at REAL, updated_at REAL
            );
            CREATE TABLE facts (id INTEGER PRIMARY KEY, subject TEXT, predicate TEXT, object TEXT);
            CREATE TABLE tags (id INTEGER PRIMARY KEY, name TEXT);
            CREATE TABLE tag_relations (id INTEGER PRIMARY KEY, source_tag_id INTEGER, target_tag_id INTEGER);

            INSERT INTO memories VALUES
                (1, 'u1', '同域用户', 'g1', '规范记忆', 10, 'bot-alpha', 'qq:group:g1', 'group', 'resolved', 0, 1),
                (2, 'u2', '跨域用户', 'g2', '跨域记忆', 11, 'bot-beta', 'qq:group:g2', 'group', 'resolved', 0, 1),
                (3, 'u3', '未解析用户', 'g1', '旧记忆', 12, NULL, NULL, NULL, 'unresolved', 1, NULL);
            INSERT INTO scoped_facts VALUES
                (11, 'bot-alpha', 'qq:group:g1', 'group', '同域用户', '喜欢', '猫', 0.9, 'reviewed', 1, '{}', NULL, NULL, 10, 10, 4),
                (12, 'bot-beta', 'qq:group:g2', 'group', '跨域用户', '知道', '秘密', 0.9, 'reviewed', 2, '{}', NULL, NULL, 11, 11, 2),
                (13, 'bot-alpha', 'qq:group:g1', 'group', '已删除主体', '隐藏', '已删除对象', 1.0, 'deleted', NULL, '{}', NULL, 12, 12, 12, 2);
            INSERT INTO scoped_tags VALUES
                (21, 'bot-alpha', 'qq:group:g1', 'group', '同域用户', 'person', '', 0.9, '{}', 10, 10),
                (22, 'bot-alpha', 'qq:group:g1', 'group', '猫', 'entity', '', 0.9, '{}', 10, 10),
                (23, 'bot-beta', 'qq:group:g2', 'group', '秘密', 'entity', '', 0.9, '{}', 11, 11);
            INSERT INTO scoped_memory_tags VALUES ('bot-alpha', 'qq:group:g1', 'group', 1, 22, 0, 1.0, 10);
            INSERT INTO scoped_tag_relations VALUES
                (31, 'bot-alpha', 'qq:group:g1', 'group', 21, 22, '关注', 0.8, 0.8, '{}', 'active', NULL, 3, 10, 10),
                (32, 'bot-beta', 'qq:group:g2', 'group', 23, 23, '泄漏', 1.0, 1.0, '{}', 'active', NULL, 2, 11, 11),
                (33, 'bot-alpha', 'qq:group:g1', 'group', 21, 22, '已删除关系', 1.0, 1.0, '{}', 'deleted', 12, 2, 12, 12);
            INSERT INTO facts VALUES (99, 'legacy', '泄漏', 'legacy-secret');
            """
        )
        self.conn.commit()
        container = get_container()
        from webui.api_contract import ObjectRefRegistry

        container.db = types.SimpleNamespace(conn=self.conn, scoped_knowledge=None)
        container.plugin_config = {}
        container.object_refs = ObjectRefRegistry(signing_key=b"k" * 32)
        container.scoped_knowledge_mutations = types.SimpleNamespace()
        self.explore = explore
        self.kg = kg
        self.originals = []
        for module in (explore, kg):
            self.originals.append((module, module.jsonify, module.request))
            module.jsonify = lambda value: value

    def tearDown(self):
        for module, jsonify, request in self.originals:
            module.jsonify = jsonify
            module.request = request

    def _request(self, args=None, body=None):
        class FakeRequest:
            def __init__(self):
                self.args = args or {}

            async def get_json(self, silent=False):
                return body or {}

        return FakeRequest()

    def test_explore_requires_complete_runtime_scope(self):
        self.explore.request = self._request({})
        payload, status = asyncio.run(self.explore.persons.__wrapped__())
        self.assertEqual(status, 400)
        self.assertEqual(payload["error"]["code"], "scope_required")

    def test_explore_reads_only_exact_scope_memories(self):
        self.explore.request = self._request({**_SCOPE_ARGS, "limit": "50"})
        people = asyncio.run(self.explore.persons.__wrapped__())
        self.assertEqual([item["name"] for item in people], ["同域用户"])

        self.explore.request = self._request({**_SCOPE_ARGS, "max_memories": "50"})
        person = asyncio.run(self.explore.person.__wrapped__("u1"))
        self.assertEqual([item["content"] for item in person["memories"]], ["规范记忆"])
        self.assertNotIn("跨域记忆", str(person))

    def test_explore_galaxy_excludes_scoped_tombstones(self):
        self.explore.request = self._request(_SCOPE_ARGS)
        payload = asyncio.run(self.explore.galaxy.__wrapped__())
        serialized = str(payload)
        self.assertIn("同域用户", serialized)
        self.assertIn("关注", serialized)
        self.assertNotIn("已删除主体", serialized)
        self.assertNotIn("已删除对象", serialized)
        self.assertNotIn("已删除关系", serialized)

    def test_kg_full_uses_scoped_facts_and_relations_only(self):
        self.kg.request = self._request({**_SCOPE_ARGS, "layers": "facts"})
        payload = asyncio.run(self.kg.kg_full.__wrapped__())
        serialized = str(payload)
        self.assertEqual(payload["scope"]["payload"]["bot_id"], "bot-alpha")
        self.assertTrue(payload["read_only"])
        self.assertIn("同域用户", serialized)
        self.assertIn("猫", serialized)
        self.assertNotIn("legacy-secret", serialized)
        self.assertNotIn("跨域用户", serialized)
        self.assertNotIn("秘密", serialized)
        self.assertNotIn("已删除主体", serialized)
        self.assertNotIn("已删除对象", serialized)
        self.assertNotIn("已删除关系", serialized)

    def test_full_and_entity_sign_only_canonical_mutable_rows_without_cache_pollution(self):
        self.kg.clear_kg_cache()
        self.kg.request = self._request({**_SCOPE_ARGS, "layers": "facts"})
        first = asyncio.run(self.kg.kg_full.__wrapped__())
        fact = next(edge for edge in first["edges"] if edge["kind"] == "fact")
        relation = next(edge for edge in first["edges"] if edge["kind"] == "tag_relation")
        self.assertEqual(fact["revision"], 4)
        self.assertEqual(relation["revision"], 3)
        self.assertEqual(fact["object_ref"]["version"], 4)
        self.assertEqual(relation["object_ref"]["version"], 3)
        self.assertTrue(fact["capabilities"]["update"]["available"])
        self.assertTrue(fact["editable"])
        self.assertFalse(fact["read_only"])
        cached_payload = next(iter(self.kg._overview_cache.values()))[1]
        self.assertNotIn("object_ref", str(cached_payload))

        second = asyncio.run(self.kg.kg_full.__wrapped__())
        second_fact = next(edge for edge in second["edges"] if edge["kind"] == "fact")
        self.assertTrue(second["cache"]["hit"])
        self.assertEqual(second_fact["object_ref"]["ref"], fact["object_ref"]["ref"])
        self.assertNotIn("object_ref", str(next(iter(self.kg._overview_cache.values()))[1]))

        self.conn.execute(
            "UPDATE scoped_facts SET confidence=0.7, revision=5, updated_at=20 WHERE id=11"
        )
        self.conn.commit()
        refreshed = asyncio.run(self.kg.kg_full.__wrapped__())
        refreshed_fact = next(edge for edge in refreshed["edges"] if edge["kind"] == "fact")
        self.assertEqual(refreshed_fact["confidence"], 0.7)
        self.assertEqual(refreshed_fact["object_ref"]["version"], 5)
        self.assertFalse(refreshed["cache"]["hit"])
        self.assertNotIn("object_ref", str(next(iter(self.kg._overview_cache.values()))[1]))

        self.kg.request = self._request({**_SCOPE_ARGS, "limit": "10"})
        entity = asyncio.run(self.kg.entity_detail.__wrapped__("同域用户"))
        self.assertEqual(entity["facts"][0]["revision"], 5)
        self.assertEqual(entity["facts"][0]["object_ref"]["version"], 5)
        self.assertEqual(entity["relations"][0]["object_ref"]["version"], 3)

    def test_command_api_resolves_ref_scope_and_revision_before_gateway(self):
        container = self.kg.get_container()

        class Gateway:
            def __init__(self):
                self.calls = []

            async def update_fact(self, **kwargs):
                self.calls.append(("update_fact", kwargs))
                return types.SimpleNamespace(
                    operation_id="op-1", kind="fact", locator=11, revision=5,
                    status="reviewed", previous_locator=None,
                )

        gateway = Gateway()
        container.scoped_knowledge_mutations = gateway
        self.kg.clear_kg_cache()
        self.kg.request = self._request({**_SCOPE_ARGS, "layers": "facts"})
        graph = asyncio.run(self.kg.kg_full.__wrapped__())
        fact = next(edge for edge in graph["edges"] if edge["kind"] == "fact")
        ref = fact["object_ref"]["ref"]

        self.kg.request = self._request(
            _SCOPE_ARGS,
            {"ref": ref, "revision": 4, "patch": {"confidence": 0.7}},
        )
        payload = asyncio.run(self.kg.command_update_fact.__wrapped__())
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["revision"], 5)
        self.assertEqual(gateway.calls[0][1]["target"].locator, 11)
        self.assertEqual(gateway.calls[0][1]["target"].revision, 4)
        self.assertEqual(self.kg._overview_cache, {})

        self.kg.request = self._request(
            _SCOPE_ARGS,
            {"ref": ref, "revision": 3, "patch": {"confidence": 0.6}},
        )
        stale, stale_status = asyncio.run(self.kg.command_update_fact.__wrapped__())
        self.assertEqual(stale_status, 409)
        self.assertEqual(stale["error"]["code"], "stale_revision")

        forged = ref[:-1] + ("0" if ref[-1] != "0" else "1")
        self.kg.request = self._request(
            _SCOPE_ARGS,
            {"ref": forged, "revision": 4, "patch": {"confidence": 0.6}},
        )
        hidden, hidden_status = asyncio.run(self.kg.command_update_fact.__wrapped__())
        self.assertEqual(hidden_status, 404)
        self.assertEqual(hidden["error"]["code"], "not_found")

        self.kg.request = self._request(
            {"bot_id": "bot-beta", "session_id": "qq:group:g2", "visibility": "group"},
            {"ref": ref, "revision": 4, "patch": {"confidence": 0.6}},
        )
        cross_scope, cross_status = asyncio.run(self.kg.command_update_fact.__wrapped__())
        self.assertEqual(cross_status, 404)
        self.assertEqual(cross_scope["error"]["code"], "not_found")

        self.kg.request = self._request(
            _SCOPE_ARGS,
            {"revision": 4, "patch": {"confidence": 0.6}},
        )
        missing, missing_status = asyncio.run(self.kg.command_update_fact.__wrapped__())
        self.assertEqual(missing_status, 400)
        self.assertEqual(missing["error"]["code"], "invalid_request")

        container.scoped_knowledge_mutations = None
        container.write_gateway = None
        self.kg.request = self._request(
            _SCOPE_ARGS,
            {"ref": ref, "revision": 4, "patch": {"confidence": 0.6}},
        )
        unavailable, unavailable_status = asyncio.run(self.kg.command_update_fact.__wrapped__())
        self.assertEqual(unavailable_status, 503)
        self.assertEqual(
            unavailable["error"]["code"],
            "scoped_knowledge_mutation_gateway_unavailable",
        )

    def test_all_four_command_routes_delegate_only_resolved_targets(self):
        container = self.kg.get_container()

        class Gateway:
            def __init__(self):
                self.calls = []

            async def delete_fact(self, **kwargs):
                self.calls.append(("delete_fact", kwargs["target"]))
                return types.SimpleNamespace(
                    operation_id="op-fd", kind="fact", locator=11, revision=5,
                    status="deleted", previous_locator=None,
                )

            async def update_tag_relation(self, **kwargs):
                self.calls.append(("update_relation", kwargs["target"], kwargs["fields"]))
                return types.SimpleNamespace(
                    operation_id="op-ru", kind="tag_relation", locator=31, revision=4,
                    status="active", previous_locator=None,
                )

            async def delete_tag_relation(self, **kwargs):
                self.calls.append(("delete_relation", kwargs["target"]))
                return types.SimpleNamespace(
                    operation_id="op-rd", kind="tag_relation", locator=31, revision=4,
                    status="deleted", previous_locator=None,
                )

            async def update_tag(self, **kwargs):
                self.calls.append(("update_tag", kwargs["target"], kwargs["fields"]))
                return types.SimpleNamespace(
                    operation_id="op-tu", kind="tag", locator=kwargs["target"].locator, revision=kwargs["target"].revision + 1,
                    status="active", previous_locator=None,
                )

        gateway = Gateway()
        container.scoped_knowledge_mutations = gateway
        self.kg.clear_kg_cache()
        self.kg.request = self._request({**_SCOPE_ARGS, "layers": "facts"})
        graph = asyncio.run(self.kg.kg_full.__wrapped__())
        fact = next(edge for edge in graph["edges"] if edge["kind"] == "fact")
        relation = next(edge for edge in graph["edges"] if edge["kind"] == "tag_relation")

        self.kg.request = self._request(
            _SCOPE_ARGS, {"ref": fact["object_ref"]["ref"], "revision": 4}
        )
        fact_deleted = asyncio.run(self.kg.command_delete_fact.__wrapped__())
        self.assertEqual(fact_deleted["status"], "deleted")

        self.kg.request = self._request(
            _SCOPE_ARGS,
            {"ref": relation["object_ref"]["ref"], "revision": 3, "patch": {"weight": 0.6}},
        )
        relation_updated = asyncio.run(self.kg.command_update_tag_relation.__wrapped__())
        self.assertEqual(relation_updated["revision"], 4)

        self.kg.request = self._request(
            _SCOPE_ARGS, {"ref": relation["object_ref"]["ref"], "revision": 3}
        )
        relation_deleted = asyncio.run(self.kg.command_delete_tag_relation.__wrapped__())
        self.assertEqual(relation_deleted["status"], "deleted")

        # 验证新增的 command_update_tag 路由
        scope = self.kg._group_scope_from_query()
        tag_ref = self.kg._object_descriptor("tag", 21, 1, scope)["ref"]
        self.kg.request = self._request(
            _SCOPE_ARGS,
            {"ref": tag_ref, "revision": 1, "patch": {"name": "新名称", "tag_type": "topic"}},
        )
        tag_updated = asyncio.run(self.kg.command_update_tag.__wrapped__())
        self.assertEqual(tag_updated["status"], "active")
        self.assertEqual(tag_updated["revision"], 2)

        self.assertEqual(
            [(call[0], call[1].kind) for call in gateway.calls],
            [
                ("delete_fact", "fact"),
                ("update_relation", "tag_relation"),
                ("delete_relation", "tag_relation"),
                ("update_tag", "tag"),
            ],
        )

        self.kg.request = self._request(
            _SCOPE_ARGS,
            {"ref": relation["object_ref"]["ref"], "revision": 3, "patch": {"metadata": {}}},
        )
        rejected, status = asyncio.run(self.kg.command_update_tag_relation.__wrapped__())
        self.assertEqual(status, 400)
        self.assertEqual(rejected["error"]["code"], "invalid_request")

    def test_legacy_and_bare_id_mutations_are_gone(self):
        for call in (
            lambda: self.kg.create_scoped_fact.__wrapped__(),
            lambda: self.kg.create_scoped_relation.__wrapped__(),
            lambda: self.kg.update_fact.__wrapped__(11),
            lambda: self.kg.delete_fact.__wrapped__(11),
            lambda: self.kg.update_tag_relation.__wrapped__(31),
            lambda: self.kg.delete_tag_relation.__wrapped__(31),
            lambda: self.kg.update_tag.__wrapped__(21),
            lambda: self.kg.rename_entity.__wrapped__(),
            lambda: self.kg.rename_entity_preview.__wrapped__(),
            lambda: self.kg.legacy_audit_facts.__wrapped__(),
            lambda: self.kg.legacy_audit_relations.__wrapped__(),
        ):
            payload, status = asyncio.run(call())
            self.assertEqual(status, 410)
            self.assertEqual(payload["error"]["code"], "legacy_mutation_disabled")

    def test_payment_is_disabled_without_strong_secret_and_never_writes_knowledge(self):
        before = (
            self.conn.execute("SELECT COUNT(*) FROM scoped_facts").fetchone()[0],
            self.conn.execute("SELECT COUNT(*) FROM scoped_tag_relations").fetchone()[0],
        )
        self.kg.request = self._request(_SCOPE_ARGS, {"amount": 100, "secret": "wavemoney"})
        payload, status = asyncio.run(self.kg.payment_webhook.__wrapped__())
        after = (
            self.conn.execute("SELECT COUNT(*) FROM scoped_facts").fetchone()[0],
            self.conn.execute("SELECT COUNT(*) FROM scoped_tag_relations").fetchone()[0],
        )
        self.assertEqual(status, 503)
        self.assertEqual(payload["error"]["code"], "payment_disabled")
        self.assertEqual(after, before)

    def test_scope_less_cache_warmup_never_reads_legacy_tables(self):
        result = self.kg.warmup_kg_cache("facts")
        self.assertEqual(result["edges"], 0)
        self.assertEqual(result["reason_code"], "scope_required")


if __name__ == "__main__":
    unittest.main()
