import asyncio
import unittest


class FakeSoulRepository:
    def __init__(self, state):
        self.state = state
        self.calls = []

    def get_state(self, scope, limit=25, offset=0):
        self.calls.append({"scope": scope, "limit": limit, "offset": offset})
        return self.state


class FakeMoodService:
    def __init__(self, lines):
        self.lines = lines
        self.calls = []

    def render_lines(self, bot_id, scope):
        self.calls.append((bot_id, scope))
        return list(self.lines)


class SoulStateChannelTest(unittest.TestCase):
    def _ctx(self, *, mode="full", config=None, scope=None):
        from domain.scope import RuntimeScope, SessionRef
        from services.injection.context import InjectionContext

        if scope is None:
            scope = RuntimeScope(
                bot_id="yushu",
                visibility="group",
                session=SessionRef("qq:group:g1", "qq", "group", "g1"),
            )
        return InjectionContext(
            event="event",
            req=object(),
            message="hello",
            group_id="g1",
            sender_id="u1",
            sender_name="用户",
            bot_id="bot",
            bot_profile_id="yushu",
            scope=scope,
            mode=mode,
            config=config or {"channels": {"soul_state": {"enabled": True}}},
            trace_id="trace-soul-state",
        )

    def test_full_mode_records_hit_from_formal_state(self):
        from services.injection.channels.soul_state import SoulStateChannel

        repo = FakeSoulRepository({
            "mood": {"state": "known", "components": {"valence": 0.2, "arousal": 0.1}, "cause": "群里有人帮忙"},
            "concerns": {"items": [{"topic": "考试", "intensity": 0.8}]},
            "timeline": {"items": [{"event_summary": "昨天一起对过题"}]},
            "revision": 3,
        })
        mood = FakeMoodService(["心情：愉快", "缘由：20 分钟前，小明帮你查了资料"])
        result = asyncio.run(SoulStateChannel(repository=repo, mood_service=mood).build(self._ctx()))

        self.assertEqual(result.status, "hit")
        self.assertIn("心情：愉快", result.text)
        self.assertIn("小明帮你查了资料", result.text)
        self.assertIn("昨天一起对过题", result.text)
        # 旧的按群数值只是群氛围：不再注入，也不输出裸数字
        self.assertNotIn("valence", result.text)
        self.assertNotIn("群里有人帮忙", result.text)
        self.assertNotIn("当前关切", result.text)
        self.assertEqual(len(repo.calls), 1)
        self.assertEqual(mood.calls[0][0], "yushu")

    def test_group_diary_is_not_repeated_in_timeline(self):
        from services.injection.channels.soul_state import SoulStateChannel

        repo = FakeSoulRepository({"timeline": {"items": [
            {"event_summary": "【日记】热闹的一天：大家都在", "event_type": "episode"},
            {"event_summary": "答应了帮他看代码", "event_type": "social_anchor.bot_promised_user"},
        ]}})
        result = asyncio.run(SoulStateChannel(repository=repo).build(self._ctx()))

        self.assertEqual(result.status, "hit")
        self.assertNotIn("日记", result.text)
        self.assertIn("答应了帮他看代码", result.text)

    def test_mood_failure_or_contamination_does_not_break_channel(self):
        from services.injection.channels.soul_state import SoulStateChannel

        class Broken:
            def render_lines(self, *_a, **_k):
                raise RuntimeError("boom")

        repo = FakeSoulRepository({"timeline": {"items": [{"event_summary": "一起通关"}]}})
        result = asyncio.run(SoulStateChannel(repository=repo, mood_service=Broken()).build(self._ctx()))
        self.assertEqual(result.status, "hit")
        self.assertIn("一起通关", result.text)

    def test_memory_only_disables_without_querying_repository(self):
        from services.injection.channels.soul_state import SoulStateChannel

        repo = FakeSoulRepository({"mood": {"state": "known"}})
        result = asyncio.run(SoulStateChannel(repository=repo).build(self._ctx(mode="memory_only")))

        self.assertEqual(result.status, "disabled")
        self.assertEqual(repo.calls, [])

    def test_full_mode_empty_state_still_returns_channel_result(self):
        from services.injection.channels.soul_state import SoulStateChannel

        repo = FakeSoulRepository({})
        result = asyncio.run(SoulStateChannel(repository=repo).build(self._ctx()))

        self.assertEqual(result.status, "empty")
        self.assertEqual(result.warnings, ["soul_state_unknown"])
        self.assertEqual(len(repo.calls), 1)
