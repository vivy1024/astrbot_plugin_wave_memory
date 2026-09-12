"""Holyman 资产解析：知识文档层级与代码围栏语录必须被真实抽取。

回归背景：这两处解析缺失导致 `gaming.md` / `internet-culture.md` 解析出 0 条，
`internal.md` 74 行只抽出 1 条——manifest 记录 ok，内容却在管道里丢失。
"""

from __future__ import annotations

import sys
import types
import unittest

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

        for item in parse_examples(fetched, {}):
            self.assertFalse(item["runtime_match"])
            self.assertTrue(item["reference_only"])


if __name__ == "__main__":
    unittest.main()
