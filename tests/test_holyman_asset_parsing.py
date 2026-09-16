"""Holyman 资产解析：知识文档层级与代码围栏语录必须被真实抽取。

回归背景：这两处解析缺失导致 `gaming.md` / `internet-culture.md` 解析出 0 条，
`internal.md` 74 行只抽出 1 条——manifest 记录 ok，内容却在管道里丢失。
"""

from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path

if "astrbot.api" not in sys.modules:
    _astrbot_mod = types.ModuleType("astrbot")
    _api_mod = types.ModuleType("astrbot.api")

    class _Logger:
        def debug(self, *args, **kwargs): pass
        def info(self, *args, **kwargs): pass
        def warning(self, *args, **kwargs): pass

    _api_mod.logger = _Logger()
    sys.modules["astrbot"] = _astrbot_mod
    sys.modules["astrbot.api"] = _api_mod

from services.jargon.holyman_assets import parse_concepts, parse_examples


class KnowledgeDocLevelTest(unittest.TestCase):
    """A1：知识文档用 `## ` 二级标题，解析器必须认得。"""

    def test_parse_concepts_accepts_h2_heading(self):
        fetched = {
            "神人.skill/_knowledge/gaming.md": (
                "# 互联网抽象社群 — 游戏观\n"
                "\n"
                "## Core Views\n"
                "游戏是身份认同而非娱乐产品。\n"
                "\n"
                "## Decision Logic\n"
                "遇到游戏话题优先传教。\n"
            ),
        }

        concepts = parse_concepts(fetched)

        titles = [item["title"] for item in concepts]
        self.assertIn("Core Views", titles)
        self.assertIn("Decision Logic", titles)
        self.assertTrue(all(item["source"] == "神人.skill/_knowledge/gaming.md" for item in concepts))

    def test_parse_concepts_accepts_h3_heading(self):
        fetched = {"神人.skill/_persona/rules.md": "### 群聊对线\n用文案轰炸。\n"}

        concepts = parse_concepts(fetched)

        self.assertEqual([item["title"] for item in concepts], ["群聊对线"])

    def test_parse_concepts_never_marks_runtime_match(self):
        fetched = {"神人.skill/_knowledge/gaming.md": "## Core Views\n游戏身份认同。\n"}

        for item in parse_concepts(fetched):
            self.assertFalse(item["runtime_match"])
            self.assertTrue(item["reference_only"])
            self.assertEqual(item["layer"], "concept")


class FencedQuoteTest(unittest.TestCase):
    """A2：internal.md 的语录包在 ``` 代码围栏里，必须被抽取。"""

    def test_parse_examples_reads_fenced_quotes(self):
        fetched = {
            "神人.skill/_quotes/internal.md": (
                "# 私下语录\n"
                "\n"
                "## 罕见真诚\n"
                "\n"
                "```\n"
                '"？"\n'
                "```\n"
                "单字问号，最短的一条。\n"
                "\n"
                "```\n"
                '"睡不着"\n'
                "```\n"
                "深夜的单字发言。\n"
            ),
        }

        examples = parse_examples(fetched, {})

        texts = [item["text"] for item in examples]
        self.assertTrue(any("？" in text for text in texts), texts)
        self.assertTrue(any("睡不着" in text for text in texts), texts)
        self.assertGreaterEqual(len(examples), 2)

    def test_parse_examples_never_marks_runtime_match(self):
        fetched = {"神人.skill/_quotes/iconic.md": "> 你说得对，但是《原神》是一款……\n"}

        examples = parse_examples(fetched, {})
        self.assertEqual(len(examples), 1)
        for item in examples:
            self.assertFalse(item["runtime_match"])
            self.assertTrue(item["reference_only"])


class LocalRawExampleTest(unittest.TestCase):
    """使用仓库内真实上游文档，防止抽象夹具掩盖章节误分类。"""

    @classmethod
    def setUpClass(cls):
        raw = Path(__file__).resolve().parents[1] / "assets" / "holyman" / "raw"
        cls.fetched = {
            source: (raw / source).read_text(encoding="utf-8")
            for source in (
                "神人.skill/_persona/communication.md",
                "神人.skill/_quotes/iconic.md",
                "神人.skill/_quotes/internal.md",
            )
        }
        cls.examples = parse_examples(cls.fetched, {})

    def test_signature_pattern_fence_is_reference_only_template(self):
        templates = [e for e in self.examples if e["example_type"] == "template"]
        self.assertEqual(len(templates), 10)
        self.assertTrue(any("《X》是由Y" in e["text"] for e in templates))
        self.assertTrue(any('"……v我50"（万能结尾）' == e["text"] for e in templates))
        for item in templates:
            self.assertFalse(item["safe_for_prompt"])
            self.assertFalse(item["runtime_match"])
            self.assertTrue(item["reference_only"])

    def test_document_introductions_are_not_examples(self):
        introductions = (
            "结构化语言基因。定义这个集体人格最底层的语言特征。",
            "这些是在表演性文案间隙中泄露的真实声音。",
            "筛选标准：反映思维方式和价值观",
            "展示在不同场景下如何用抽象文化回应。",
        )
        for intro in introductions:
            self.assertTrue(any(intro in text for text in self.fetched.values()))
            self.assertFalse(any(intro in item["text"] for item in self.examples))

    def test_real_sentence_samples_are_not_templates(self):
        communication = [e for e in self.examples if e["source"].endswith("communication.md")]
        for text in (
            "你说得对，但是《原神》是由米哈游自主研发的一款全新开放世界冒险游戏……",
            "睡不着",
            "Ciallo～(∠・ω< )⌒★",
        ):
            item = next(e for e in communication if e["text"] == text)
            self.assertEqual(item["example_type"], "voice_sample")
            self.assertTrue(item["safe_for_prompt"])

    def test_dialogue_inherits_parent_section_and_char_continuation(self):
        dialogue = [e for e in self.examples if e["example_type"] == "dialogue"]
        # 原始 5 段 Char 中的猫娘化回复仍由既有身份安全过滤器排除，另有一条后续回复。
        self.assertEqual(len(dialogue), 5)
        self.assertFalse(any("哥哥不介意就好喵" in e["text"] for e in self.examples))
        followup = next(e for e in self.examples if e["text"] == "开玩笑的。别往心里去。")
        self.assertEqual(followup["example_type"], "dialogue")
        self.assertEqual(followup["attribution"], "Example Exchanges (Char)")
        self.assertFalse(any("//" in e["text"] or e["text"] == "今天好累" for e in self.examples))

    def test_numbered_quote_keeps_attribution_and_inner_quotes(self):
        item = next(e for e in self.examples if e["text"].startswith("别急是一种态度"))
        self.assertEqual(item["example_type"], "voice_sample")
        self.assertIn("学会'别急'", item["text"])
        self.assertEqual(item["attribution"], '将"别急"两个字写成一篇《人民日报》评论员文章——最抽象的严肃')
        self.assertFalse(any(e["text"].startswith("—") for e in self.examples))

    def test_internal_fenced_short_quotes_survive_intro_filter(self):
        items = [e for e in self.examples if e["source"].endswith("internal.md")]
        for text in ("？", "原神好玩吗", "睡不着"):
            item = next(e for e in items if e["text"] == text)
            self.assertEqual(item["example_type"], "voice_sample")
            self.assertTrue(item["safe_for_prompt"])

    def test_unknown_section_does_not_inherit_previous_sample_block(self):
        source = "神人.skill/_persona/communication.md"
        text = self.fetched[source] + '\n## 附加说明\n```\n"这里只是文档说明，不是真实发言。"\n```\n'
        self.assertFalse(any("这里只是文档说明" in e["text"] for e in parse_examples({source: text}, {})))


if __name__ == "__main__":
    unittest.main()
