import ast
import asyncio
import copy
import json
import sqlite3
import unittest
from pathlib import Path
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from services.impression_timeline import append_impression, relationship_context


class _FakePlain:
    def __init__(self, text: str):
        self.text = text


class _FakeImage:
    def __init__(self, *args, **kwargs):
        pass


class _FakeResult:
    def __init__(self, chain):
        self.chain = chain


class _FakeEvent:
    def __init__(self, chain=None, *, sender_id="user123", group_id="group456", bot_id="bot789", platform_id="qq"):
        self._result = _FakeResult(chain or [])
        self._sender_id = sender_id
        self._group_id = group_id
        self._bot_id = bot_id
        self._platform_id = platform_id
        self._wave_memory_runtime_scope = RuntimeScope(
            bot_id=bot_id,
            visibility="group",
            session=SessionRef(
                id=f"{platform_id}:group:{group_id}",
                platform_id=platform_id,
                kind="group",
                conversation_id=group_id,
            ),
            subject_principal_id=f"{platform_id}:user:{sender_id}",
        )

    def get_result(self):
        return self._result

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._bot_id


def _load_method(method_name: str):
    source_path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    plugin_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WaveMemoryPlugin"
    )
    method = copy.deepcopy(next(
        node for node in plugin_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == method_name
    ))
    method.decorator_list = []
    method.returns = None
    for argument in (*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs):
        argument.annotation = None

    class _DropComponentImport(ast.NodeTransformer):
        def visit_ImportFrom(self, node):
            if node.module == "astrbot.core.message.components":
                return None
            return node

    method = _DropComponentImport().visit(method)
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))

    namespace = {
        "logger": SimpleNamespace(debug=lambda *args, **kwargs: None, warning=lambda *args, **kwargs: None, info=lambda *args, **kwargs: None),
        "RuntimeScope": RuntimeScope,
        "Plain": _FakePlain,
        "Image": _FakeImage,
        "json": json,
        "time": SimpleNamespace(time=lambda: 1700000000.0),
        "append_impression": append_impression,
        "relationship_context": relationship_context,
    }
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace[method_name]


class ImpressionHookTest(unittest.TestCase):
    def setUp(self):
        self.conn = sqlite3.connect(":memory:")
        self.conn.execute("""
            CREATE TABLE user_profiles (
                user_id TEXT,
                group_id TEXT,
                bot_id TEXT,
                metadata TEXT,
                interaction_count INTEGER DEFAULT 0,
                last_seen REAL DEFAULT 0,
                PRIMARY KEY (user_id, group_id, bot_id)
            )
        """)
        self.conn.commit()
        async def _fake_enqueue(x):
            pass

        self.plugin = SimpleNamespace(
            db=SimpleNamespace(conn=self.conn),
            ignore_bot_messages=False,
            _bot_registry={},
            _get_bot_name=lambda bot_id: "test_bot",
            writer=SimpleNamespace(enqueue=_fake_enqueue),
            self_reflect=None,
            _reply_tracker={},
        )
        self.on_decorating_result = _load_method("on_decorating_result")

    def tearDown(self):
        self.conn.close()

    def test_extract_and_clean_impression_brackets(self):
        # 1. 模拟 LLM 输出末尾携带 <<impression:...>>
        chain = [
            _FakePlain("你好呀，今天天气真不错！\n<<impression:活泼开朗的朋友>>")
        ]
        event = _FakeEvent(chain=chain, sender_id="u1", group_id="g1", bot_id="b1")

        asyncio.run(self.on_decorating_result(self.plugin, event))

        # 验证文本中的 impression 标记被完全清洗
        self.assertEqual(chain[0].text, "你好呀，今天天气真不错！")

        # 验证数据库中写入了画像 impression
        row = self.conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u1' AND group_id='g1' AND bot_id='b1'").fetchone()
        self.assertIsNotNone(row)
        meta = json.loads(row[0])
        self.assertEqual(meta.get("impression"), "活泼开朗的朋友")
        self.assertIn("impression_updated_at", meta)

    def test_extract_and_clean_legacy_impression_square_brackets(self):
        # 2. 模拟兼容 [impression:...] 标记及多行与额外换行
        chain = [
            _FakePlain("这是正文内容。\n\n[impression: 喜欢钻研技术的小伙伴 ]\n")
        ]
        event = _FakeEvent(chain=chain, sender_id="u2", group_id="g1", bot_id="b1")

        asyncio.run(self.on_decorating_result(self.plugin, event))

        # 验证文本清洗干净
        self.assertEqual(chain[0].text, "这是正文内容。")

        # 验证数据库中写入了画像
        row = self.conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u2' AND group_id='g1' AND bot_id='b1'").fetchone()
        self.assertIsNotNone(row)
        meta = json.loads(row[0])
        self.assertEqual(meta.get("impression"), "喜欢钻研技术的小伙伴")

    def test_no_impression_tag_leaves_chain_untouched(self):
        # 3. 正常消息不含 impression 标记
        chain = [
            _FakePlain("这是一条普通消息，不含任何隐藏标记。")
        ]
        event = _FakeEvent(chain=chain, sender_id="u3", group_id="g1", bot_id="b1")

        asyncio.run(self.on_decorating_result(self.plugin, event))

        self.assertEqual(chain[0].text, "这是一条普通消息，不含任何隐藏标记。")
        row = self.conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u3' AND group_id='g1' AND bot_id='b1'").fetchone()
        self.assertIsNone(row)

    def test_impression_in_middle_of_text_cleaned(self):
        # 4. 模拟 LLM 在回复正文中间输出了 impression 标记（非末尾）
        chain = [
            _FakePlain("收到！<<impression:很有礼貌的提问者>> 稍后我会详细为你解答。")
        ]
        event = _FakeEvent(chain=chain, sender_id="u4", group_id="g1", bot_id="b1")

        asyncio.run(self.on_decorating_result(self.plugin, event))

        self.assertEqual(chain[0].text, "收到！稍后我会详细为你解答。")
        row = self.conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u4' AND group_id='g1' AND bot_id='b1'").fetchone()
        self.assertIsNotNone(row)
        meta = json.loads(row[0])
        self.assertEqual(meta.get("impression"), "很有礼貌的提问者")

    def test_existing_profile_updated_safely(self):
        # 5. 用户已有 metadata（如 tags 等），更新 impression 时不覆盖已有字段
        initial_meta = {"tags": {"geek": 1}, "impression": "旧印象"}
        self.conn.execute(
            "INSERT INTO user_profiles (user_id, group_id, bot_id, metadata) VALUES ('u5', 'g1', 'b1', ?)",
            (json.dumps(initial_meta, ensure_ascii=False),)
        )
        self.conn.commit()

        chain = [
            _FakePlain("好的。\n<<impression:最新深入讨论的新印象>>")
        ]
        event = _FakeEvent(chain=chain, sender_id="u5", group_id="g1", bot_id="b1")

        asyncio.run(self.on_decorating_result(self.plugin, event))

        self.assertEqual(chain[0].text, "好的。")
        row = self.conn.execute("SELECT metadata FROM user_profiles WHERE user_id='u5' AND group_id='g1' AND bot_id='b1'").fetchone()
        self.assertIsNotNone(row)
        meta = json.loads(row[0])
        self.assertEqual(meta.get("impression"), "最新深入讨论的新印象")
        self.assertEqual(meta.get("tags"), {"geek": 1})
        self.assertEqual(meta.get("impression_history")[0]["text"], "旧印象")

    def test_impression_persistence_uses_package_relative_import(self):
        source_path = Path(__file__).resolve().parents[1] / "main.py"
        tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
        plugin_class = next(
            node for node in tree.body
            if isinstance(node, ast.ClassDef) and node.name == "WaveMemoryPlugin"
        )
        method = next(
            node for node in plugin_class.body
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_decorating_result"
        )
        module_imports = [
            node for node in tree.body
            if isinstance(node, ast.ImportFrom)
            and any(alias.name == "append_impression" for alias in node.names)
        ]
        self.assertEqual(len(module_imports), 1)
        self.assertEqual(module_imports[0].module, "services.impression_timeline")
        self.assertEqual(module_imports[0].level, 1)
        method_imports = [
            node for node in ast.walk(method)
            if isinstance(node, ast.ImportFrom)
            and any(alias.name == "append_impression" for alias in node.names)
        ]
        self.assertEqual(method_imports, [])
        assigned: list[str] = []
        for node in ast.walk(method):
            if not isinstance(node, ast.Assign) or not isinstance(node.value, ast.Call):
                continue
            func = node.value.func
            if not isinstance(func, ast.Name) or func.id != "relationship_context":
                continue
            target = node.targets[0]
            names = target.elts if isinstance(target, ast.Tuple) else [target]
            assigned.extend(item.id for item in names if isinstance(item, ast.Name))
        self.assertIn("event_anchor", assigned)
        self.assertNotIn("event", assigned)


if __name__ == "__main__":
    unittest.main()
