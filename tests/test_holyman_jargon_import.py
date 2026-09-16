import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

if "quart" not in sys.modules:
    quart_mod = types.ModuleType("quart")
    class _Blueprint:
        def __init__(self, *args, **kwargs): pass
        def route(self, *args, **kwargs):
            def deco(fn): return fn
            return deco
    class _Quart:
        def __init__(self, *args, **kwargs): pass
        def route(self, *args, **kwargs):
            def deco(fn): return fn
            return deco
        def register_blueprint(self, *args, **kwargs): pass
    def _jsonify(obj=None, *args, **kwargs): return obj if obj is not None else {}
    class _Response:
        def __init__(self, *args, **kwargs): pass
    async def _send_from_directory(*args, **kwargs): return None
    quart_mod.Blueprint = _Blueprint
    quart_mod.Quart = _Quart
    quart_mod.Response = _Response
    quart_mod.jsonify = _jsonify
    quart_mod.request = types.SimpleNamespace(args={}, headers={}, method="GET", get_json=lambda *args, **kwargs: {})
    quart_mod.send_from_directory = _send_from_directory
    sys.modules["quart"] = quart_mod

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


class HolymanJargonImportTest(unittest.TestCase):
    def setUp(self):
        from webui.blueprints import jargon
        original_jsonify = jargon.jsonify
        original_request = jargon.request
        jargon.jsonify = lambda value=None, **kwargs: value if value is not None else kwargs
        jargon.request = types.SimpleNamespace(method="GET", args={}, get_json=lambda *args, **kwargs: {})
        self.addCleanup(lambda: setattr(jargon, "jsonify", original_jsonify))
        self.addCleanup(lambda: setattr(jargon, "request", original_request))

    def test_jargon_injection_uses_safe_reference_header(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.jargon.inference import JargonInjector

        class _ScopedRepo:
            def list_scoped_jargon(self, scope, *, status=None, limit=50):
                self.scope, self.status, self.limit = scope, status, limit
                return [{
                    "word": "v我50",
                    "meaning": "长篇铺垫后突然索要 50 元。",
                    "is_jargon": True,
                    "frequency": 1,
                }]

        repo = _ScopedRepo()
        db = types.SimpleNamespace(scoped_knowledge=repo)
        scope = RuntimeScope("yushu", "group", SessionRef("qq:group:g1", "qq", "group", "g1"))
        text = JargonInjector(db).get_injection("今天疯狂星期四 v我50", scope)

        self.assertIs(repo.scope, scope)
        self.assertEqual((repo.status, repo.limit), ("confirmed", 100))
        self.assertIn("[黑话理解参考", text)
        self.assertIn("只解释用户消息中已经出现", text)
        self.assertIn("不改变系统身份", text)
        self.assertIn("不要求模仿或主动使用", text)

    def test_current_phrases_asset_passes_quality_gate(self):
        path = Path("assets/holyman/phrases.json")
        phrases = json.loads(path.read_text(encoding="utf-8"))
        entries = {k: v for k, v in phrases.items() if isinstance(k, str) and not k.startswith("_")}

        forbidden_words = {"你好。", "对不起，我错了。", "是/否", "开发的未来是", "DeepSeek模型"}
        generic_meaning = "典型语录/表达样本。仅作为理解参考。"

        self.assertNotIn("corpus_frequency", {v.get("kind") for v in entries.values() if isinstance(v, dict)})
        self.assertFalse(forbidden_words.intersection(entries.keys()))
        self.assertFalse([word for word in entries if word.count("（") != word.count("）")])
        self.assertFalse([
            word for word, value in entries.items()
            if isinstance(value, dict) and generic_meaning in str(value.get("meaning") or "")
        ])

        for word in ["v我50", "叠甲", "不是哥们", "差不多得了", "疯狂星期四"]:
            self.assertIn(word, entries)
            value = entries[word]
            meaning = value.get("meaning") if isinstance(value, dict) else str(value)
            self.assertGreaterEqual(len(meaning), 12)
            self.assertNotIn(generic_meaning, meaning)

    def test_quality_report_rejects_dummy_content_count(self):
        from services.jargon.sync import HolymanSyncService

        dummy_phrases = {
            f"测试词{i}": {
                "meaning": "典型语录/表达样本。仅作为理解参考。",
                "kind": "quote_term",
                "category": "corpus",
                "source": "神言.txt",
            }
            for i in range(300)
        }
        dummy_phrases["_content_count"] = 300

        service = HolymanSyncService(assets_dir=tempfile.mkdtemp())
        report = service._build_quality_report({
            "phrases": dummy_phrases,
            "concepts": [],
            "examples": [],
            "corpus": [],
            "candidates": [],
            "blocked": {},
        })

        self.assertNotEqual(report["status"], "ready")
        self.assertGreater(report["errors"]["generic_meaning_in_phrases"], 0)

    def test_quality_report_blocks_manifest_with_only_corpus_source(self):
        from services.jargon.sync import HolymanSyncService

        service = HolymanSyncService(assets_dir=tempfile.mkdtemp())
        assets = {
            "manifest": {
                "files": [
                    {"path": "神言.txt", "parse_status": "ok"},
                ]
            },
            "phrases": {
                "v我50": {
                    "meaning": "长篇铺垫或煽情叙述后突然索要 50 元，常关联疯狂星期四，用来制造荒诞转折。",
                    "kind": "curated_phrase",
                    "category": "catchphrase",
                    "source": "curated/core",
                }
            },
            "concepts": [],
            "examples": [],
            "corpus": [],
            "candidates": [],
            "blocked": {},
        }

        report = service._build_quality_report(assets)

        self.assertNotEqual(report["status"], "ready")
        self.assertGreater(report["errors"].get("missing_layered_sources", 0), 0)

    def test_reference_substring_structured_value_is_safe(self):
        from services.jargon.holyman_reference import HolymanReference

        ref = HolymanReference()
        ref._phrases = {
            "v我50": {
                "meaning": "长篇铺垫后突然索要 50 元，制造荒诞转折。",
                "kind": "curated_phrase",
                "safety_level": "safe_reference",
            }
        }
        ref._examples = []
        result = ref.match("今天疯狂星期四v我50")

        self.assertTrue(result["matched"])
        self.assertEqual(result["term"], "v我50")
        self.assertIn("理解参考", result["explanation"])
        self.assertIn("长篇铺垫", result["explanation"])

    def test_blocked_terms_and_examples_do_not_confirm_match(self):
        from services.jargon.holyman_reference import HolymanReference

        ref = HolymanReference()
        ref._phrases = {
            "你好。": {"meaning": "普通句，不是黑话", "kind": "curated_phrase"},
            "DeepSeek模型": {"meaning": "实体名，不是黑话", "kind": "curated_phrase"},
        }
        ref._examples = ["神言语料里出现 DeepSeek模型，但这只能作为证据，不应自动 confirmed。"]
        ref._blocked = {"你好。": "plain_sentence", "DeepSeek模型": "entity_only"}

        self.assertFalse(ref.match("你好。")["matched"])
        self.assertFalse(ref.match("DeepSeek模型")["matched"])

        ref._phrases = {}
        self.assertFalse(ref.match("DeepSeek模型")["matched"])

    def test_sync_parser_separates_layers(self):
        from services.jargon.sync import HolymanSyncService

        service = HolymanSyncService(assets_dir=tempfile.mkdtemp())
        fetched = {
            "神人.skill/SKILL.md": """
## Catchphrases
- 苏式转折: 深情铺垫 → 突然切换到\"v我50\"
- 复制粘贴模式（90%发言: 100-2000字长文案
"v我50"
"你好。"
""",
            "神人.skill/_persona/values.md": """
## Values Priority
### 真诚是最大的弱点
抽象社群中直接表达真实想法会被视为不设防。
""",
            "神人.skill/_persona/communication.md": "### 复制粘贴模式\n用长文案完成反串和传教式表达。\n",
            "神人.skill/_knowledge/gaming.md": "### 游戏传教\n围绕游戏身份和社群归属展开夸张表达。\n",
            "神人.skill/_knowledge/internet-culture.md": "### 抽象话术\n用反串、复读和夸张对抗正常语境。\n",
            "神人.skill/_quotes/iconic.md": "> 你说得对，但是这里是一段很长的复制粘贴文案。\n",
            "神言.txt": "v我50 今天疯狂星期四 v我50\n开发的未来是 AI DeepSeek模型\n",
        }

        assets = service.build_assets_from_fetched(fetched, remote_version="local-test")
        phrases = assets["phrases"]
        concepts = assets["concepts"]
        examples = assets["examples"]
        corpus = assets["corpus"]
        candidates = assets["candidates"]

        from services.jargon.holyman_assets import content_entries

        entries = content_entries(phrases)
        self.assertIn("v我50", entries)
        self.assertNotIn("你好。", entries)
        self.assertFalse(any(v.get("kind") == "corpus_frequency" for v in entries.values() if isinstance(v, dict)))
        self.assertTrue(any(c["title"] == "真诚是最大的弱点" for c in concepts))
        self.assertTrue(any("你说得对" in e["text"] for e in examples))
        self.assertTrue(any(item["text"] == "v我50 今天疯狂星期四 v我50" for item in corpus))
        self.assertTrue(all(c["status"] == "pending_review" for c in candidates))
        self.assertEqual(assets["quality_report"]["status"], "ready")

    def test_curated_phrases_do_not_include_skill_directives_or_concepts(self):
        from services.jargon.sync import HolymanSyncService
        from services.jargon.holyman_assets import content_entries

        service = HolymanSyncService(assets_dir=tempfile.mkdtemp())
        fetched = {
            "神人.skill/SKILL.md": """
### Core Rules
- 存在方式: 用梗说话，用段子回复，用复制粘贴表达所有情感
- 默认输出: 50-2000字长文案，使用复制粘贴/模板替换风格
""",
            "神人.skill/_knowledge/gaming.md": """
### Core View
- **游戏即信仰**: 游戏不是娱乐，是身份
- **所有游戏都可以被缝合**: 跨游戏缝合是最高级的幽默
""",
            "神人.skill/_quotes/iconic.md": "> 疯狂星期四 v我50\n",
            "神言.txt": "v我50 疯狂星期四\n",
        }

        entries = content_entries(service._parse_curated_phrases(fetched, existing_phrases={}))

        for noise in ["存在方式", "默认输出", "游戏即信仰", "游戏即信仰：游戏不是娱乐，是身份", "所有游戏都可以被缝合"]:
            self.assertNotIn(noise, entries)
        for core in ["v我50", "叠甲", "不是哥们", "差不多得了", "疯狂星期四"]:
            self.assertIn(core, entries)

    def test_curated_phrases_do_not_inherit_legacy_corpus_quotes(self):
        from services.jargon.sync import HolymanSyncService
        from services.jargon.holyman_assets import content_entries

        service = HolymanSyncService(assets_dir=tempfile.mkdtemp())
        existing = {
            "梁先生，你的算力从哪里来？": {
                "meaning": "神言语料中的高频/标志性表达，用于理解群聊抽象语境。",
                "category": "corpus",
                "source": "神言.txt",
                "kind": "corpus_quote",
            },
            "人工保留词": {
                "meaning": "人工审核确认的安全黑话表达。",
                "category": "manual",
                "source": "manual_review",
                "kind": "curated_phrase",
            },
        }

        entries = content_entries(service._parse_curated_phrases({}, existing_phrases=existing))

        self.assertNotIn("梁先生，你的算力从哪里来？", entries)
        self.assertIn("人工保留词", entries)

    def test_holyman_candidates_have_stable_ids_for_selection(self):
        from services.jargon.sync import HolymanSyncService

        service = HolymanSyncService(assets_dir=tempfile.mkdtemp())
        corpus = [
            {"text": "你说得对 你说得对 要么加入 要么落后"},
            {"text": "你说得对 要么加入 要么落后"},
        ]

        candidates = service._generate_candidates(corpus, {})

        self.assertTrue(candidates)
        self.assertTrue(all(candidate.get("id") for candidate in candidates), candidates[:3])
        self.assertEqual(len({candidate["id"] for candidate in candidates}), len(candidates))

    def test_jargon_context_with_anchor_returns_messages_not_review_payload(self):
        import asyncio
        from webui.blueprints import jargon as jargon_bp_mod

        class _Cursor:
            def __init__(self, rows=None):
                self._rows = rows or []
            def fetchall(self):
                return self._rows
            def fetchone(self):
                return self._rows[0] if self._rows else None

        class _Conn:
            def execute(self, sql, params=()):
                if "sqlite_master" in sql:
                    return _Cursor([(1,)])
                if "PRAGMA table_info(jargon)" in sql:
                    cols = ["id", "word", "meaning", "group_id", "contexts", "source_memory_id", "source_message_ts", "source_sender_id", "source_context", "candidate_type"]
                    return _Cursor([(idx, name) for idx, name in enumerate(cols)])
                if "FROM jargon WHERE id" in sql:
                    return _Cursor([(7, "v我50", "疯狂星期四转折", "g1", "[]", 11, 1000.0, "u1", "[]", "jargon")])
                if "FROM memories" in sql and "WHERE id = ?" in sql:
                    return _Cursor([(11, "g1", "u1", "用户", "今天疯狂星期四 v我50", 1000.0)])
                if "timestamp <" in sql:
                    return _Cursor([(10, "g1", "u2", "路人", "铺垫", 990.0)])
                if "timestamp >" in sql:
                    return _Cursor([(12, "g1", "u3", "路人", "回应", 1010.0)])
                return _Cursor([])
            def commit(self):
                pass

        class _DB:
            def __init__(self): self.conn = _Conn()
        class _Container:
            def __init__(self): self.db = _DB()

        original_container = jargon_bp_mod.get_container
        original_request = jargon_bp_mod.request
        jargon_bp_mod.get_container = lambda: _Container()
        jargon_bp_mod.request = types.SimpleNamespace(args={"before": "1", "after": "1"})
        try:
            data = asyncio.run(jargon_bp_mod.get_jargon_context(7))
            self.assertTrue(data["ok"])
            self.assertEqual(data["jargon"]["word"], "v我50")
            self.assertEqual([m["role"] for m in data["messages"]], ["before", "anchor", "after"])
            self.assertFalse(data["used_fallback"])
        finally:
            jargon_bp_mod.get_container = original_container
            jargon_bp_mod.request = original_request

    def test_holyman_candidate_review_and_blocklist_api_helpers(self):
        import asyncio
        from webui.blueprints import jargon as jargon_bp_mod

        class _Row:
            def __init__(self, values):
                self.values = values
            def __getitem__(self, idx):
                return self.values[idx]

        class _Conn:
            def __init__(self):
                self.rows = {
                    "candidates": [_Row((1, "外层词", "需要判断", 2, "神言.txt", "pending_review", ""))],
                    "blocklist": [],
                }
            def execute(self, sql, params=()):
                if "FROM jargon_candidates" in sql and "ORDER BY" in sql:
                    return self
                if "FROM jargon_candidates" in sql:
                    return self
                if "FROM jargon_blocklist" in sql:
                    return self
                if "INSERT OR REPLACE INTO jargon_blocklist" in sql:
                    self.rows["blocklist"].append(params)
                    return self
                if "UPDATE jargon_candidates SET status = 'approved'" in sql:
                    self.rows["candidates"][0] = _Row((1, "外层词", "需要判断", 2, "神言.txt", "approved", ""))
                    return self
                if "UPDATE jargon_candidates SET status = 'rejected'" in sql:
                    self.rows["candidates"][0] = _Row((1, "外层词", "需要判断", 2, "神言.txt", "rejected", 'manual_reject'))
                    return self
                return self
            def fetchall(self):
                return self.rows["candidates"] if self.rows["candidates"] else []
            def fetchone(self):
                return self.rows["candidates"][0] if self.rows["candidates"] else None
            def commit(self):
                pass

        class _DB:
            def __init__(self):
                self.conn = _Conn()
        class _Container:
            def __init__(self):
                self.db = _DB()
        original = jargon_bp_mod.get_container
        container = _Container()
        jargon_bp_mod.get_container = lambda: container
        try:
            data = asyncio.run(jargon_bp_mod.list_holyman_candidates())
            self.assertIn("items", data)
            self.assertEqual(data["items"][0]["word"], "外层词")
            self.assertEqual(data["items"][0]["status"], "pending_review")

            approve = asyncio.run(jargon_bp_mod.review_holyman_candidate(1, "approve"))
            self.assertEqual(approve, ({"error": {"code": "legacy_mutation_disabled"}}, 410))
            reject = asyncio.run(jargon_bp_mod.review_holyman_candidate(1, "reject"))
            self.assertEqual(reject, ({"error": {"code": "legacy_mutation_disabled"}}, 410))
            self.assertEqual(container.db.conn.rows["candidates"][0][5], "pending_review")
            block = asyncio.run(jargon_bp_mod.holyman_blocklist())
            self.assertTrue("items" in block)
        finally:
            jargon_bp_mod.get_container = original

    def test_holyman_api_ignores_generic_legacy_activation_meaning(self):
        from webui.blueprints.jargon import _normalize_holyman_phrase, _merge_holyman_db_activation

        item = _normalize_holyman_phrase("v我50", {
            "meaning": "长篇铺垫后突然索要 50 元，制造荒诞转折。",
            "category": "catchphrase",
            "source": "curated/core",
            "kind": "curated_phrase",
        })
        _merge_holyman_db_activation(item, {"id": 1, "meaning": "典型语录/表达样本。仅作为理解参考。", "status": "confirmed"})

        self.assertTrue(item["is_activated"])
        self.assertIsNone(item["custom_meaning"])
        self.assertIn("索要 50", item["meaning"])

    def test_holyman_phrase_defaults_to_activated_and_respects_disable(self):
        """修复的开关：无覆盖行=默认启用；inactive/manual_deleted 必须报告为未启用。"""
        from webui.blueprints.jargon import _normalize_holyman_phrase, _merge_holyman_db_activation

        payload = {"meaning": "长篇铺垫后突然索要 50 元，制造荒诞转折。", "layer": "catchphrase", "runtime_match": True}

        default_item = _normalize_holyman_phrase("v我50", payload)
        self.assertTrue(default_item["is_activated"])

        disabled = _normalize_holyman_phrase("v我50", payload)
        _merge_holyman_db_activation(disabled, {"id": 7, "meaning": "", "status": "inactive"})
        self.assertFalse(disabled["is_activated"])

        removed = _normalize_holyman_phrase("v我50", payload)
        _merge_holyman_db_activation(removed, {"id": 8, "meaning": "", "status": "deleted"})
        self.assertFalse(removed["is_activated"])

    def test_get_holyman_reads_activation_from_bot_jargon_not_legacy_table(self):
        """启用状态必须来自 bot_jargon 覆盖层；legacy jargon 表不再是这条链路的存储。"""
        import inspect

        from webui.blueprints import jargon

        source = inspect.getsource(jargon.get_holyman)
        self.assertNotIn("FROM jargon WHERE scope = 'global'", source)
        self.assertIn("bot_jargon", source)

    def test_holyman_candidate_batch_review_word_fallback_is_fail_closed(self):
        from webui.blueprints.jargon import _review_holyman_candidate_ids

        class _Conn:
            def execute(self, *_args, **_kwargs):
                self.fail("Holyman candidate review must not execute SQL")

            def commit(self):
                self.fail("Holyman candidate review must not commit")

        conn = _Conn()
        conn.fail = self.fail
        result = _review_holyman_candidate_ids(
            conn,
            ["holyman-candidate-0001"],
            "reject",
            words=["资产候选词"],
        )

        self.assertEqual(result["error"], "legacy_mutation_disabled")
        self.assertEqual(result["reviewed_count"], 0)
        self.assertEqual(result["blocked_count"], 0)

    def test_holyman_candidate_batch_review_updates_selected_and_blocks_rejected(self):
        import asyncio
        from webui.blueprints import jargon as jargon_bp_mod

        class _Cursor:
            def __init__(self, rows=None, rowcount=0):
                self._rows = rows or []
                self.rowcount = rowcount
            def fetchall(self):
                return self._rows
            def fetchone(self):
                return self._rows[0] if self._rows else None

        class _Conn:
            def __init__(self):
                self.candidates = {
                    1: {"word": "候选甲", "status": "pending_review", "reject_reason": ""},
                    2: {"word": "候选乙", "status": "pending_review", "reject_reason": ""},
                    3: {"word": "候选丙", "status": "approved", "reject_reason": ""},
                }
                self.blocked = []
            def execute(self, sql, params=()):
                if "sqlite_master" in sql:
                    return _Cursor([(1,)])
                if "SELECT id, word, reason, count, source, status, reject_reason FROM jargon_candidates" in sql:
                    ids = params if params else self.candidates.keys()
                    rows = []
                    for candidate_id in ids:
                        item = self.candidates[int(candidate_id)]
                        rows.append((int(candidate_id), item["word"], "疑似词", 1, "神言.txt", item["status"], item["reject_reason"]))
                    return _Cursor(rows)
                if "SELECT id, word, reason, source, created_at FROM jargon_blocklist" in sql:
                    return _Cursor([(idx + 1, word, "manual_reject", "holyman_review", 1234) for idx, word in enumerate(self.blocked)])
                if "SELECT word, reason FROM jargon_blocklist" in sql:
                    return _Cursor([(word, "manual_reject") for word in self.blocked])
                if "SELECT id, word, meaning, status FROM jargon" in sql:
                    return _Cursor([])
                if "UPDATE jargon_candidates SET status = 'approved'" in sql:
                    ids = params[1:]
                    for candidate_id in ids:
                        self.candidates[int(candidate_id)]["status"] = "approved"
                    return _Cursor(rowcount=len(ids))
                if "UPDATE jargon_candidates SET status = 'rejected'" in sql:
                    ids = params[1:]
                    for candidate_id in ids:
                        self.candidates[int(candidate_id)]["status"] = "rejected"
                        self.candidates[int(candidate_id)]["reject_reason"] = "manual_reject"
                    return _Cursor(rowcount=len(ids))
                if "INSERT OR IGNORE INTO jargon_blocklist" in sql:
                    self.blocked.append(params[0])
                    return _Cursor(rowcount=1)
                return _Cursor([])
            def commit(self):
                pass

        class _DB:
            def __init__(self):
                self.conn = _Conn()
        class _Container:
            def __init__(self):
                self.db = _DB()

        container = _Container()
        original_container = jargon_bp_mod.get_container
        original_request = jargon_bp_mod.request
        jargon_bp_mod.get_container = lambda: container
        try:
            async def _approve_json(*args, **kwargs):
                return {"ids": [1, 2], "action": "approve"}
            jargon_bp_mod.request = types.SimpleNamespace(get_json=_approve_json)
            approved = asyncio.run(jargon_bp_mod.batch_review_holyman_candidates())
            self.assertEqual(approved, ({"error": {"code": "legacy_mutation_disabled"}}, 410))
            self.assertEqual(container.db.conn.candidates[1]["status"], "pending_review")
            self.assertEqual(container.db.conn.candidates[2]["status"], "pending_review")
            self.assertEqual(container.db.conn.blocked, [])

            async def _reject_json(*args, **kwargs):
                return {"ids": [2, 3], "action": "reject"}
            jargon_bp_mod.request = types.SimpleNamespace(get_json=_reject_json)
            rejected = asyncio.run(jargon_bp_mod.batch_review_holyman_candidates())
            self.assertEqual(rejected, ({"error": {"code": "legacy_mutation_disabled"}}, 410))
            self.assertEqual(container.db.conn.candidates[2]["status"], "pending_review")
            self.assertEqual(container.db.conn.candidates[3]["status"], "approved")
            self.assertEqual(container.db.conn.blocked, [])
        finally:
            jargon_bp_mod.get_container = original_container
            jargon_bp_mod.request = original_request

    def test_holyman_api_contract_has_layered_tabs_search_and_batch_candidate_data(self):
        import asyncio
        from webui.blueprints import jargon as jargon_bp_mod

        class _Cursor:
            def __init__(self, rows=None):
                self._rows = rows or []
            def fetchall(self):
                return self._rows
            def fetchone(self):
                return self._rows[0] if self._rows else None

        class _Conn:
            def execute(self, sql, params=()):
                if "sqlite_master" in sql:
                    return _Cursor([])
                return _Cursor([])

        class _DB:
            def __init__(self): self.conn = _Conn()
        class _Container:
            def __init__(self): self.db = _DB()

        original_container = jargon_bp_mod.get_container
        jargon_bp_mod.get_container = lambda: _Container()
        try:
            data = asyncio.run(jargon_bp_mod.get_holyman())
        finally:
            jargon_bp_mod.get_container = original_container

        self.assertEqual(data["asset_type"], "global_jargon_reference")
        self.assertEqual(data["runtime_policy"], "understanding_only")
        self.assertIn("layers", data)
        self.assertEqual(
            set(data["layers"].keys()),
            {"catchphrases", "concepts", "quotes_knowledge", "corpus", "candidates", "blocked"},
        )
        self.assertIn("categories", data)
        self.assertIn("candidates", data)
        self.assertIn("blocked", data)
        self.assertIn("corpus_counts", data)
        self.assertTrue(any(item.get("layer") == "catchphrase" for item in data["items"]))
        self.assertTrue(all("category_label" in item for item in data["items"]))
        self.assertTrue(data["layers"]["corpus"]["reference_only"])

    def test_global_jargon_reference_runtime_layer_is_explicit_and_conservative(self):
        from services.jargon.holyman_assets import content_entries, CORE_CURATED_PHRASES

        phrases = json.loads(Path("assets/holyman/phrases.json").read_text(encoding="utf-8"))
        entries = content_entries(phrases)
        runtime_words = {word for word, value in entries.items() if isinstance(value, dict) and value.get("runtime_match") is True}
        core_runtime = {"你说得对，但是", "不是哥们", "差不多得了", "动了XX的蛋糕", "别急", "那咋了", "又幻想了", "v我50", "疯狂星期四", "叠甲"}

        self.assertTrue(core_runtime.issubset(runtime_words))
        # 全量黑词黑句必须等于 CORE_CURATED_PHRASES，杜绝无节制噪音混入
        self.assertEqual(runtime_words, set(CORE_CURATED_PHRASES.keys()))
        for word in runtime_words:
            self.assertEqual(entries[word].get("layer"), "catchphrase")
            self.assertIs(entries[word].get("reference_only"), True)
        self.assertFalse(entries.get("就很……你们懂吧？[捂脸]", {}).get("runtime_match"))

    def test_persona_skill_markers_block_quality_report(self):
        from services.jargon.sync import HolymanSyncService

        service = HolymanSyncService(assets_dir=tempfile.mkdtemp())
        assets = {
            "manifest": {"files": [{"path": path, "parse_status": "ok"} for path in [
                "神人.skill/SKILL.md",
                "神人.skill/_persona/communication.md",
                "神人.skill/_persona/values.md",
                "神人.skill/_knowledge/gaming.md",
                "神人.skill/_knowledge/internet-culture.md",
                "神人.skill/_quotes/iconic.md",
            ]]},
            "phrases": {
                "默认输出": {"meaning": "50-2000字长文案", "kind": "curated_phrase", "layer": "catchphrase", "runtime_match": True},
                "Core Rules": {"meaning": "persona 指令", "kind": "curated_phrase", "layer": "catchphrase", "runtime_match": True},
            },
            "concepts": [],
            "examples": [],
            "corpus": [],
            "candidates": [],
            "blocked": {},
            "raw_sources": {"README.md": "神言.txt（365条）", "神言.txt": json.dumps({"items": []}, ensure_ascii=False)},
        }

        report = service._build_quality_report(assets)

        self.assertNotEqual(report["status"], "ready")
        self.assertGreater(report["errors"].get("persona_instruction_in_phrases", 0), 0)

    def test_corpus_count_audit_distinguishes_declared_source_and_parsed_counts(self):
        from services.jargon.sync import HolymanSyncService

        service = HolymanSyncService(assets_dir=tempfile.mkdtemp())
        fetched = {
            "README.md": "神言.txt（365条，~32万字）",
            "神人.skill/SKILL.md": "### Catchphrases\n- 别急\n",
            "神人.skill/_persona/communication.md": "### Expression DNA\n只读。",
            "神人.skill/_persona/values.md": "### Values Priority\n只读。",
            "神人.skill/_knowledge/gaming.md": "### 游戏文化\n只读。",
            "神人.skill/_knowledge/internet-culture.md": "### 抽象话术\n只读。",
            "神人.skill/_quotes/iconic.md": "> 别急\n",
            "神言.txt": json.dumps({"items": [{"text": "样本一"}, {"text": "样本二"}]}, ensure_ascii=False),
        }

        assets = service.build_assets_from_fetched(fetched, remote_version="local-test")
        report = assets["quality_report"]

        self.assertEqual(report.get("declared_corpus_count"), 365)
        self.assertEqual(report.get("source_corpus_items_count"), 2)
        self.assertEqual(report.get("parsed_corpus_count"), 2)
        self.assertTrue(report.get("corpus_count_mismatch"))
        self.assertIn("declares 365", report.get("corpus_count_note", ""))
        self.assertEqual(report["status"], "ready")

    def test_holyman_reference_only_matches_runtime_catchphrases(self):
        from services.jargon.holyman_reference import HolymanReference

        ref = HolymanReference()
        ref._phrases = {
            "别急": {"meaning": "提醒对方不要急于反应。", "layer": "catchphrase", "runtime_match": True},
            "动了XX的蛋糕": {"meaning": "反串阴谋化解释。", "layer": "catchphrase", "runtime_match": True},
            "Expression DNA": {"meaning": "文化表达概念参考", "layer": "concept", "runtime_match": False},
        }
        ref._examples = ["Expression DNA 是只读文化概念参考。"]
        ref._blocked = {}

        self.assertTrue(ref.match("别急")["matched"])
        self.assertFalse(ref.match("Expression DNA")["matched"])
        self.assertFalse(ref.match("动了")["matched"])
        self.assertTrue(ref.match("动了游戏厂商的蛋糕")["matched"])

    def test_holyman_api_returns_global_jargon_reference_metadata(self):
        import asyncio
        from webui.blueprints import jargon as jargon_bp_mod

        class _Cursor:
            def __init__(self, rows=None):
                self._rows = rows or []
            def fetchall(self):
                return self._rows
            def fetchone(self):
                return self._rows[0] if self._rows else None

        class _Conn:
            def execute(self, sql, params=()):
                if "sqlite_master" in sql:
                    return _Cursor([])
                return _Cursor([])

        class _DB:
            def __init__(self): self.conn = _Conn()
        class _Container:
            def __init__(self): self.db = _DB()

        original_container = jargon_bp_mod.get_container
        jargon_bp_mod.get_container = lambda: _Container()
        try:
            data = asyncio.run(jargon_bp_mod.get_holyman())
        finally:
            jargon_bp_mod.get_container = original_container

        self.assertEqual(data["asset_type"], "global_jargon_reference")
        self.assertEqual(data["runtime_policy"], "understanding_only")
        self.assertIn("layers", data)
        self.assertIn("corpus_counts", data)

    def test_holyman_asset_contract_uses_global_jargon_reference_language(self):
        phrases = json.loads(Path("assets/holyman/phrases.json").read_text(encoding="utf-8"))
        concepts = json.loads(Path("assets/holyman/concepts.json").read_text(encoding="utf-8"))
        examples = json.loads(Path("assets/holyman/examples.json").read_text(encoding="utf-8"))

        self.assertTrue(any(item.get("layer") == "catchphrase" for item in phrases.values() if isinstance(item, dict)))
        self.assertTrue(any("summary" in item and item.get("layer") == "concept" for item in concepts if isinstance(item, dict)))
        self.assertTrue(any(item.get("reference_only") is True for item in examples if isinstance(item, dict)))
        self.assertTrue(any(item.get("runtime_match") is True for item in phrases.values() if isinstance(item, dict)))
        self.assertTrue(all(item.get("runtime_match") is not True for item in concepts if isinstance(item, dict)))
        self.assertTrue(all(item.get("layer") != "persona" for item in concepts if isinstance(item, dict)))

    def _jargon_conn(self):
        import sqlite3
        conn = sqlite3.connect(":memory:")
        self.addCleanup(conn.close)
        conn.execute(
            """CREATE TABLE jargon (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                word TEXT,
                meaning TEXT,
                is_jargon INTEGER,
                status TEXT,
                frequency INTEGER,
                confidence REAL,
                is_global INTEGER,
                group_id TEXT,
                contexts TEXT,
                created_at INTEGER,
                updated_at INTEGER,
                scope TEXT,
                source TEXT
            )"""
        )
        return conn

    def test_holyman_toggle_rejects_legacy_global_activation(self):
        import asyncio
        import types
        from webui.blueprints import jargon

        conn = self._jargon_conn()
        old_container = jargon.get_container
        old_request = jargon.request

        class _DB:
            def __init__(self, conn):
                self.conn = conn

        async def _get_json():
            return {"word": "v我50", "meaning": "长篇铺垫后突然索要 50 元。", "activate": True}

        jargon.get_container = lambda: types.SimpleNamespace(db=_DB(conn))
        jargon.request = types.SimpleNamespace(get_json=_get_json)
        self.addCleanup(lambda: setattr(jargon, "get_container", old_container))
        self.addCleanup(lambda: setattr(jargon, "request", old_request))

        result = asyncio.run(jargon.toggle_holyman.__wrapped__())

        self.assertEqual(result, ({"error": {"code": "legacy_mutation_disabled"}}, 410))
        row = conn.execute("SELECT word, source, scope, status FROM jargon WHERE word = ?", ("v我50",)).fetchone()
        self.assertIsNone(row)

    def test_get_holyman_returns_corpus_items_for_readonly_display(self):
        import asyncio
        import types
        from webui.blueprints import jargon

        conn = self._jargon_conn()
        old_container = jargon.get_container
        old_fetch = jargon._fetch_github_commit_info_sync

        class _DB:
            def __init__(self, conn):
                self.conn = conn

        jargon.get_container = lambda: types.SimpleNamespace(db=_DB(conn))
        jargon._fetch_github_commit_info_sync = lambda: "remote-test"
        self.addCleanup(lambda: setattr(jargon, "get_container", old_container))
        self.addCleanup(lambda: setattr(jargon, "_fetch_github_commit_info_sync", old_fetch))

        payload = asyncio.run(jargon.get_holyman.__wrapped__())

        self.assertIn("corpus", payload)
        self.assertGreater(len(payload["corpus"]), 0)
        self.assertEqual(payload["corpus"][0]["layer"], "corpus")
        self.assertTrue(payload["corpus"][0]["reference_only"])
        self.assertFalse(payload["corpus"][0]["safe_for_prompt"])
        self.assertTrue(payload["layers"]["corpus"]["reference_only"])

    def test_holyman_sync_preview_compares_remote_without_writing_assets(self):
        from services.jargon.sync import HolymanSyncService

        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp)
            (assets_dir / "phrases.json").write_text(json.dumps({
                "v我50": {
                    "meaning": "旧解释",
                    "category": "catchphrase",
                    "layer": "catchphrase",
                    "runtime_match": True,
                },
                "_version": "sync-old",
                "_content_hash": "oldhash",
            }, ensure_ascii=False), encoding="utf-8")
            fetched = {
                "神人.skill/SKILL.md": "\"v我50\"\n",
                "神人.skill/_knowledge/gaming.md": "### 游戏传教\n围绕游戏身份展开夸张表达。\n",
                "神人.skill/_knowledge/internet-culture.md": "### 抽象话术\n用反串和复读制造幽默。\n",
                "神人.skill/_persona/communication.md": "### 复制粘贴模式\n用长文案表达。\n",
                "神人.skill/_persona/values.md": "### 真诚是弱点\n直接表达会被视为不设防。\n",
                "神人.skill/_quotes/iconic.md": "> 疯狂星期四 v我50\n",
                "神言.txt": "v我50 今天疯狂星期四\n",
            }
            service = HolymanSyncService(assets_dir=assets_dir)

            preview = service.build_sync_preview_from_fetched(fetched, remote_version="remote-new")

            self.assertEqual(preview["local_version"], "sync-old")
            self.assertEqual(preview["remote_version"], "remote-new")
            self.assertTrue(preview["will_update"])
            self.assertGreaterEqual(preview["remote_counts"]["corpus"], 1)
            self.assertFalse((assets_dir / "corpus.json").exists())

    def test_holyman_update_check_reports_remote_difference_and_cache(self):
        import asyncio
        from webui.blueprints import jargon

        with tempfile.TemporaryDirectory() as tmp:
            assets_dir = Path(tmp)
            (assets_dir / "phrases.json").write_text(json.dumps({
                "v我50": {
                    "meaning": "长篇铺垫后突然索要 50 元。",
                    "category": "catchphrase",
                    "layer": "catchphrase",
                    "runtime_match": True,
                },
                "_version": "sync-local",
                "_content_hash": "localhash",
                "_content_count": 1,
                "_remote_commit_version": "remote-old",
            }, ensure_ascii=False), encoding="utf-8")
            (assets_dir / "quality_report.json").write_text(json.dumps({"status": "ready"}), encoding="utf-8")

            old_resolve = jargon._resolve_holyman_assets_dir
            old_fetch = jargon._fetch_github_commit_info_sync
            old_cache = getattr(jargon, "_HOLYMAN_UPDATE_CHECK_CACHE", None)
            jargon._resolve_holyman_assets_dir = lambda: assets_dir
            jargon._fetch_github_commit_info_sync = lambda: "remote-new"
            jargon._HOLYMAN_UPDATE_CHECK_CACHE = None
            self.addCleanup(lambda: setattr(jargon, "_resolve_holyman_assets_dir", old_resolve))
            self.addCleanup(lambda: setattr(jargon, "_fetch_github_commit_info_sync", old_fetch))
            self.addCleanup(lambda: setattr(jargon, "_HOLYMAN_UPDATE_CHECK_CACHE", old_cache))

            first = asyncio.run(jargon._check_holyman_update(force=True))
            self.assertTrue(first["ok"])
            self.assertFalse(first["cached"])
            self.assertTrue(first["has_update"])
            self.assertTrue(first["is_update_available"])
            self.assertEqual(first["local_version"], "sync-local")
            self.assertEqual(first["remote_version"], "remote-new")
            self.assertEqual(first["remote_commit_version"], "remote-old")
            self.assertIn("checked_at", first)

            jargon._fetch_github_commit_info_sync = lambda: "remote-even-newer"
            cached = asyncio.run(jargon._check_holyman_update(force=False))
            self.assertTrue(cached["cached"])
            self.assertEqual(cached["remote_version"], "remote-new")

            refreshed = asyncio.run(jargon._check_holyman_update(force=True))
            self.assertFalse(refreshed["cached"])
            self.assertEqual(refreshed["remote_version"], "remote-even-newer")


if __name__ == "__main__":
    unittest.main()
