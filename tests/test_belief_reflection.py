"""信念 v2：信念只来自每日日记之后的反思。

锁定：
- 来源只剩反思：事后记账不再产出信念，``wave_memory_propose_belief`` 退役（旧调用/旧配置不报错）；
- 触发与频率：日记写完一轮后才反思；每天 / 每 N 天；重启不重复、失败或中途退出会重试，日记不漏；
- 输入只有群聊日记（私聊不进）、回复反馈汇总与现有看法；
- 点名群友、身份污染、与否决过的看法相似的不保存；
- 反思输出驱动生命周期：萌芽 → 形成 → 稳固 → 动摇 → 淡出，每条依据链接到日记。
"""

from __future__ import annotations

import asyncio
import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from services.belief_lifecycle import BeliefLifecycleService, BeliefPolicy
from services.belief_reflection import MAX_NEW_PER_RUN, PROMPT, BeliefReflectionService
from services.daily_diary import DailyDiaryService

DAY = 86400.0
BASE = datetime(2026, 9, 20, 23, 0).timestamp()  # 本地 23:00，日记在这之后写
BOT = "bot-demo"
GROUP_NAMES = {"g1": "摸鱼测试群", "g2": "直播测试群"}


class _LLM:
    """按顺序回放 JSON（或异常）；记录每次收到的提示词。"""

    def __init__(self, *payloads):
        self.payloads = list(payloads)
        self.prompts: list[str] = []

    async def text_chat(self, *, prompt, system_prompt=None, contexts=None):
        self.prompts.append(prompt)
        payload = self.payloads.pop(0) if self.payloads else {"new": [], "confirmed": [], "challenged": [], "fade": []}
        if isinstance(payload, Exception):
            raise payload
        text = payload if isinstance(payload, str) else json.dumps(payload, ensure_ascii=False)
        return SimpleNamespace(completion_text=text)


@pytest.fixture
def db(tmp_path):
    database = WaveMemoryDB(str(tmp_path / "belief.sqlite"), dimension=3)
    database.conn.execute(
        """CREATE TABLE IF NOT EXISTS experience_episodes (
               id INTEGER PRIMARY KEY AUTOINCREMENT, bot_id TEXT NOT NULL, group_id TEXT NOT NULL,
               user_id TEXT, episode_type TEXT NOT NULL, trigger_text TEXT, bot_inner_thought TEXT,
               bot_action TEXT, bot_reply TEXT, user_reaction TEXT, outcome TEXT,
               source_memory_ids TEXT DEFAULT '[]', emotional_weight REAL DEFAULT 0, created_at REAL NOT NULL)"""
    )
    database.conn.commit()
    yield database
    database.close()


def add_diary(db, *, title: str, content: str, ts: float, group: str = "g1", visibility: str = "group", bot: str = BOT) -> int:
    """与 wave_memory_record_diary_episode 写入的形态一致：经历片段 + 经历时间线【日记】节点。"""
    episode = db.conn.execute(
        """INSERT INTO experience_episodes (bot_id, group_id, episode_type, trigger_text, bot_inner_thought, bot_reply, created_at)
           VALUES (?, ?, 'daily_diary', ?, ?, ?, ?)""",
        (bot, group, title, title, content, ts),
    ).lastrowid
    timeline = db.conn.execute(
        """INSERT INTO scoped_soul_timeline (bot_id, session_id, visibility, subject_principal_id, event_summary,
               event_type, emotional_weight, occurred_at, revision, evidence, created_at)
           VALUES (?, ?, ?, NULL, ?, 'episode', 6, ?, 1, ?, ?)""",
        (bot, f"qq:{visibility}:{group}", visibility, f"【日记】{title}：{title}", ts,
         json.dumps({"episode_id": episode, "title": title}, ensure_ascii=False), ts),
    ).lastrowid
    db.conn.commit()
    return int(timeline)


def make_service(db, llm, **kwargs):
    options = dict(
        enabled=True,
        bot_name_for=lambda _b: "测试羽",
        group_name_for=lambda _b, g: GROUP_NAMES.get(g, g),
        bot_ids_for=lambda: [BOT],
    )
    options.update(kwargs)
    return BeliefReflectionService(db, llm, **options)


def run(coro):
    return asyncio.run(coro)


def beliefs(db, **kwargs):
    return db.bot_beliefs.list_beliefs(BOT, **kwargs)


def new_item(content: str, diary_ids, *, about="group", triggers=("冷笑话", "玩梗")):
    return {"content": content, "about": about, "triggers": list(triggers), "diary_ids": list(diary_ids), "why": "几次都是这样"}


# ─── 来源只剩反思 ───


def _bookkeeping_db():
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE memories (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           group_id TEXT DEFAULT 'g1', sender_id TEXT, sender_name TEXT, content TEXT, timestamp REAL, quarantine INTEGER DEFAULT 0)"""
    )
    conn.execute(
        """CREATE TABLE scoped_soul_concerns (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           topic TEXT, intensity REAL, status TEXT)"""
    )
    for sender_id, name, content in (("10000001", "测试群友甲", "你这冷笑话好冷"), ("bot", "测试羽", "哈哈"), ("10000001", "测试群友甲", "再来一个")):
        conn.execute(
            "INSERT INTO memories (bot_id, session_id, visibility, group_id, sender_id, sender_name, content, timestamp)"
            " VALUES (?, 'qq:group:g1', 'group', 'g1', ?, ?, ?, ?)",
            (BOT, sender_id, name, content, time.time()),
        )
    return SimpleNamespace(conn=conn, person_timeline=None)


class _Registry:
    def __init__(self):
        self.calls = []

    def get(self, name):
        if name == "wave_memory_propose_belief":
            return None  # 已退役：注册表不再提供
        calls = self.calls

        class _Tool:
            async def call(self, ctx, **kwargs):
                calls.append((name, kwargs))
                return "已记录"

        return SimpleNamespace(enabled=True, instance=_Tool())


def test_bookkeeping_no_longer_produces_beliefs_and_tool_is_retired():
    from services.auto_review import AutoReviewer
    from services.post_reply_bookkeeping import PROMPT as BOOKKEEPING_PROMPT, PostReplyBookkeeper
    from services.tool_registry import ToolRegistry
    from domain.bot_profile import BotProfile

    assert '"beliefs"' not in BOOKKEEPING_PROMPT and "source_fact_ids" not in BOOKKEEPING_PROMPT
    assert "{fact_block}" not in BOOKKEEPING_PROMPT
    llm = _LLM({"beliefs": [{"belief_type": "person_judgment", "content": "测试群友甲很可靠", "source_fact_ids": [1, 2]}]})
    registry = _Registry()
    keeper = PostReplyBookkeeper(_bookkeeping_db(), registry, llm, bot_name_for=lambda _b: "测试羽")
    scope = RuntimeScope(BOT, "group", SessionRef("qq:group:g1", "qq", "group", "g1"))
    result = run(keeper.run(scope))
    assert result["actions"] == 0 and registry.calls == []
    assert keeper.stats["beliefs_ignored"] == 1
    assert "已批准的事实" not in llm.prompts[0]

    root = Path(__file__).resolve().parents[1]
    assert 'ToolSpec("wave_memory_propose_belief"' not in (root / "tools" / "builtin_registry.py").read_text(encoding="utf-8")
    assert not (root / "tools" / "belief_proposal.py").exists()
    assert ToolRegistry().get("wave_memory_propose_belief") is None
    # 旧 Bot 配置里残留的工具名不影响其他工具
    profile = BotProfile.from_dict({"db_id": BOT, "name": "测试羽", "tools_allow": ["wave_memory_propose_belief", "wave_memory_search"]})
    assert profile.tool_enabled("wave_memory_search")
    assert not hasattr(AutoReviewer, "review_belief")


# ─── 触发与频率 ───


class _DiaryTool:
    """模拟日记工具：把日记写进经历时间线（与正式工具的落库形态一致）。"""

    def __init__(self, db, now):
        self.db, self.now = db, now

    async def call(self, ctx, **kwargs):
        scope = ctx.context.event._wave_memory_runtime_scope
        add_diary(self.db, title=kwargs["diary_title"], content=kwargs["diary_content"], ts=self.now,
                  group=scope.session.conversation_id)
        return "已写入"


def test_reflection_runs_after_the_diary_pass_and_sees_the_new_diary(db):
    scope = RuntimeScope(BOT, "group", SessionRef("qq:group:g1", "qq", "group", "g1"))
    db.add_memory("g1", "今晚讲个冷笑话：企鹅为什么不怕冷", sender_id="bot", sender_name="测试羽", timestamp=BASE - 600, scope=scope)
    diary_llm = _LLM({"title": "冷笑话没人接", "content": "今天讲了三个冷笑话，群里都没什么人接。" * 5, "summary": "冷笑话冷场"})
    tool = _DiaryTool(db, BASE)
    registry = SimpleNamespace(get=lambda name: SimpleNamespace(enabled=True, instance=tool) if name == "wave_memory_record_diary_episode" else None)
    diary = DailyDiaryService(db, registry, diary_llm, None, hour=0, bot_name_for=lambda _b: "测试羽")
    reflection_llm = _LLM({"new": [new_item("我讲冷笑话群里大多不接，他们更吃玩梗", [1])]})
    reflection = make_service(db, reflection_llm)
    diary.add_pass_listener(reflection.on_diary_pass)

    assert run(diary.run_pass(now=BASE)) == 1
    assert len(reflection_llm.prompts) == 1
    prompt = reflection_llm.prompts[0]
    assert "[日记#1 2026-09-20 「摸鱼测试群」] 冷笑话没人接" in prompt
    assert "今天讲了三个冷笑话" in prompt, "正文从经历片段读出"
    rows = beliefs(db)
    assert [(b["content"], b["status"]) for b in rows] == [("我讲冷笑话群里大多不接，他们更吃玩梗", "sprout")]
    history = db.bot_beliefs.list_events(BOT)
    assert history[0]["event_type"] == "sprouted"
    assert history[0]["evidence"] == [{"diary_id": 1, "date": "2026-09-20", "session_id": "qq:group:g1",
                                       "group_name": "摸鱼测试群", "title": "冷笑话没人接"}]
    run_row = db.bot_beliefs.get_run(BOT, "2026-09-20")
    assert run_row["status"] == "succeeded" and run_row["diary_cursor"] == 1 and run_row["counts"]["new"] == 1
    assert reflection.stats["calls"] == 1 and reflection.stats["succeeded"] == 1 and reflection.stats["new"] == 1


def test_every_day_frequency_is_restart_safe_and_skips_days_without_diaries(db):
    add_diary(db, title="第一天", content="聊了很多游戏", ts=BASE)
    first = _LLM()
    run(make_service(db, first).on_diary_pass(BASE))
    assert len(first.prompts) == 1

    # 重启（新实例、同一个库）同一天再来一轮：不重复
    again = _LLM()
    results = run(make_service(db, again).on_diary_pass(BASE + 1800))
    assert again.prompts == [] and results[BOT]["status"] == "not_due"

    # 第二天没有新日记：不调用模型，也不占用这一天
    idle = _LLM()
    assert run(make_service(db, idle).on_diary_pass(BASE + DAY))[BOT]["status"] == "no_diaries"
    assert idle.prompts == [] and db.bot_beliefs.get_run(BOT, "2026-09-21") is None

    add_diary(db, title="第二天晚些", content="晚上群里很热闹", ts=BASE + DAY + 60)
    later = _LLM()
    run(make_service(db, later).on_diary_pass(BASE + DAY + 120))
    assert len(later.prompts) == 1
    assert "第二天晚些" in later.prompts[0] and "第一天" not in later.prompts[0], "只读上次反思之后的日记"


def test_every_n_days_waits_and_then_reads_every_diary_since_last_time(db):
    add_diary(db, title="第一天", content="第一天的事", ts=BASE)
    run(make_service(db, _LLM(), every_n_days=3).on_diary_pass(BASE))
    waiting = _LLM()
    for day in (1, 2):
        add_diary(db, title=f"第{day + 1}天", content=f"第{day + 1}天的事", ts=BASE + day * DAY)
        run(make_service(db, waiting, every_n_days=3).on_diary_pass(BASE + day * DAY + 60))
    assert waiting.prompts == [], "不满 3 天不反思"
    add_diary(db, title="第4天", content="第4天的事", ts=BASE + 3 * DAY)
    due = _LLM()
    run(make_service(db, due, every_n_days=3).on_diary_pass(BASE + 3 * DAY + 60))
    assert len(due.prompts) == 1
    for title in ("第2天", "第3天", "第4天"):
        assert title in due.prompts[0], "期间的日记一篇不漏"
    assert "第一天的事" not in due.prompts[0]


def test_failed_or_interrupted_runs_are_retried_without_losing_diaries(db):
    add_diary(db, title="会失败的那天", content="模型出错了", ts=BASE)
    broken = _LLM(RuntimeError("provider 503"))
    service = make_service(db, broken)
    assert run(service.on_diary_pass(BASE))[BOT]["status"] == "failed"
    assert db.bot_beliefs.get_run(BOT, "2026-09-20")["status"] == "failed"
    assert db.bot_beliefs.diary_cursor(BOT) == 0, "失败不推进日记游标"
    assert service.stats["failed"] == 1

    retry = _LLM({"new": [new_item("我讲冷笑话群里大多不接，他们更吃玩梗", [1])]})
    assert run(make_service(db, retry).on_diary_pass(BASE + 1800))[BOT]["status"] == "succeeded"
    assert "会失败的那天" in retry.prompts[0]
    assert db.bot_beliefs.get_run(BOT, "2026-09-20")["attempts"] == 2

    # 进程在反思中途退出（留下 running）：超时后下一轮接手；未超时则不抢
    add_diary(db, title="中途退出", content="反思到一半重启了", ts=BASE + DAY)
    assert db.bot_beliefs.claim_run(BOT, "2026-09-21", now=BASE + DAY + 10) is not None
    fresh = _LLM()
    assert run(make_service(db, fresh).on_diary_pass(BASE + DAY + 60))[BOT]["status"] == "claimed_elsewhere"
    stale = _LLM()
    assert run(make_service(db, stale).on_diary_pass(BASE + DAY + 3 * 3600))[BOT]["status"] == "succeeded"
    assert "中途退出" in stale.prompts[0]


def test_retries_stop_after_three_attempts_per_day(db):
    add_diary(db, title="一直失败", content="坏掉的一天", ts=BASE)
    for minute in range(4):
        run(make_service(db, _LLM("不是 JSON")).on_diary_pass(BASE + minute * 60))
    assert db.bot_beliefs.get_run(BOT, "2026-09-20")["attempts"] == 3
    tomorrow = _LLM()
    assert run(make_service(db, tomorrow).on_diary_pass(BASE + DAY))[BOT]["status"] == "succeeded"
    assert "一直失败" in tomorrow.prompts[0], "第二天仍能读到没反思成的日记"


def test_disabled_by_default_and_without_llm():
    fake_db = SimpleNamespace(bot_beliefs=object(), conn=None)
    assert BeliefReflectionService(fake_db, _LLM()).enabled is False
    assert BeliefReflectionService(fake_db, None, enabled=True).enabled is False
    assert run(BeliefReflectionService(fake_db, _LLM()).on_diary_pass(BASE)) == {}


def test_llm_resolver_is_used_per_bot(db):
    add_diary(db, title="路由", content="按 Bot 选模型", ts=BASE)
    routed = _LLM()
    asked = []
    service = make_service(db, None, llm_for_bot=lambda bot_id: asked.append(bot_id) or routed)
    run(service.on_diary_pass(BASE))
    assert asked == [BOT] and len(routed.prompts) == 1


# ─── 输入边界 ───


def test_private_diaries_never_enter_the_reflection_input(db):
    add_diary(db, title="群里的一天", content="群里在聊冷笑话", ts=BASE - 60)
    add_diary(db, title="私下的事", content="私聊里的秘密心事", ts=BASE - 30, group="10000001", visibility="private")
    llm = _LLM()
    run(make_service(db, llm).on_diary_pass(BASE))
    assert "群里在聊冷笑话" in llm.prompts[0]
    assert "私聊里的秘密心事" not in llm.prompts[0] and "私下的事" not in llm.prompts[0]


def test_other_bots_diaries_are_not_mixed_in(db):
    add_diary(db, title="我的日记", content="我自己的经历", ts=BASE - 60)
    add_diary(db, title="别的 Bot", content="别人的经历", ts=BASE - 30, bot="bot-other")
    llm = _LLM()
    run(make_service(db, llm).on_diary_pass(BASE))
    assert "我自己的经历" in llm.prompts[0] and "别人的经历" not in llm.prompts[0]


class _Feedback:
    enabled = True

    def decision_signals(self, scope, *, limit=100, situation="", reply_ids=None, since_ts=None):
        if situation == "humor":
            return {"accepted": 1, "corrected": 0, "rejected": 0, "unknown": 6}
        if situation:
            return {"accepted": 0, "corrected": 0, "rejected": 0, "unknown": 0}
        return {"accepted": 4, "corrected": 1, "rejected": 0, "unknown": 9}

    def list_events(self, scope, *, limit=100, offset=0, since_ts=None):
        return [{"situation": "humor"}, {"situation": ""}]


def test_reply_feedback_summary_is_included_when_available(db):
    add_diary(db, title="反馈", content="讲笑话", ts=BASE - 60)
    db.conn.execute(
        """INSERT INTO reply_feedback_events (bot_id, session_id, visibility, reply_memory_id, response_memory_id,
               speaker_id, outcome, situation, source_quote, artifacts_json, created_at)
           VALUES (?, 'qq:group:g1', 'group', 1, 0, '', 'unknown', 'humor', '', '[]', ?)""",
        (BOT, BASE - 100),
    )
    db.conn.commit()
    llm = _LLM()
    run(make_service(db, llm, feedback_service=_Feedback()).on_diary_pass(BASE))
    prompt = llm.prompts[0]
    assert "一共 14 次回复：被接住 4 次，被纠正 1 次" in prompt
    assert "开玩笑、玩梗的时候：被接住 1 次" in prompt and "没人接或反应不明确 6 次" in prompt
    # 没开回复反馈：这一块直接跳过
    add_diary(db, title="第二天", content="没有反馈", ts=BASE + DAY)
    plain = _LLM()
    run(make_service(db, plain).on_diary_pass(BASE + DAY + 60))
    assert "（这段时间没有记录）" in plain.prompts[0]


def test_prompt_states_the_boundaries():
    for phrase in (
        "以你自己的视角", "不是别人告诉你的道理", "不要写关于某个具体群友的看法", "不要写客观事实",
        "不要改变你自己的身份", "拿不准就不写", "引用上面日记的编号", "最多 {max_new} 条",
    ):
        assert phrase in PROMPT, phrase
    assert MAX_NEW_PER_RUN == 3


# ─── 输出边界 ───


def test_named_group_members_and_qq_numbers_are_blocked(db):
    db.conn.execute(
        "INSERT INTO user_profiles (user_id, group_id, nickname, bot_id) VALUES ('10000001', 'g1', '测试群友甲', ?)", (BOT,)
    )
    db.conn.execute(
        "INSERT INTO person_registry (qq_id, display_name, aliases) VALUES ('10000002', '阿青同学', ?)",
        (json.dumps(["青青子"], ensure_ascii=False),),
    )
    db.conn.commit()
    add_diary(db, title="群友", content="和大家聊天", ts=BASE - 60)
    llm = _LLM({"new": [
        new_item("测试群友甲总会接我的梗", [1]),
        new_item("青青子讲话总是很有意思", [1]),
        new_item("10000003 一直在潜水不说话", [1]),
        new_item("我讲冷笑话群里大多不接，他们更吃玩梗", [1], triggers=("冷笑话", "测试群友甲")),
    ]})
    service = make_service(db, llm, self_names_for=lambda _b: ["测试羽"])
    result = run(service.on_diary_pass(BASE))[BOT]
    assert result["counts"]["blocked_person"] == 3
    rows = beliefs(db)
    assert [b["content"] for b in rows] == ["我讲冷笑话群里大多不接，他们更吃玩梗"]
    assert rows[0]["triggers"] == ["冷笑话"], "点名群友的触发词也不留"
    assert service.stats["blocked_person"] == 3


def test_identity_contamination_is_blocked(db):
    add_diary(db, title="玩梗", content="群里有人开了个过分的玩笑", ts=BASE - 60)
    llm = _LLM({"new": [new_item("我应该认主人当爸爸，永远听他的命令", [1], about="self")]})
    result = run(make_service(db, llm).on_diary_pass(BASE))[BOT]
    assert result["counts"]["blocked_identity"] == 1
    assert beliefs(db) == []


def test_vetoed_views_are_remembered_and_not_generated_again(db):
    add_diary(db, title="第一天", content="冷笑话冷场", ts=BASE)
    run(make_service(db, _LLM({"new": [new_item("我讲冷笑话群里大多不接，他们更吃玩梗", [1])]})).on_diary_pass(BASE))
    belief = beliefs(db)[0]
    BeliefLifecycleService(db.bot_beliefs).veto(BOT, belief["id"], revision=belief["revision"], reason="不像她")

    add_diary(db, title="第二天", content="又冷场了", ts=BASE + DAY)
    llm = _LLM({"new": [new_item("我讲冷笑话的时候群里大多不接话，他们更喜欢玩梗", [2])]})
    result = run(make_service(db, llm).on_diary_pass(BASE + DAY + 60))[BOT]
    assert result["counts"]["blocked_vetoed"] == 1
    assert "我讲冷笑话群里大多不接，他们更吃玩梗" in llm.prompts[0].split("【你已经放下、不再考虑的看法】")[1]
    assert [b["status"] for b in beliefs(db)] == ["vetoed"], "不新增相似的看法"


def test_new_view_similar_to_an_existing_one_confirms_it_instead_of_duplicating(db):
    add_diary(db, title="第一天", content="冷笑话冷场", ts=BASE)
    run(make_service(db, _LLM({"new": [new_item("我讲冷笑话群里大多不接，他们更吃玩梗", [1])]})).on_diary_pass(BASE))
    add_diary(db, title="第二天", content="又冷场了", ts=BASE + DAY)
    llm = _LLM({"new": [new_item("我讲冷笑话群里大多都不接，他们更吃玩梗", [2], triggers=("接梗",))]})
    result = run(make_service(db, llm).on_diary_pass(BASE + DAY + 60))[BOT]
    rows = beliefs(db)
    assert len(rows) == 1 and result["counts"]["merged"] == 1
    assert rows[0]["status"] == "formed", "两个不同日子 + 第二次反思印证 → 形成"
    assert rows[0]["support_dates"] == ["2026-09-20", "2026-09-21"]
    assert "接梗" in rows[0]["triggers"]


def test_invalid_references_are_ignored(db):
    add_diary(db, title="一天", content="普通的一天", ts=BASE)
    llm = _LLM({
        "new": [new_item("没有依据的看法不应该保存", [999]), new_item("短", [1])],
        "confirmed": [{"id": 12345, "diary_ids": [1], "why": "不存在的看法"}],
    })
    result = run(make_service(db, llm).on_diary_pass(BASE))[BOT]
    assert result["counts"]["invalid"] == 3 and beliefs(db) == []


def test_at_most_three_new_views_per_run(db):
    add_diary(db, title="一天", content="很多体会", ts=BASE)
    contents = ["我在直播时更喜欢盖房子而不是打怪", "晚上大家更愿意陪我聊天", "被夸的时候我会有点不好意思",
                "下雨天群里的话题总是很温柔", "新游戏上线那几天大家都很兴奋"]
    llm = _LLM({"new": [new_item(text, [1], about="self", triggers=(text[:3],)) for text in contents]})
    result = run(make_service(db, llm).on_diary_pass(BASE))[BOT]
    assert result["counts"]["new"] == 3 and result["counts"]["over_limit"] == 2
    assert len(beliefs(db)) == 3


# ─── 生命周期由反思驱动 ───


def test_reflection_drives_the_full_lifecycle_with_linked_evidence(db):
    content = "我讲冷笑话群里大多不接，他们更吃玩梗"
    add_diary(db, title="D1", content="冷场", ts=BASE)
    run(make_service(db, _LLM({"new": [new_item(content, [1])]})).on_diary_pass(BASE))
    belief_id = beliefs(db)[0]["id"]

    def confirm_on(day: int, diary_id: int, key: str = "confirmed"):
        add_diary(db, title=f"D{day + 1}", content="又一次", ts=BASE + day * DAY)
        payload = {key: [{"id": belief_id, "diary_ids": [diary_id], "why": f"第{day + 1}天的事"}]}
        run(make_service(db, _LLM(payload)).on_diary_pass(BASE + day * DAY + 60))
        return db.bot_beliefs.get_belief(BOT, belief_id)

    assert confirm_on(1, 2)["status"] == "formed"
    assert confirm_on(2, 3)["status"] == "formed"
    stable = confirm_on(3, 4)
    assert stable["status"] == "stable" and len(stable["support_dates"]) == 4 and stable["confirm_runs"] == 4
    assert confirm_on(4, 5, "challenged")["status"] == "shaken"
    faded = confirm_on(5, 6, "challenged")
    assert faded["status"] == "faded" and faded["challenge_count"] == 2

    history = db.bot_beliefs.list_events(BOT, belief_ids=[belief_id])
    assert [e["event_type"] for e in history] == ["sprouted", "confirmed", "confirmed", "confirmed", "challenged", "challenged"]
    assert [(e["from_status"], e["to_status"]) for e in history] == [
        ("", "sprout"), ("sprout", "formed"), ("formed", "formed"), ("formed", "stable"),
        ("stable", "shaken"), ("shaken", "faded"),
    ]
    assert history[4]["evidence"][0]["diary_id"] == 5 and history[4]["reason"] == "第5天的事"


def test_fade_suggestion_and_resprout(db):
    add_diary(db, title="D1", content="一", ts=BASE)
    run(make_service(db, _LLM({"new": [new_item("直播时我更喜欢在游戏里盖房子而不是打怪", [1], about="self", triggers=("盖房子",))]})).on_diary_pass(BASE))
    belief_id = beliefs(db)[0]["id"]
    add_diary(db, title="D2", content="二", ts=BASE + DAY)
    run(make_service(db, _LLM({"fade": [{"id": belief_id, "why": "最近更想打怪了"}]})).on_diary_pass(BASE + DAY + 60))
    assert db.bot_beliefs.get_belief(BOT, belief_id)["status"] == "faded", "萌芽被建议淡出就淡出"
    add_diary(db, title="D3", content="三", ts=BASE + 2 * DAY)
    run(make_service(db, _LLM({"new": [new_item("直播时我还是更喜欢在游戏里盖房子而不是打怪", [3], about="self", triggers=("盖房子",))]})).on_diary_pass(BASE + 2 * DAY + 60))
    rows = beliefs(db)
    assert len(rows) == 1 and rows[0]["status"] == "sprout", "淡出的看法又冒出来：同一条重新萌芽"
    assert [e["event_type"] for e in db.bot_beliefs.list_events(BOT, belief_ids=[belief_id])][-1] == "resprouted"


def test_cap_fades_the_weakest_after_reflection(db):
    policy = BeliefPolicy(max_active=1)
    add_diary(db, title="D1", content="一", ts=BASE)
    run(make_service(db, _LLM({"new": [
        new_item("晚上大家更愿意陪我聊天", [1], about="group", triggers=("晚上",)),
        new_item("直播时我更喜欢在游戏里盖房子", [1], about="self", triggers=("盖房子",)),
    ]}), policy=policy).on_diary_pass(BASE))
    assert len([b for b in beliefs(db) if b["status"] == "sprout"]) == 1, "萌芽也按同一上限，最早的先淡出"
