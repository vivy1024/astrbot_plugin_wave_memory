"""游戏经验：Bot 从自己的游戏经历里写经验卡（不预设攻略）。

锁定：
- 素材只来自她自己的游戏经历（记忆表 source=game_event）与前后说的话；别人的话不带名字；
- 死亡 / 危机这类刚发生的经历才安排即时回想，补录的历史经历与零碎发现留给每日一轮；
- 写成只属于这个 Bot 的经验卡（source_type=experience），依据链接到具体经历；同一条经历不重复计数；
- 否决过的不再写回来；每个游戏有上限；模型失败时进度不前进，下次重来；
- 注入时她自己的经验排在攻略前面、措辞分开；别的 Bot 看不到。
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime
from types import SimpleNamespace

import pytest

from domain.game_knowledge import parse_game_context
from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from services.game_knowledge import GameKnowledgeService
from services.game_knowledge.lessons import (
    FRESH_EVENT_SECONDS,
    GameLessonService,
    group_episodes,
    veto_lesson,
)
from services.game_knowledge.service import EXPERIENCE_HEADER, INJECTION_HEADER, render_card, render_injection

BOT = "bot-demo"
LIVE = "bilibili:group:30000001"
BASE = datetime(2026, 9, 18, 20, 40).timestamp()


class _LLM:
    """按顺序回放 JSON（或异常）；记录每次收到的提示词。"""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.prompts: list[str] = []

    async def text_chat(self, *, prompt, system_prompt=None, contexts=None):
        self.prompts.append(prompt)
        payload = self.payloads.pop(0) if self.payloads else {"new": [], "again": [], "revise": []}
        if isinstance(payload, Exception):
            raise payload
        return SimpleNamespace(completion_text=payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False))


@pytest.fixture
def db(tmp_path):
    database = WaveMemoryDB(str(tmp_path / "lessons.sqlite"), dimension=3)
    yield database
    database.close()


def add_memory(db, content: str, ts: float, *, sender: str = "bot", name: str = "羽书", game: str = "", event_type: str = "",
               session: str = LIVE, bot: str = BOT) -> int:
    source = "game_event" if game else ""
    provenance = json.dumps({"metadata": {"origin_kind": f"runtime:game_event:{game}:{event_type}"}}) if game else None
    row = db.conn.execute(
        "INSERT INTO memories (bot_id, session_id, visibility, group_id, sender_id, sender_name, content, timestamp, source, provenance)"
        " VALUES (?, ?, 'group', ?, ?, ?, ?, ?, ?, ?)",
        (bot, session, session.rsplit(":", 1)[-1], sender, name, content, ts, source, provenance),
    ).lastrowid
    db.conn.commit()
    return int(row)


def river_deaths(db) -> list[int]:
    """9 月 18 日晚上在河边连着被溺尸戳死两次，前后有她自己说的话和弹幕。"""
    add_memory(db, "天黑了，先去河边钓鱼吧", BASE - 60)
    add_memory(db, "主播快跑后面有溺尸", BASE - 30, sender="30000009", name="路过的观众甲")
    first = add_memory(db, "[我的世界] 今天第 3 次死亡：被溺尸戳死（yushu was impaled by Drowned），死在主世界 (82, 60, -306)",
                       BASE, game="minecraft", event_type="death")
    add_memory(db, "啊啊啊我的东西全掉水里了", BASE + 20)
    second = add_memory(db, "[我的世界] 今天第 4 次死亡：被溺尸戳死（yushu was impaled by Drowned），死在主世界 (80, 61, -300)",
                        BASE + 180, game="minecraft", event_type="death")
    add_memory(db, "晚上别去河边啊", BASE + 200, sender="30000010", name="路过的观众乙")
    return [first, second]


def new_lesson(event_ids, **overrides):
    lesson = {
        "title": "夜里别在河边待",
        "do": ["天黑前离开水边", "听到溺尸的声音就往岸上跑"],
        "story": ["9月18日夜里在河边连着被溺尸戳死两次"],
        "keywords": ["河边", "溺尸", "我的世界"],
        "conditions": ["time_of_day == night", "moon_phase == full"],
        "event_ids": event_ids,
    }
    lesson.update(overrides)
    return lesson


def make(db, llm, **kwargs):
    knowledge = GameKnowledgeService(db.game_knowledge)
    service = GameLessonService(
        db.game_knowledge, knowledge=knowledge, llm_for_bot=lambda bot_id: llm, enabled=True,
        bot_name_for=lambda bot_id: "羽书", bot_ids_for=lambda: [BOT], **kwargs,
    )
    return service, knowledge


def lessons_of(knowledge, bot=BOT, game="minecraft"):
    return knowledge.repo.all_items("card", game, bot_id=bot)


def test_deaths_become_an_experience_card_owned_by_the_bot(db):
    first, second = river_deaths(db)
    llm = _LLM({"new": [new_lesson([first, second])]})
    service, knowledge = make(db, llm)
    # 直播请求带来的状态键：写条件时只能用这些
    knowledge.repo.ensure_game("minecraft", "我的世界")
    scope = RuntimeScope(BOT, "group", SessionRef(LIVE, "bilibili", "group", "30000001"))
    knowledge.note_active_game(scope, parse_game_context({"id": "minecraft", "state": {"time_of_day": "night", "health": 20}}))

    result = asyncio.run(service.reflect(BOT, trigger="manual"))
    assert result["status"] == "succeeded" and result["counts"]["new"] == 1

    [prompt] = llm.prompts
    assert "被溺尸戳死" in prompt and "你说：天黑了，先去河边钓鱼吧" in prompt and "别人说：主播快跑后面有溺尸" in prompt
    # 经历与前后说的话按时间交错：先说去河边、弹幕提醒，然后第一次死，东西掉水里，再死一次
    order = [prompt.index(text) for text in (
        "你说：天黑了", "别人说：主播快跑", f"[#{first} ", "你说：啊啊啊", f"[#{second} ", "别人说：晚上别去河边",
    )]
    assert order == sorted(order), "经历和对话按时间先后排"
    assert "路过的观众" not in prompt and "30000009" not in prompt, "别人的话不带名字和号码"
    assert "time_of_day（例：night）" in prompt and f"[#{first} " in prompt

    [card] = lessons_of(knowledge)
    assert card["source_type"] == "experience" and card["bot_id"] == BOT and card["status"] == "active"
    assert card["title"] == "夜里别在河边待" and card["points"][0] == "天黑前离开水边"
    assert card["keywords"] == ["河边", "溺尸"], "游戏名不当触发词"
    assert card["conditions"] == [{"key": "time_of_day", "op": "eq", "value": "night"}], "没见过的状态键不能写进条件"
    assert card["meta"]["times"] == 2 and card["meta"]["evidence_ids"] == [first, second]
    assert knowledge.get_game("minecraft")["counts"]["lessons"]["active"] == 1

    # 进度前进：没有新经历就不再调用模型
    assert asyncio.run(service.reflect(BOT, trigger="manual"))["status"] in {"no_events", "skipped"}
    assert len(llm.prompts) == 1


def test_game_library_is_created_for_a_new_game(db):
    first, second = river_deaths(db)
    service, knowledge = make(db, _LLM({"new": [new_lesson([first, second], conditions=[])]}))
    assert knowledge.get_game("minecraft") is None
    asyncio.run(service.reflect(BOT, trigger="manual"))
    game = knowledge.get_game("minecraft")
    assert game["name"] == "我的世界" and game["lessons_enabled"] is True
    assert len(lessons_of(knowledge)) == 1


def test_same_event_is_not_counted_twice_and_again_adds_new_ones(db):
    first, second = river_deaths(db)
    llm = _LLM({"new": [new_lesson([first])]})
    service, knowledge = make(db, llm)
    asyncio.run(service.reflect(BOT, trigger="manual"))
    [card] = lessons_of(knowledge)
    assert card["meta"]["times"] == 1

    third = add_memory(db, "[我的世界] 今天第 5 次死亡：被溺尸戳死", BASE + 3600, game="minecraft", event_type="death")
    llm.payloads = [{"again": [{"id": card["id"], "event_ids": [first, third]}]}]
    asyncio.run(service.reflect(BOT, trigger="manual"))
    [card] = lessons_of(knowledge)
    assert card["meta"]["times"] == 2, "已经算过的经历不重复计数"
    assert card["meta"]["evidence_ids"] == [first, third]
    assert card["meta"]["last_at"] == BASE + 3600
    assert f"经验#{card['id']}（1 次" in llm.prompts[1], "已有经验连同次数给她看"


def test_similar_new_lesson_merges_into_existing_one(db):
    first, second = river_deaths(db)
    llm = _LLM({"new": [new_lesson([first])]})
    service, knowledge = make(db, llm)
    asyncio.run(service.reflect(BOT, trigger="manual"))
    third = add_memory(db, "[我的世界] 又掉进河里了", BASE + 7200, game="minecraft", event_type="crisis")
    llm.payloads = [{"new": [new_lesson([third], title="夜里别在河边待着")]}]
    result = asyncio.run(service.reflect(BOT, trigger="manual"))
    [card] = lessons_of(knowledge)
    assert result["counts"].get("merged") == 1 and card["meta"]["times"] == 2


def test_vetoed_lesson_is_not_written_again(db):
    first, second = river_deaths(db)
    llm = _LLM({"new": [new_lesson([first])]})
    service, knowledge = make(db, llm)
    asyncio.run(service.reflect(BOT, trigger="manual"))
    [card] = lessons_of(knowledge)
    vetoed = veto_lesson(knowledge.repo, "minecraft", card["id"], version=card["version"], bot_id=BOT)
    assert vetoed["status"] == "archived" and vetoed["meta"]["vetoed"] is True

    sixth = add_memory(db, "[我的世界] 今天第 6 次死亡：被溺尸戳死", BASE + 5000, game="minecraft", event_type="death")
    llm.payloads = [{"new": [new_lesson([sixth])]}]
    result = asyncio.run(service.reflect(BOT, trigger="manual"))
    assert result["counts"]["blocked_vetoed"] == 1
    assert [c["status"] for c in lessons_of(knowledge)] == ["archived"]
    assert "夜里别在河边待" in llm.prompts[-1].split("【你删掉、不再要的经验】", 1)[1], "否决过的给她看，别再写"


def test_revise_rewrites_content_and_keeps_evidence(db):
    first, second = river_deaths(db)
    llm = _LLM({"new": [new_lesson([first])]})
    service, knowledge = make(db, llm)
    asyncio.run(service.reflect(BOT, trigger="manual"))
    [card] = lessons_of(knowledge)
    third = add_memory(db, "[我的世界] 白天在河边也被溺尸戳死了", BASE + 40000, game="minecraft", event_type="death")
    llm.payloads = [{"revise": [{"id": card["id"], "title": "河边一直要小心溺尸", "do": ["带三叉戟的溺尸白天也会远程戳人，离水远一点"],
                                 "event_ids": [third], "why": "白天也被戳死了"}]}]
    result = asyncio.run(service.reflect(BOT, trigger="manual"))
    [card] = lessons_of(knowledge)
    assert result["counts"]["revised"] == 1
    assert card["title"] == "河边一直要小心溺尸" and card["points"] == ["带三叉戟的溺尸白天也会远程戳人，离水远一点"]
    assert card["meta"]["revisions"] == 1 and card["meta"]["revised_why"] == "白天也被戳死了"
    assert card["meta"]["evidence_ids"] == [first, third]


def test_model_failure_keeps_progress_for_next_time(db):
    first, second = river_deaths(db)
    llm = _LLM(RuntimeError("upstream down"), "不是 JSON", {"new": [new_lesson([first, second])]})
    service, knowledge = make(db, llm)
    assert asyncio.run(service.reflect(BOT, trigger="event"))["status"] == "failed"
    assert asyncio.run(service.reflect(BOT, trigger="event"))["status"] == "failed"
    assert knowledge.repo.lesson_cursor(BOT) == 0 and lessons_of(knowledge) == []
    assert asyncio.run(service.reflect(BOT, trigger="daily"))["status"] == "succeeded"
    assert len(lessons_of(knowledge)) == 1 and knowledge.repo.lesson_cursor(BOT) >= second
    runs = knowledge.repo.lesson_runs(bot_id=BOT)
    assert [run["status"] for run in runs] == ["succeeded", "failed", "failed"]


def test_game_switch_off_skips_without_model_calls(db):
    river_deaths(db)
    llm = _LLM()
    service, knowledge = make(db, llm)
    game = knowledge.repo.ensure_game("minecraft", "我的世界")
    knowledge.update_game("minecraft", {"lessons_enabled": False}, expected_version=game["version"])
    result = asyncio.run(service.reflect(BOT, trigger="daily"))
    assert result["counts"]["skipped_game_disabled"] == 2 and llm.prompts == []


def test_scattered_discoveries_wait_until_there_is_enough(db):
    add_memory(db, "[我的世界] 拿到了钻石×1", BASE, game="minecraft", event_type="discovery")
    llm = _LLM()
    service, _ = make(db, llm)
    result = asyncio.run(service.reflect(BOT, trigger="daily"))
    assert result["status"] == "skipped" and result["counts"]["skipped_not_worthy"] == 1 and llm.prompts == []


def test_daily_limit_blocks_model_calls(db):
    river_deaths(db)
    add_memory(db, "[杀戮尖塔] 这一局在第 2 幕倒下了", BASE + 100, game="sts", event_type="death")
    llm = _LLM()
    service, _ = make(db, llm, daily_limit=1)
    assert asyncio.run(service.reflect(BOT, trigger="daily"))["status"] == "daily_limit"
    assert llm.prompts == []


def test_cap_archives_the_lesson_not_seen_for_longest(db):
    service, knowledge = make(db, _LLM(), max_active=5)
    knowledge.repo.ensure_game("minecraft", "我的世界")
    knowledge.repo.apply_lessons("minecraft", BOT, creates=[
        {"key": f"exp:{BOT}:{i}", "title": f"经验{i}号", "points": [f"做法{i}"], "keywords": [f"词{i}"], "source_type": "experience",
         "status": "active", "meta": {"times": 1, "last_at": BASE + (i if i else 99) * 1000.0}}
        for i in range(6)
    ])
    # 第 1 条最久没再经历（第 0 条虽然键在前，但最近一次经历最晚）
    assert service._enforce_cap("minecraft", BOT, BASE + 10**6) == 1
    archived = [c for c in lessons_of(knowledge) if c["status"] == "archived"]
    assert [c["title"] for c in archived] == ["经验1号"], "最久没再经历的先归档"
    assert not archived[0]["meta"].get("vetoed"), "超上限归档不算否决"


def test_only_fresh_deaths_schedule_an_immediate_reflection(db):
    started = []

    def spawn(coro, name):
        started.append(name)
        coro.close()
        return SimpleNamespace(cancel=lambda: None)

    service, _ = make(db, _LLM(), spawn=spawn, clock=lambda: 1_000_000.0)

    async def run():
        assert service.note_event(BOT, "discovery", 1_000_000.0) is False
        assert service.note_event(BOT, "death", 1_000_000.0 - FRESH_EVENT_SECONDS - 1) is False, "补录的历史经历不即时回想"
        assert service.note_event(BOT, "death", 999_990.0) is True
        assert service.note_event(BOT, "crisis", 999_995.0) is True, "已经排上了，不重复排"

    asyncio.run(run())
    assert started == [f"wave-memory:learning:game-lesson:{BOT}"]
    disabled = GameLessonService(db.game_knowledge, llm_for_bot=lambda b: None, enabled=False)
    assert disabled.note_event(BOT, "death", 999_990.0) is False


def test_experience_comes_first_and_stays_with_its_bot(db):
    first, second = river_deaths(db)
    service, knowledge = make(db, _LLM({"new": [new_lesson([first, second], conditions=[])]}))
    asyncio.run(service.reflect(BOT, trigger="manual"))
    knowledge.create_item("card", "minecraft", {"title": "溺尸", "points": ["溺尸在水里刷新，有的拿三叉戟"], "keywords": ["溺尸"], "status": "active"})
    context = parse_game_context({"id": "minecraft", "state": {"time_of_day": "night"}})

    mine = knowledge.match(context, message="河边有溺尸", bot_id=BOT)
    assert [hit.card["title"] for hit in mine.cards] == ["夜里别在河边待", "溺尸"]
    text = render_injection(mine.game, mine.cards, mine.terms)[0]
    assert text.index(EXPERIENCE_HEADER.format(name="我的世界")) < text.index(INJECTION_HEADER.format(name="我的世界"))
    assert "【夜里别在河边待】（2 次经历，最近 9月18日）" in text and "做法：天黑前离开水边" in text and "经过：" in text

    other = knowledge.match(context, message="河边有溺尸", bot_id="bot-other")
    assert [hit.card["title"] for hit in other.cards] == ["溺尸"], "别的 Bot 看不到她的经验"

    search = knowledge.search("我的世界", "溺尸", bot_id=BOT)
    assert {card["title"] for card in search.cards} == {"夜里别在河边待", "溺尸"}
    assert knowledge.search("我的世界", "溺尸", bot_id="bot-other").cards[0]["title"] == "溺尸"


def test_export_leaves_out_personal_experience(db):
    first, second = river_deaths(db)
    service, knowledge = make(db, _LLM({"new": [new_lesson([first])]}))
    asyncio.run(service.reflect(BOT, trigger="manual"))
    exported = knowledge.export_game("minecraft")
    assert exported["cards"] == []


def test_long_cards_pick_points_related_to_the_question():
    card = {"title": "EP08 制氧模块", "points": ["先拉电线", "做好厕所", "电解器放在高处", "氢气要单独排走", "装好空气清净机", "控制氧气压力"],
            "pitfalls": [], "keywords": []}
    assert "要点：先拉电线；做好厕所；电解器放在高处；氢气要单独排走；装好空气清净机" in render_card(card)
    focused = render_card(card, focus=["氧气", "压力"])
    assert "控制氧气压力" in focused and focused.index("先拉电线") < focused.index("控制氧气压力")


def test_group_episodes_splits_by_gap():
    events = [
        {"session_id": LIVE, "timestamp": 100.0, "memory_id": 1},
        {"session_id": LIVE, "timestamp": 250.0, "memory_id": 2},
        {"session_id": LIVE, "timestamp": 1000.0, "memory_id": 3},
    ]
    assert [[e["memory_id"] for e in episode] for episode in group_episodes(events)] == [[1, 2], [3]]
