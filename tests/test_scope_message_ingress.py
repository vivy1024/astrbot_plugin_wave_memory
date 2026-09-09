import ast
import asyncio
import copy
import json
import unittest
from pathlib import Path
from types import SimpleNamespace


class _FakeClock:
    def __init__(self, value=100.0):
        self.value = value

    def time(self):
        self.value += 5.0
        return self.value

    @staticmethod
    def strftime(*args, **kwargs):
        return "12"

    @staticmethod
    def localtime(*args, **kwargs):
        return None


class _FakeLogger:
    def __init__(self):
        self.warnings = []

    def warning(self, message, *args, **kwargs):
        self.warnings.append(str(message))

    def info(self, *args, **kwargs):
        pass

    def debug(self, *args, **kwargs):
        pass


class _FakeWriter:
    def __init__(self):
        self.items = []

    async def enqueue(self, item):
        self.items.append(dict(item))


class _FakeScopedKnowledge:
    def __init__(self):
        self.fact_calls = []

    def upsert_scoped_fact(self, scope, **kwargs):
        self.fact_calls.append((scope, dict(kwargs)))
        return len(self.fact_calls)


class _FakeSelfReflect:
    def __init__(self):
        self.corrections = []
        self.replies = []

    async def check_correction(self, *args, **kwargs):
        self.corrections.append((args, dict(kwargs)))
        return False

    def record_reply(self, *args, **kwargs):
        self.replies.append((args, dict(kwargs)))


class _FakeLifecycle:
    def __init__(self):
        self.calls = []

    def process_scoped_message(self, **kwargs):
        self.calls.append(dict(kwargs))
        return True


class _FakeConcernTracker:
    def __init__(self):
        self.added = []

    def add(self, **kwargs):
        self.added.append(dict(kwargs))


class _FakeSubjectiveTime:
    def __init__(self):
        self.anchors = []

    def add_anchor(self, *args, **kwargs):
        self.anchors.append((args, dict(kwargs)))


class _FakeEvent:
    def __init__(self, *, message="作用域入口测试", sender_id="user-event", group_id="group-event", self_id="bot-event"):
        self.message_str = message
        self._sender_id = sender_id
        self._group_id = group_id
        self._self_id = self_id
        self.message_obj = SimpleNamespace(
            message=[],
            sender=SimpleNamespace(nickname="测试用户"),
        )
        self.is_at_or_wake_command = False
        self.message_id = "message-event"
        self.llm_calls = []
        self.stopped = False

    def get_message_str(self):
        return self.message_str

    def get_sender_id(self):
        return self._sender_id

    def get_group_id(self):
        return self._group_id

    def get_self_id(self):
        return self._self_id

    @staticmethod
    def get_platform_id():
        return "qq"

    @staticmethod
    def get_message_type():
        return SimpleNamespace(value="GroupMessage")

    def should_call_llm(self, value):
        self.llm_calls.append(value)

    def stop_event(self):
        self.stopped = True


class _FakePrivateEvent(_FakeEvent):
    def get_message_type(self):
        return SimpleNamespace(value="FriendMessage")

    def get_session_id(self):
        return "private-event"


class _FakePlain:
    def __init__(self, text):
        self.text = text


class _FakeImage:
    pass


class _FakeBotSentEvent(_FakeEvent):
    def __init__(self, *, reply_text="bot reply", **kwargs):
        super().__init__(**kwargs)
        self._result = SimpleNamespace(chain=[_FakePlain(reply_text)])

    def get_result(self):
        return self._result


class _FakePrivateBotSentEvent(_FakeBotSentEvent):
    def get_message_type(self):
        return SimpleNamespace(value="FriendMessage")

    def get_session_id(self):
        return "private-event"


class _CountingResolver:
    def __init__(self, resolver):
        self.resolver = resolver
        self.calls = []

    def resolve_event(self, event):
        self.calls.append(event)
        return self.resolver.resolve_event(event)


class _FakeJargon:
    def __init__(self):
        self.fed = []
        self.mine_calls = []

    def feed_message(self, *args, **kwargs):
        self.fed.append((args, dict(kwargs)))

    def should_mine(self, runtime_scope):
        return True

    async def mine(self, runtime_scope):
        self.mine_calls.append(runtime_scope)
        return [{"word": "垃圾黑话"}]


class _FakeBeliefEmergence:
    def __init__(self):
        self.calls = []

    async def emerge_recent(self, **kwargs):
        self.calls.append(dict(kwargs))
        return ["pending-belief"]


class _IngressPlugin:
    def __init__(self, resolver):
        from services.inbound_message_handler import InboundMessagePipeline

        self.scope_resolver = resolver
        self.writer = _FakeWriter()
        self.db = SimpleNamespace(scoped_knowledge=_FakeScopedKnowledge())
        self.min_message_length = 1
        self.max_message_length = 2000
        self._bot_qq_ids = []
        self.ignore_bot_messages = False
        self.group_whitelist = []
        self.group_blacklist = []
        self.jargon_service = None
        self.self_reflect = None
        self.lifecycle = None
        self.belief_emergence = None
        self.desire_engine = None
        self.meta_thinking = None
        self.concern_tracker = None
        self.subjective_time = None
        self._scope_resolution_failed_total = {}
        self._scope_resolution_last_warning = {}
        self._spawned = []
        self.inbound_pipeline = InboundMessagePipeline(self)

    def _spawn(self, coro, **kwargs):
        self._spawned.append((coro, dict(kwargs)))
        if hasattr(coro, "close"):
            coro.close()
        return None

    @staticmethod
    def _get_bot(_bot_id):
        return None

    @staticmethod
    def _get_admin_ids():
        return {"admin-event"}


class _BotSentPlugin:
    def __init__(self, resolver):
        self.scope_resolver = resolver
        self.writer = _FakeWriter()
        self.ignore_bot_messages = False
        self._reply_tracker = {}
        self.self_reflect = None
        self._scope_resolution_failed_total = {}

    @staticmethod
    def _get_bot_name(_bot_id):
        return "测试 Bot"

    @staticmethod
    def _get_bot(_bot_id):
        return None


def _load_on_message():
    """只编译 main.py 中真实 on_message，避免测试环境安装完整 AstrBot。"""
    source_path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    plugin_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WaveMemoryPlugin"
    )
    method = copy.deepcopy(next(
        node for node in plugin_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_message"
    ))
    method.decorator_list = []
    method.returns = None
    for argument in (*method.args.posonlyargs, *method.args.args, *method.args.kwonlyargs):
        argument.annotation = None
    module = ast.fix_missing_locations(ast.Module(body=[method], type_ignores=[]))

    logger = _FakeLogger()
    recorded_errors = []
    namespace = {
        "asyncio": asyncio,
        "json": json,
        "logger": logger,
        "time": _FakeClock(),
        "_record_err": lambda source, reason: recorded_errors.append((source, reason)),
    }
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["on_message"], logger, recorded_errors


def _load_on_bot_sent():
    """编译真实 on_bot_sent，并以测试组件替换 AstrBot 的局部组件 import。"""
    source_path = Path(__file__).resolve().parents[1] / "main.py"
    tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))
    plugin_class = next(
        node for node in tree.body
        if isinstance(node, ast.ClassDef) and node.name == "WaveMemoryPlugin"
    )
    method = copy.deepcopy(next(
        node for node in plugin_class.body
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "on_bot_sent"
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
    from domain.scope import RuntimeScope

    logger = _FakeLogger()
    recorded_errors = []
    namespace = {
        "logger": logger,
        "time": _FakeClock(),
        "RuntimeScope": RuntimeScope,
        "Plain": _FakePlain,
        "Image": _FakeImage,
        "_record_err": lambda source, reason: recorded_errors.append((source, reason)),
    }
    exec(compile(module, str(source_path), "exec"), namespace)
    return namespace["on_bot_sent"], logger, recorded_errors


class ScopeMessageIngressTest(unittest.TestCase):
    @staticmethod
    def _resolver(*, self_id="bot-event"):
        from services.scopes import BotIdentityBinding, ScopeResolver

        return _CountingResolver(ScopeResolver([
            BotIdentityBinding(self_id=self_id, db_id="bot-profile", display_name="测试 Bot")
        ]))

    def test_resolution_failure_is_stable_and_enqueues_nothing(self):
        on_message, logger, recorded_errors = _load_on_message()
        resolver = self._resolver(self_id="another-bot")
        plugin = _IngressPlugin(resolver)
        event = _FakeEvent()

        asyncio.run(on_message(plugin, event))

        self.assertEqual(resolver.calls, [event])
        self.assertEqual(plugin.writer.items, [])
        self.assertEqual(plugin._scope_resolution_failed_total, {"unknown_bot_self_id": 1})
        self.assertIn("reason=unknown_bot_self_id", logger.warnings[0])
        self.assertEqual(recorded_errors, [("ScopeResolution", "unknown_bot_self_id")])

    def test_success_resolves_once_and_attaches_scope_to_legacy_payload(self):
        on_message, _, _ = _load_on_message()
        resolver = self._resolver()
        plugin = _IngressPlugin(resolver)
        event = _FakeEvent(sender_id="user-event", group_id="group-event", self_id="bot-event")

        asyncio.run(on_message(plugin, event))

        self.assertEqual(resolver.calls, [event])
        self.assertEqual(len(plugin.writer.items), 1)
        payload = plugin.writer.items[0]
        self.assertEqual(payload["scope"].bot_id, "bot-profile")
        self.assertIs(event._wave_memory_runtime_scope, payload["scope"])
        self.assertEqual(payload["scope"].session.id, "qq:group:group-event")
        self.assertEqual(payload["group_id"], "group-event")
        self.assertEqual(payload["sender_id"], "user-event")
        self.assertEqual(payload["content"], "作用域入口测试")
        self.assertEqual(payload["event_id"], "message-event")
        self.assertFalse(event.stopped)
        self.assertNotIn(False, event.llm_calls)

    def test_duplicate_platform_event_id_stops_second_delivery_before_llm(self):
        on_message, _, _ = _load_on_message()
        resolver = self._resolver()
        plugin = _IngressPlugin(resolver)
        first = _FakeEvent()
        duplicate = _FakeEvent()

        asyncio.run(on_message(plugin, first))
        asyncio.run(on_message(plugin, duplicate))

        self.assertEqual(resolver.calls, [first, duplicate])
        self.assertEqual(len(plugin.writer.items), 1)
        self.assertTrue(duplicate.stopped)
        self.assertIn(False, duplicate.llm_calls)

    def test_same_event_id_in_another_scope_is_not_deduplicated(self):
        on_message, _, _ = _load_on_message()
        resolver = self._resolver()
        plugin = _IngressPlugin(resolver)
        first = _FakeEvent(group_id="group-one")
        other_scope = _FakeEvent(group_id="group-two")

        asyncio.run(on_message(plugin, first))
        asyncio.run(on_message(plugin, other_scope))

        self.assertEqual(len(plugin.writer.items), 2)
        self.assertFalse(other_scope.stopped)
        self.assertNotIn(False, other_scope.llm_calls)

    def test_private_message_enters_exact_memory_writer_without_group_derivations(self):
        on_message, _, recorded_errors = _load_on_message()
        resolver = self._resolver()
        plugin = _IngressPlugin(resolver)
        lifecycle = _FakeLifecycle()
        reflection = _FakeSelfReflect()
        plugin.lifecycle = lifecycle
        plugin.self_reflect = reflection
        event = _FakePrivateEvent()

        asyncio.run(on_message(plugin, event))

        self.assertEqual(resolver.calls, [event])
        self.assertEqual(len(plugin.writer.items), 1)
        payload = plugin.writer.items[0]
        self.assertEqual(payload["scope"].visibility, "private")
        self.assertEqual(payload["scope"].session.id, "qq:private:private-event")
        self.assertEqual(payload["group_id"], "private-event")
        self.assertEqual(payload["sender_id"], "user-event")
        self.assertEqual(payload["content"], "作用域入口测试")
        self.assertEqual(recorded_errors, [])
        self.assertIs(event._wave_memory_runtime_scope, payload["scope"])
        self.assertEqual(lifecycle.calls, [])
        self.assertEqual(reflection.corrections, [])

    def test_self_reflect_receives_event_resolved_scope_without_rederivation(self):
        on_message, _, _ = _load_on_message()
        resolver = self._resolver()
        plugin = _IngressPlugin(resolver)
        recorder = _FakeSelfReflect()
        plugin.self_reflect = recorder
        event = _FakeEvent()

        asyncio.run(on_message(plugin, event))

        self.assertEqual(len(recorder.corrections), 1)
        args, kwargs = recorder.corrections[0]
        self.assertEqual(args[2], "group-event")
        self.assertEqual(kwargs["bot_id"], "bot-profile")
        self.assertIs(kwargs["scope"], event._wave_memory_runtime_scope)
        self.assertEqual(kwargs["scope"].session.id, "qq:group:group-event")

    def test_auto_extract_tasks_are_not_spawned_from_inbound_path(self):
        on_message, _, _ = _load_on_message()
        resolver = self._resolver()
        plugin = _IngressPlugin(resolver)
        plugin.lifecycle = _FakeLifecycle()
        plugin.jargon_service = _FakeJargon()
        plugin.belief_emergence = _FakeBeliefEmergence()
        event = _FakeEvent()

        asyncio.run(on_message(plugin, event))

        self.assertEqual(len(plugin.writer.items), 1)
        self.assertEqual(len(plugin.jargon_service.fed), 1)
        self.assertEqual(plugin._spawned, [])
        self.assertEqual(plugin.jargon_service.mine_calls, [])
        self.assertEqual(plugin.belief_emergence.calls, [])

    def test_main_does_not_auto_start_consolidation_loop(self):
        source = (Path(__file__).resolve().parents[1] / "main.py").read_text(encoding="utf-8")
        self.assertNotIn("self.consolidation.start(", source)
        self.assertNotIn("from .services.consolidation import ConsolidationService", source)
        self.assertIn("ConsolidationService removed", source)

    def test_long_inbound_message_does_not_guess_concern_or_time_anchor(self):
        on_message, _, _ = _load_on_message()
        resolver = self._resolver()
        plugin = _IngressPlugin(resolver)
        plugin.lifecycle = _FakeLifecycle()
        plugin.concern_tracker = _FakeConcernTracker()
        plugin.subjective_time = _FakeSubjectiveTime()
        plugin._bot_qq_ids = ["bot-event"]
        event = _FakeEvent(message="这是一条超过八十字的群聊灌水，用来确认入站不会再靠字数截出关切或主观时间锚点。" + ("哈" * 40))

        asyncio.run(on_message(plugin, event))

        self.assertEqual(len(plugin.writer.items), 1)
        self.assertEqual(plugin.concern_tracker.added, [])
        self.assertEqual(plugin.subjective_time.anchors, [])
        self.assertEqual(len(plugin.lifecycle.calls), 1)

    def test_lifecycle_receives_event_resolved_scope_without_first_bot_fallback(self):
        on_message, _, _ = _load_on_message()
        resolver = self._resolver()
        plugin = _IngressPlugin(resolver)
        lifecycle = _FakeLifecycle()
        plugin.lifecycle = lifecycle
        event = _FakeEvent()

        asyncio.run(on_message(plugin, event))

        self.assertEqual(len(lifecycle.calls), 1)
        payload = lifecycle.calls[0]
        self.assertIs(payload["scope"], event._wave_memory_runtime_scope)
        self.assertEqual(payload["scope"].bot_id, "bot-profile")
        self.assertEqual(payload["scope"].session.id, "qq:group:group-event")
        self.assertNotIn("sender_id", payload)
        self.assertNotIn("group_id", payload)

    def test_admin_teach_uses_resolved_scope_writer_path(self):
        on_message, _, _ = _load_on_message()
        resolver = self._resolver()
        plugin = _IngressPlugin(resolver)
        event = _FakeEvent(
            message="/teach 作用域必须可追溯",
            sender_id="admin-event",
        )

        asyncio.run(on_message(plugin, event))

        self.assertEqual(resolver.calls, [event])
        self.assertEqual(len(plugin.writer.items), 1)
        payload = plugin.writer.items[0]
        self.assertEqual(payload["scope"].bot_id, "bot-profile")
        self.assertEqual(payload["scope"].session.id, "qq:group:group-event")
        self.assertEqual(payload["group_id"], "group-event")
        self.assertEqual(payload["content"], "[管理员教导] 作用域必须可追溯")
        self.assertEqual(payload["source"], "teach")
        self.assertEqual(payload["importance"], 2.5)
        self.assertEqual(payload["event_id"], "message-event")

    def test_admin_teach_fact_uses_resolved_scope_repository_path(self):
        on_message, _, _ = _load_on_message()
        resolver = self._resolver()
        plugin = _IngressPlugin(resolver)
        event = _FakeEvent(message="/teach 用户是猫", sender_id="admin-event")

        asyncio.run(on_message(plugin, event))

        self.assertEqual(len(plugin.db.scoped_knowledge.fact_calls), 1)
        scope, payload = plugin.db.scoped_knowledge.fact_calls[0]
        self.assertIs(scope, event._wave_memory_runtime_scope)
        self.assertEqual(scope.bot_id, "bot-profile")
        self.assertEqual(scope.session.id, "qq:group:group-event")
        self.assertEqual(payload["subject"], "用户")
        self.assertEqual(payload["predicate"], "是")
        self.assertEqual(payload["object"], "猫")
        self.assertEqual(payload["status"], "pending")
        self.assertEqual(payload["provenance"], {"source": "teach", "event_id": "message-event"})

    def test_explicit_remember_uses_resolved_scope_writer_path(self):
        on_message, _, _ = _load_on_message()
        resolver = self._resolver()
        plugin = _IngressPlugin(resolver)
        event = _FakeEvent(message="记住：作用域必须可追溯")

        asyncio.run(on_message(plugin, event))

        self.assertEqual(resolver.calls, [event])
        self.assertEqual(len(plugin.writer.items), 1)
        payload = plugin.writer.items[0]
        self.assertEqual(payload["scope"].bot_id, "bot-profile")
        self.assertEqual(payload["scope"].session.id, "qq:group:group-event")
        self.assertEqual(payload["group_id"], "group-event")
        self.assertEqual(payload["content"], "[用户要求记住] 作用域必须可追溯")
        self.assertEqual(payload["source"], "explicit")
        self.assertEqual(payload["importance"], 2.0)
        self.assertEqual(payload["event_id"], "message-event")

    def test_bot_sent_resolves_real_group_scope_before_enqueue(self):
        on_bot_sent, _, recorded_errors = _load_on_bot_sent()
        resolver = self._resolver()
        plugin = _BotSentPlugin(resolver)
        recorder = _FakeSelfReflect()
        plugin.self_reflect = recorder
        event = _FakeBotSentEvent(sender_id="bot", group_id="group-event", self_id="bot-event")

        asyncio.run(on_bot_sent(plugin, event))

        self.assertEqual(resolver.calls, [event])
        self.assertEqual(len(plugin.writer.items), 1)
        payload = plugin.writer.items[0]
        self.assertEqual(payload["scope"].bot_id, "bot-profile")
        self.assertEqual(payload["scope"].session.id, "qq:group:group-event")
        self.assertEqual(payload["group_id"], "group-event")
        self.assertEqual(payload["sender_id"], "bot")
        self.assertEqual(payload["event_id"], "message-event")
        self.assertEqual(len(recorder.replies), 1)
        _, reply_kwargs = recorder.replies[0]
        self.assertEqual(reply_kwargs["bot_id"], "bot-profile")
        self.assertIs(reply_kwargs["scope"], payload["scope"])
        self.assertEqual(recorded_errors, [])

    def test_private_bot_sent_writes_exact_memory_without_group_reflection(self):
        on_bot_sent, _, recorded_errors = _load_on_bot_sent()
        resolver = self._resolver()
        plugin = _BotSentPlugin(resolver)
        reflection = _FakeSelfReflect()
        plugin.self_reflect = reflection
        event = _FakePrivateBotSentEvent(sender_id="user-event", self_id="bot-event")

        asyncio.run(on_bot_sent(plugin, event))

        self.assertEqual(resolver.calls, [event])
        self.assertEqual(len(plugin.writer.items), 1)
        payload = plugin.writer.items[0]
        self.assertEqual(payload["scope"].visibility, "private")
        self.assertEqual(payload["scope"].session.id, "qq:private:private-event")
        self.assertEqual(payload["group_id"], "private-event")
        self.assertEqual(payload["sender_id"], "bot")
        self.assertEqual(reflection.replies, [])
        self.assertEqual(recorded_errors, [])

    def test_bot_sent_unknown_bot_fails_closed_without_pseudo_private_group(self):
        on_bot_sent, _, recorded_errors = _load_on_bot_sent()
        resolver = self._resolver(self_id="another-bot")
        plugin = _BotSentPlugin(resolver)
        event = _FakeBotSentEvent(sender_id="user-event", group_id="group-event", self_id="bot-event")

        asyncio.run(on_bot_sent(plugin, event))

        self.assertEqual(plugin.writer.items, [])
        self.assertEqual(plugin._scope_resolution_failed_total, {"unknown_bot_self_id": 1})
        self.assertEqual(recorded_errors, [("BotSentScopeResolution", "unknown_bot_self_id")])


if __name__ == "__main__":
    unittest.main()
