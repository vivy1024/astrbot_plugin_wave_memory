import sqlite3
import sys
import types
import unittest


if "astrbot.api" not in sys.modules:
    astrbot_mod = types.ModuleType("astrbot")
    api_mod = types.ModuleType("astrbot.api")

    class _Logger:
        def debug(self, *args, **kwargs): pass
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass
        def error(self, *args, **kwargs): pass

    api_mod.logger = _Logger()
    sys.modules["astrbot"] = astrbot_mod
    sys.modules["astrbot.api"] = api_mod


class _DB:
    def __init__(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, group_id TEXT, sender_id TEXT, sender_name TEXT, content TEXT, timestamp REAL, memory_type TEXT DEFAULT 'message')")
        self.conn.execute("CREATE TABLE user_profiles (user_id TEXT, group_id TEXT, bot_id TEXT, nickname TEXT)")

    def close(self):
        self.conn.close()


class V430JargonCandidateRoutingTest(unittest.TestCase):
    def setUp(self):
        from services.jargon.service import JargonService

        self.db = _DB()
        self.service = JargonService(self.db, enabled=True, config={"min_frequency": 1})

    def tearDown(self):
        self.db.close()

    def test_known_sender_name_routes_to_person_alias_without_llm(self):
        self.db.conn.execute(
            "INSERT INTO memories (group_id, sender_id, sender_name, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            ("g1", "u1", "阿洛", "阿洛 今天来了", 1000.0),
        )

        route = self.service.classify_candidate("阿洛", "g1", {"sender_id": "u1", "sender_name": "阿洛"})

        self.assertEqual(route["candidate_type"], "person_alias")
        self.assertFalse(route["enter_llm"])
        self.assertEqual(route["reject_reason"], "person_alias_diverted")

    def test_known_user_profile_nickname_routes_to_person_alias_without_llm(self):
        self.db.conn.execute(
            "INSERT INTO user_profiles (user_id, group_id, bot_id, nickname) VALUES (?, ?, ?, ?)",
            ("u2", "g1", "yushu", "小羽"),
        )

        route = self.service.classify_candidate("小羽", "g1", {"sender_id": "u2"})

        self.assertEqual(route["candidate_type"], "person_alias")
        self.assertFalse(route["enter_llm"])

    def test_technical_noise_routes_to_audit_without_llm(self):
        route = self.service.classify_candidate("object", "g1", {"sender_id": "u1"})

        self.assertEqual(route["candidate_type"], "technical_noise")
        self.assertFalse(route["enter_llm"])
        self.assertEqual(route["reject_reason"], "technical_noise_filtered")

    def test_ordinary_word_routes_to_audit_without_llm(self):
        route = self.service.classify_candidate("吃饭", "g1", {"sender_id": "u1"})

        self.assertEqual(route["candidate_type"], "ordinary_word")
        self.assertFalse(route["enter_llm"])
        self.assertEqual(route["reject_reason"], "ordinary_word_filtered")

    def test_holyman_phrase_routes_to_reference_hit_without_llm(self):
        route = self.service.classify_candidate("v我50", "g1", {"sender_id": "u1"})

        self.assertEqual(route["candidate_type"], "holyman_reference_hit")
        self.assertFalse(route["enter_llm"])
        self.assertEqual(route["source"], "holyman_skills")
        self.assertTrue(route["reference_only"])
        self.assertIn("理解参考", route["meaning"])

    def test_unknown_candidate_routes_to_local_jargon_candidate_for_llm(self):
        route = self.service.classify_candidate("猫猫税", "g1", {"sender_id": "u1"})

        self.assertEqual(route["candidate_type"], "local_jargon_candidate")
        self.assertTrue(route["enter_llm"])
        self.assertEqual(route["source"], "wave_memory")

    def test_diverted_candidates_never_enter_statistics(self):
        from domain.scope import RuntimeScope, SessionRef
        from services.jargon import statistical_filter

        scope = RuntimeScope("yushu", "group", SessionRef("qq:group:g1", "qq", "group", "g1"))
        original_has_jieba = statistical_filter._HAS_JIEBA
        statistical_filter._HAS_JIEBA = True
        self.addCleanup(setattr, statistical_filter, "_HAS_JIEBA", original_has_jieba)
        self.service._filter._tokenize = lambda text: ["阿洛", "object", "吃饭", "v我50", "猫猫税"]
        self.db.conn.execute(
            "INSERT INTO memories (group_id, sender_id, sender_name, content, timestamp) VALUES (?, ?, ?, ?, ?)",
            ("g1", "u1", "阿洛", "阿洛发言", 1000.0),
        )

        self.service.feed_message("ignored", scope, sender_id="u1", timestamp=1000.0)

        frequencies = self.service._filter._group_freq[("yushu", "qq:group:g1", "group")]
        self.assertEqual(dict(frequencies), {"猫猫税": 1})

    def test_holyman_blocklist_uses_same_runtime_classifier(self):
        self.service._holyman._blocked["v我50"] = "manual block"

        route = self.service.classify_candidate("v我50", "g1", {"sender_id": "u1"})

        self.assertEqual(route["candidate_type"], "local_jargon_candidate")
        self.assertTrue(route["enter_llm"])


if __name__ == "__main__":
    unittest.main()
