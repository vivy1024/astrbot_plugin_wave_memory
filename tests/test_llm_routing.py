"""模型分工（services/llm_routing.py）回归测试。

硬约束：Model_Routing_Settings 全部留空、Bot 也没有模型覆盖时，每个后台大模型任务的回退链
必须与改造前逐字一致。下面的 ``_legacy_*`` 是改造前各调用点拼链逻辑的原样拷贝
（app/startup.py 的 ``_llm_chain``、MetaThinking.__init__、标签审计维护任务），
不调用新模块，专门用来锁住兼容性。测试数据全部是虚构 QQ 号与昵称。
"""

from __future__ import annotations

import ast
import asyncio
import json
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace

import pytest

if "astrbot.api" not in sys.modules:  # TagExtractor / TagAuditor 只用到 logger
    _api = types.ModuleType("astrbot.api")
    _api.logger = SimpleNamespace(info=lambda *a, **k: None, warning=lambda *a, **k: None,
                                  debug=lambda *a, **k: None, error=lambda *a, **k: None)
    _astrbot = types.ModuleType("astrbot")
    _astrbot.api = _api
    sys.modules["astrbot"] = _astrbot
    sys.modules["astrbot.api"] = _api

from domain.bot_profile import BOT_MODEL_TASKS, BotProfile
from services.llm_fallback import (
    LLMCallStats,
    LLMFallbackClient,
    build_provider_chain,
    call_first_available_provider,
    parse_provider_ids,
    provider_ids_from_config,
)
from services.llm_routing import (
    BOT_OVERRIDABLE_TASKS,
    ROUTING_SECTION,
    TASK_IDS,
    TASKS,
    LLMRouter,
    legacy_meta_chain,
)

ROOT = Path(__file__).resolve().parents[1]

TAG_FAMILY = ("tag", "jargon", "fewshot", "self_reflect", "belief")
LEARNING = ("bookkeeping", "diary", "portrait")


# ---------------------------------------------------------------- 改造前的拼链逻辑（原样拷贝）

def _legacy_tag_chain(cfg: dict) -> list[str]:
    # app/startup.py 旧 _llm_chain() 与 app/bootstrap.py 旧 TagExtractor(provider_id, provider_fallback_ids)
    return build_provider_chain(cfg.get("tag_llm_provider_id", ""), cfg.get("llm_fallback_provider_ids", ""))


def _legacy_meta_chain(cfg: dict) -> list[str]:
    # services/meta_thinking.py 旧 __init__
    meta = cfg.get("MetaThinking_Settings", {}) or {}
    default_model = meta.get("default_model", "")
    meta_fallback_ids = (
        provider_ids_from_config(meta, prefix="provider_")
        or parse_provider_ids(meta.get("provider_fallback_ids", ""))
    )
    return build_provider_chain(
        default_model, meta_fallback_ids or parse_provider_ids(cfg.get("meta_thinking_fallback_ids", ""))
    )


def _legacy_learning_chain(cfg: dict, meta_active: bool) -> list[str]:
    # app/startup.py 旧 _init_learning_services：有 MetaThinking 用 meta.llm，否则 _llm_chain()
    return _legacy_meta_chain(cfg) if meta_active else _legacy_tag_chain(cfg)


def _legacy_tag_audit_chain(cfg: dict) -> list[str]:
    # app/maintenance.py 旧 _maintenance_run_tag_audit：TagAuditor(provider_id=tag_llm_provider_id)
    provider_id = str(cfg.get("tag_llm_provider_id") or "")
    return [provider_id] if provider_id else []


def _expected(cfg: dict, task: str, meta_active: bool) -> list[str]:
    if task in TAG_FAMILY:
        return _legacy_tag_chain(cfg)
    if task == "tag_audit":
        return _legacy_tag_audit_chain(cfg)
    if task == "meta_thinking":
        return _legacy_meta_chain(cfg)
    return _legacy_learning_chain(cfg, meta_active)


def _legacy_available(cfg: dict, task: str, meta_active: bool) -> bool:
    if task in TAG_FAMILY or task == "tag_audit":
        return bool(cfg.get("tag_llm_provider_id", ""))
    if task == "meta_thinking":
        return True
    return meta_active or bool(_legacy_tag_chain(cfg))


ONLINE = {
    "tag_llm_provider_id": "nug/antigravity:gemini-3.7-flash-medium",
    "llm_fallback_provider_ids": "",
    "MetaThinking_Settings": {"enabled": True, "default_model": "nug/antigravity:gemini-3.8-flash-medium"},
    "MetaThinking_Bot1": {"qq_id": "10001", "name": "甲", "db_id": "bot_a", "model": ""},
    "MetaThinking_Bot2": {"qq_id": "", "name": "", "db_id": "", "model": ""},
}

COMPAT_CONFIGS = {
    "online": ONLINE,
    "fallbacks": {
        "tag_llm_provider_id": "grok/a",
        "llm_fallback_provider_ids": "b, c ,d",
        "MetaThinking_Settings": {"default_model": "m1", "provider_fallback_ids": "m2,m3"},
    },
    "meta_provider_slots": {
        "tag_llm_provider_id": "t1",
        "MetaThinking_Settings": {"default_model": "", "provider_1": "p1", "provider_2": " p2 "},
        "meta_thinking_fallback_ids": "g1",
    },
    "meta_global_fallback_only": {
        "tag_llm_provider_id": "t1",
        "llm_fallback_provider_ids": "t2",
        "MetaThinking_Settings": {"default_model": ""},
        "meta_thinking_fallback_ids": "g1, g2",
    },
    "tag_empty_fallback_only": {"tag_llm_provider_id": "", "llm_fallback_provider_ids": "b"},
    "fallback_list": {"tag_llm_provider_id": "a", "llm_fallback_provider_ids": ["x", "", "y"]},
    "all_empty": {},
    "new_keys_present_but_empty": {
        **ONLINE,
        ROUTING_SECTION: {task: "" for task in TASK_IDS} | {"diary": None, "portrait": "  "},
    },
}


class _Profiles:
    """Bot 注册表的最小替身：按 db_id 取 Profile。"""

    def __init__(self, *profiles: BotProfile):
        self.by_id = {p.db_id: p for p in profiles}

    def get(self, db_id):
        return self.by_id.get(db_id)


def _router(cfg: dict, *profiles: BotProfile, context=None) -> LLMRouter:
    registry = _Profiles(*profiles)
    return LLMRouter(cfg, context=context, profile_lookup=registry.get)


def _bot(db_id="bot_a", name="甲", qq_id="10001", **extra) -> BotProfile:
    return BotProfile.from_dict({"db_id": db_id, "name": name, "qq_id": qq_id, **extra})


# ---------------------------------------------------------------- 兼容：新键全空 = 改造前

@pytest.mark.parametrize("meta_active", [True, False])
@pytest.mark.parametrize("name", sorted(COMPAT_CONFIGS))
def test_empty_routing_keys_keep_every_task_chain_identical(name, meta_active):
    cfg = COMPAT_CONFIGS[name]
    router = _router(cfg, _bot())  # Bot 没有模型覆盖
    router.set_meta_thinking_active(meta_active)
    for task in TASK_IDS:
        expected = _expected(cfg, task, meta_active)
        assert router.chain(task) == expected, (name, task)
        assert router.chain(task, "bot_a") == expected, (name, task, "bot")
        assert router.client(task).provider_ids == expected, (name, task, "client")
        assert router.available(task) == _legacy_available(cfg, task, meta_active), (name, task, "available")


def test_online_config_sources_and_family_default():
    router = _router(ONLINE)
    tag = router.resolve("jargon")
    assert tag.chain == ("nug/antigravity:gemini-3.7-flash-medium",)
    assert (tag.source, tag.source_key) == ("default", "tag_llm_provider_id")
    diary = router.resolve("diary")
    assert diary.chain == ("nug/antigravity:gemini-3.8-flash-medium",)
    assert (diary.source, diary.source_key) == ("default", "MetaThinking_Settings.default_model")
    router.set_meta_thinking_active(False)
    diary = router.resolve("diary")
    assert diary.chain == ("nug/antigravity:gemini-3.7-flash-medium",)
    assert diary.source == "legacy"
    # 旧 WaveMemoryPlugin._llm_chain() 保留为兼容别名，指向标签一族默认链
    assert router.family_chain("tag") == _legacy_tag_chain(ONLINE)


def test_meta_thinking_without_injected_client_keeps_legacy_chain():
    from services.meta_thinking import MetaThinking

    for cfg in COMPAT_CONFIGS.values():
        meta = MetaThinking(
            db=SimpleNamespace(conn=sqlite3.connect(":memory:")),
            context=None,
            bot_qq_id="10001",
            config=cfg.get("MetaThinking_Settings", {}),
            global_fallback_ids=cfg.get("meta_thinking_fallback_ids", ""),
        )
        assert meta.provider_ids == _legacy_meta_chain(cfg)
        assert legacy_meta_chain(cfg.get("MetaThinking_Settings", {}), cfg.get("meta_thinking_fallback_ids", "")) == _legacy_meta_chain(cfg)


def test_meta_thinking_uses_injected_router_client_and_bot_override():
    from services.meta_thinking import MetaThinking

    router = _router(ONLINE, _bot(model="bot-a-model"))
    meta = MetaThinking(
        db=SimpleNamespace(conn=sqlite3.connect(":memory:")),
        context=None,
        bot_qq_id="10001",
        bot_qq_ids=["10001"],
        bot_names={"10001": "甲"},
        bot_db_ids={"10001": "bot_a"},
        config=ONLINE["MetaThinking_Settings"],
        llm_client=router.client("meta_thinking"),
    )
    assert meta.llm is router.client("meta_thinking")
    assert meta.provider_ids == ["nug/antigravity:gemini-3.8-flash-medium"]
    assert meta._llm_for("10001").provider_ids == ["bot-a-model", "nug/antigravity:gemini-3.8-flash-medium"]


def test_tag_extractor_standalone_chain_unchanged_and_routed_chain_is_live():
    from services.tag_extractor import TagExtractor

    standalone = TagExtractor(context=None, provider_id="grok/a", provider_fallback_ids="b, c")
    assert standalone.provider_ids == ["grok/a", "b", "c"]
    assert standalone.provider_id == "grok/a"

    cfg = {"tag_llm_provider_id": "grok/a", "llm_fallback_provider_ids": "b"}
    router = _router(cfg)
    routed = TagExtractor(
        context=None, provider_id="grok/a", provider_fallback_ids="b",
        chain_resolver=router.resolver("tag"), llm_stats=router.stats("tag"),
    )
    assert routed.provider_ids == ["grok/a", "b"]
    router.reload({**cfg, ROUTING_SECTION: {"tag": "fast/tagger"}})
    assert routed.provider_ids == ["fast/tagger", "grok/a", "b"]
    # 导入字段映射校验只用首选模型
    assert routed.provider_id == "fast/tagger"


# ---------------------------------------------------------------- 任务配置优先于默认

def test_task_config_goes_first_and_falls_back_to_family_default():
    cfg = {**COMPAT_CONFIGS["fallbacks"], ROUTING_SECTION: {"jargon": "cheap/j", "diary": "writer/d, writer/e", "tag_audit": "audit/x"}}
    router = _router(cfg)
    jargon = router.resolve("jargon")
    assert jargon.chain == ("cheap/j", "grok/a", "b", "c", "d")
    assert (jargon.source, jargon.source_key) == ("task", f"{ROUTING_SECTION}.jargon")
    assert router.chain("diary") == ["writer/d", "writer/e", "m1", "m2", "m3"]
    # 标签审计的默认仍只有标签模型本身
    assert router.chain("tag_audit") == ["audit/x", "grok/a"]
    # 其他任务不受影响
    assert router.chain("fewshot") == _legacy_tag_chain(cfg)
    assert router.chain("bookkeeping") == _legacy_meta_chain(cfg)


def test_task_config_duplicate_of_default_is_deduped():
    router = _router({**ONLINE, ROUTING_SECTION: {"tag": "nug/antigravity:gemini-3.7-flash-medium"}})
    assert router.chain("tag") == ["nug/antigravity:gemini-3.7-flash-medium"]
    assert router.resolve("tag").source == "task"


def test_task_config_makes_task_available_without_tag_model():
    router = _router({ROUTING_SECTION: {"jargon": "only/j"}})
    assert router.available("jargon") is True
    assert router.chain("jargon") == ["only/j"]
    assert router.available("fewshot") is False


# ---------------------------------------------------------------- Bot 覆盖

def test_bot_override_only_applies_to_bot_voice_tasks():
    bot = _bot(model="bot/default", models={"diary": "bot/diary", "jargon": "bot/jargon", "tag": "bot/tag"})
    cfg = {**ONLINE, ROUTING_SECTION: {"diary": "task/diary"}}
    router = _router(cfg, bot)
    meta_default = "nug/antigravity:gemini-3.8-flash-medium"

    diary = router.resolve("diary", "bot_a")
    assert diary.chain == ("bot/diary", "bot/default", "task/diary", meta_default)
    assert (diary.source, diary.source_key) == ("bot", "bot:bot_a.models.diary")
    bookkeeping = router.resolve("bookkeeping", "bot_a")
    assert bookkeeping.chain == ("bot/default", meta_default)
    assert bookkeeping.source_key == "bot:bot_a.model"
    # 标签一族不允许按 Bot 覆盖：即使 Profile 里写了也不生效
    for task in ("jargon", "tag", "tag_audit", "self_reflect"):
        assert router.chain(task, "bot_a") == router.chain(task), task
        assert router.resolve(task, "bot_a").source != "bot"
    # 不带 bot_id、或 Bot 不存在时走全局
    assert router.chain("diary") == ["task/diary", meta_default]
    assert router.chain("diary", "bot_missing") == ["task/diary", meta_default]


def test_bot_override_tasks_match_domain_whitelist():
    assert BOT_OVERRIDABLE_TASKS == frozenset(BOT_MODEL_TASKS)
    assert {spec.id for spec in TASKS if spec.bot_overridable} == set(BOT_MODEL_TASKS)


def test_bot_profile_models_round_trip_and_normalize():
    bot = _bot(model=" m ", models={" diary ": " d ", "bookkeeping": "", "future_task": "x", "bad": None})
    assert bot.model == "m"
    assert bot.models == {"diary": "d", "future_task": "x"}
    again = BotProfile.from_dict(bot.to_dict())
    assert again.models == bot.models
    # 旧 Profile（没有 models 字段）照常读取
    assert _bot().models == {}
    assert BotProfile.from_dict({"db_id": "bot_b", "name": "乙", "models": "not-a-dict"}).models == {}


def test_router_ignores_non_whitelisted_bot_models_even_if_stored():
    bot = _bot(models={"future_task": "x", "jargon": "y"})
    router = _router(ONLINE, bot)
    assert router.chain("jargon", "bot_a") == _legacy_tag_chain(ONLINE)
    assert router.chain("diary", "bot_a") == _legacy_meta_chain(ONLINE)


# ---------------------------------------------------------------- 非法 / 不存在的 provider

def test_invalid_task_values_are_treated_as_empty_and_reported():
    router = _router({**ONLINE, ROUTING_SECTION: {"jargon": 5, "fewshot": {"x": 1}, "diary": ["a", 3, " b "]}})
    assert router.chain("jargon") == _legacy_tag_chain(ONLINE)
    assert router.chain("fewshot") == _legacy_tag_chain(ONLINE)
    assert router.chain("diary")[:2] == ["a", "b"]
    assert set(router.describe()["invalid"]) == {"jargon", "fewshot", "diary"}
    broken = _router({**ONLINE, ROUTING_SECTION: "not-an-object"})
    assert broken.chain("tag") == _legacy_tag_chain(ONLINE)
    assert "*" in broken.describe()["invalid"]


def test_unknown_task_id_raises():
    with pytest.raises(ValueError, match="未知的模型分工任务"):
        _router(ONLINE).chain("nope")


class _Resp:
    def __init__(self, text):
        self.completion_text = text


class _Provider:
    def __init__(self, provider_id, *, text="ok", error=None):
        self.provider_id = provider_id
        self.text = text
        self.error = error
        self.calls: list[dict] = []

    async def text_chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.error is not None:
            raise self.error
        return _Resp(self.text)

    def meta(self):
        return SimpleNamespace(id=self.provider_id, model=f"model-of-{self.provider_id}", type="openai_chat_completion")


class _Context:
    def __init__(self, *providers):
        self.providers = {p.provider_id: p for p in providers}

    def get_provider_by_id(self, provider_id):
        return self.providers.get(provider_id)

    def get_all_providers(self):
        return list(self.providers.values())


def test_missing_configured_provider_falls_back_to_default_and_is_counted():
    tag_model = _Provider("nug/antigravity:gemini-3.7-flash-medium", text="结果")
    router = _router({**ONLINE, ROUTING_SECTION: {"jargon": "typo/not-exist"}}, context=_Context(tag_model))
    response = asyncio.run(router.client("jargon").text_chat(prompt="p"))
    assert response.completion_text == "结果"
    assert len(tag_model.calls) == 1
    stats = router.stats("jargon").snapshot()
    assert stats["calls"] == 1 and stats["succeeded"] == 1 and stats["failed"] == 0
    assert stats["missing_providers"] == ["typo/not-exist"]
    assert stats["last_provider"] == "nug/antigravity:gemini-3.7-flash-medium"
    described = {item["id"]: item for item in router.describe(known_providers=["nug/antigravity:gemini-3.7-flash-medium"])["tasks"]}
    assert described["jargon"]["missing"] == ["typo/not-exist"]
    assert described["jargon"]["chain_status"][0] == {"id": "typo/not-exist", "known": False}


def test_failing_chain_records_failure_and_last_error():
    bad = _Provider("a", error=RuntimeError("Error code: 402 - Insufficient Balance"))
    router = _router({"tag_llm_provider_id": "a"}, context=_Context(bad))
    with pytest.raises(RuntimeError):
        asyncio.run(router.client("self_reflect").text_chat(prompt="p"))
    stats = router.stats("self_reflect").snapshot()
    assert stats["failed"] == 1 and stats["provider_errors"] == 1
    assert "status=402" in stats["last_error"] and stats["last_error_provider"] == "a"
    empty = _router({})
    with pytest.raises(RuntimeError, match="No provider_ids configured"):
        asyncio.run(empty.client("jargon").text_chat(prompt="p"))
    assert empty.stats("jargon").snapshot()["failed"] == 1


def test_call_first_available_provider_records_stats():
    stats = LLMCallStats("tag")
    good = _Provider("b", text="[]")
    resp = asyncio.run(call_first_available_provider(_Context(good), ["missing", "b"], prompt="p", system_prompt="s", stats=stats))
    assert resp.completion_text == "[]"
    assert "stats" not in good.calls[0]  # 统计参数不透传给 provider
    assert stats.snapshot()["succeeded"] == 1 and stats.snapshot()["missing_providers"] == ["missing"]


def test_tag_auditor_walks_chain_and_keeps_single_provider_behaviour():
    from services.tag_auditor import TagAuditor

    tags = [{"id": 1, "name": "甲", "type": "person", "frequency": 3}]
    failing = _Provider("audit/a", error=RuntimeError("Error code: 503 - busy"))
    backup = _Provider("tag/b", text='[{"action": "delete", "tag_id": 1, "reason": "测试"}]')
    auditor = TagAuditor(db=None, context=_Context(failing, backup), provider_id="audit/a", provider_ids=["audit/a", "tag/b"], llm_stats=LLMCallStats("tag_audit"))
    assert asyncio.run(auditor.audit_batch(tags)) == [{"action": "delete", "tag_id": 1, "reason": "测试"}]
    assert auditor.llm_stats.snapshot()["provider_errors"] == 1

    # 只给 provider_id：与旧实现相同——找不到返回空、报错向上抛
    assert TagAuditor(db=None, context=_Context(), provider_id="x").provider_ids == ["x"]
    assert asyncio.run(TagAuditor(db=None, context=_Context(), provider_id="x").audit_batch(tags)) == []
    with pytest.raises(RuntimeError):
        asyncio.run(TagAuditor(db=None, context=_Context(failing), provider_id="audit/a").audit_batch(tags))


# ---------------------------------------------------------------- 热更新

def test_reload_updates_existing_clients_in_place():
    router = _router(ONLINE, _bot())
    client = router.client("bookkeeping")
    bound = client.for_bot("bot_a")
    assert client.provider_ids == ["nug/antigravity:gemini-3.8-flash-medium"]
    result = router.reload({**ONLINE, ROUTING_SECTION: {"bookkeeping": "new/model"}})
    assert result["changed"] == ["bookkeeping"]
    assert router.client("bookkeeping") is client
    assert client.provider_ids == ["new/model", "nug/antigravity:gemini-3.8-flash-medium"]
    assert bound.provider_ids == ["new/model", "nug/antigravity:gemini-3.8-flash-medium"]
    # 旧配置键需重启：reload 不重读 tag_llm_provider_id
    router.reload({"tag_llm_provider_id": "changed-without-restart"})
    assert router.chain("tag") == ["nug/antigravity:gemini-3.7-flash-medium"]


def test_bot_profile_edit_is_live_for_bound_clients():
    registry = _Profiles(_bot())
    router = LLMRouter(ONLINE, profile_lookup=registry.get)
    bound = router.client("diary").for_bot("bot_a")
    assert bound.provider_ids == ["nug/antigravity:gemini-3.8-flash-medium"]
    registry.by_id["bot_a"] = _bot(models={"diary": "bot/diary"})
    assert bound.provider_ids == ["bot/diary", "nug/antigravity:gemini-3.8-flash-medium"]


def test_fixed_client_set_provider_ids_and_for_bot_passthrough():
    client = LLMFallbackClient(None, ["a"])
    assert client.for_bot("bot_a") is client
    client.set_provider_ids(["b", "", "c"])
    assert client.provider_ids == ["b", "c"]
    routed = _router(ONLINE).client("jargon")
    routed_copy = LLMFallbackClient(None, [], chain_resolver=lambda bot_id: ["x"])
    routed_copy.provider_ids = ["y"]  # 赋值即改为固定链
    assert routed_copy.provider_ids == ["y"] and routed_copy.routed is False
    assert routed.routed is True


def test_bookkeeping_and_diary_pick_bot_bound_client():
    from domain.scope import RuntimeScope, SessionRef
    from services.daily_diary import DailyDiaryService
    from services.post_reply_bookkeeping import PostReplyBookkeeper

    seen: list[str | None] = []

    class _Routed:
        def for_bot(self, bot_id):
            seen.append(bot_id)
            return self

        async def text_chat(self, **kwargs):
            return _Resp("{}")

    class _Timeline:
        def get_unsettled_state(self, **_kwargs):
            return {"energy": 0.0, "traces": []}

    scope = RuntimeScope(bot_id="bot_a", visibility="group", session=SessionRef("qq:group:900001", "qq", "group", "900001"))
    conn = sqlite3.connect(":memory:")
    conn.execute(
        """CREATE TABLE memories (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           group_id TEXT DEFAULT 'g1', sender_id TEXT, sender_name TEXT, content TEXT, timestamp REAL, quarantine INTEGER DEFAULT 0)"""
    )
    conn.execute(
        """CREATE TABLE scoped_soul_concerns (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, visibility TEXT,
           topic TEXT, intensity REAL, status TEXT)"""
    )
    rows = [("20001", "小甲", "今天好热"), ("bot", "甲", "是啊"), ("20002", "小乙", "开空调吧"), ("bot", "甲", "好主意")]
    for index, (sender, name, text) in enumerate(rows, 1):
        conn.execute(
            "INSERT INTO memories (bot_id, session_id, visibility, sender_id, sender_name, content, timestamp) VALUES (?,?,?,?,?,?,?)",
            ("bot_a", "qq:group:900001", "group", sender, name, text, 1000.0 + index),
        )
    db = SimpleNamespace(conn=conn, person_timeline=_Timeline())
    keeper = PostReplyBookkeeper(db, SimpleNamespace(get=lambda name: None), _Routed())
    asyncio.run(keeper.run(scope))
    assert seen == ["bot_a"]
    assert keeper.stats.get("runs") == 1

    source = (ROOT / "services" / "daily_diary.py").read_text(encoding="utf-8")
    assert 'getattr(self.llm, "for_bot", None)' in source and "for_bot(scope.bot_id)" in source
    assert DailyDiaryService  # 导入成功即可


# ---------------------------------------------------------------- 启动接线（源码层）

def _method_source(path: Path, name: str) -> str:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return ast.unparse(node)
    raise AssertionError(f"{name} not found in {path}")


def test_startup_takes_every_task_client_from_router():
    startup = ROOT / "app" / "startup.py"
    init = _method_source(startup, "_initialize_once")
    learning = _method_source(startup, "_init_learning_services")
    assert "LLMFallbackClient(" not in init and "LLMFallbackClient(" not in learning
    assert "_llm_chain()" not in init and "_llm_chain()" not in learning
    for task in ("jargon", "fewshot", "meta_thinking", "self_reflect", "belief"):
        assert f"self.llm_routing.client('{task}')" in init, task
    for task in ("bookkeeping", "diary"):
        assert f"routing.client('{task}')" in learning, task
    assert "self.llm_routing.set_meta_thinking_active(self.meta_thinking is not None)" in init
    construct = _method_source(ROOT / "app" / "bootstrap.py", "_construct")
    assert "chain_resolver=self.llm_routing.resolver('tag')" in construct
    maintenance = _method_source(ROOT / "app" / "maintenance.py", "_maintenance_run_tag_audit")
    assert "routing.chain('tag_audit')" in maintenance


def test_schema_declares_every_task_with_empty_default_and_hot_apply():
    schema = json.loads((ROOT / "_conf_schema.json").read_text(encoding="utf-8"))
    items = schema[ROUTING_SECTION]["items"]
    assert list(items) == list(TASK_IDS)
    for task, meta in items.items():
        assert meta["default"] == "" and meta["type"] == "string", task
        assert meta.get("_special") == "select_provider", task
        assert not meta.get("restart_required"), task
    # 旧配置键仍标记需重启（模型分工只热重载新键）
    assert schema["tag_llm_provider_id"]["restart_required"] is True
    assert schema["MetaThinking_Settings"]["items"]["default_model"]["restart_required"] is True


# ---------------------------------------------------------------- 9876：保存热生效、总览、Bot 覆盖

@pytest.fixture
def config_app():
    from quart import Quart

    from webui.blueprints.config import config_bp
    from webui.container import get_container

    container = get_container()
    saved = {name: getattr(container, name, None) for name in ("plugin_config", "llm_routing", "embedding_service", "bot_registry", "service_registry")}
    container.password = ""
    app = Quart(__name__)
    app.register_blueprint(config_bp)
    yield app, container
    for name, value in saved.items():
        setattr(container, name, value)
    for name in ("settings_effective_config_snapshot", "settings_effective_since_by_key"):
        if hasattr(container, name):
            delattr(container, name)


@pytest.mark.asyncio
async def test_saving_routing_key_reloads_router_without_restart(config_app):
    app, container = config_app
    cfg = json.loads(json.dumps(ONLINE))
    container.plugin_config = cfg
    container.service_registry = None
    router = _router(cfg)
    container.llm_routing = router
    client = app.test_client()

    schema = await (await client.get("/api/config/schema")).get_json()
    group = next(g for g in schema["groups"] if g["key"] == ROUTING_SECTION)
    diary_item = next(item for item in group["items"] if item["key"] == "diary")
    assert diary_item["apply_mode"] == "service" and diary_item["service"] == "llm_routing"
    assert diary_item["effective"] == ""

    res = await client.post("/api/config/full", json={ROUTING_SECTION: {"diary": "writer/d"}})
    body = await res.get_json()
    assert body["ok"] is True and body["restart_required"] is False
    assert body["apply_modes"]["service"]["llm_routing"]["action"] == "reloaded"
    assert "模型分工已按新配置生效" in body["message"]
    assert router.chain("diary") == ["writer/d", "nug/antigravity:gemini-3.8-flash-medium"]
    group = next(g for g in body["schema"]["groups"] if g["key"] == ROUTING_SECTION)
    assert next(item for item in group["items"] if item["key"] == "diary")["effective"] == "writer/d"

    # 旧键仍需重启：保存后路由不变
    res = await client.post("/api/config/full", json={"tag_llm_provider_id": "other/tag"})
    body = await res.get_json()
    assert body["restart_required"] is True
    assert router.chain("tag") == ["nug/antigravity:gemini-3.7-flash-medium"]


@pytest.mark.asyncio
async def test_llm_routing_overview_endpoint(config_app):
    app, container = config_app
    context = _Context(_Provider("nug/antigravity:gemini-3.7-flash-medium"), _Provider("nug/antigravity:gemini-3.8-flash-medium"))
    cfg = {**ONLINE, ROUTING_SECTION: {"jargon": "typo/missing"}, "embedding_provider_id": "emb/qwen", "embedding_dimension": 1024}
    container.plugin_config = cfg
    bot = _bot(models={"diary": "nug/antigravity:gemini-3.7-flash-medium"})
    container.bot_registry = SimpleNamespace(all=lambda: [bot])
    container.embedding_service = SimpleNamespace(context=context, provider_id="emb/qwen", dimension=1024)
    router = _router(cfg, bot, context=context)
    router.set_active("jargon", True)
    router.set_active("self_reflect", False, "启动时没有可用模型，未创建；指定模型后需重启一次")
    container.llm_routing = router

    data = await (await app.test_client().get("/api/config/llm-routing")).get_json()
    assert data["available"] is True
    tasks = {item["id"]: item for item in data["tasks"]}
    assert list(tasks) == list(TASK_IDS)
    assert tasks["jargon"]["source"] == "task" and tasks["jargon"]["missing"] == ["typo/missing"]
    assert tasks["jargon"]["apply_mode"] == "hot"
    assert tasks["self_reflect"]["apply_mode"] == "restart"
    assert tasks["portrait"]["reserved"] is True and tasks["portrait"]["active"] is False
    assert tasks["diary"]["bots"][0]["bot_id"] == "bot_a" and tasks["diary"]["bots"][0]["source"] == "bot"
    assert tasks["bookkeeping"]["bots"] == []  # 没有覆盖的 Bot 不列出
    assert data["embedding"] == {
        "key": "embedding_provider_id", "provider_id": "emb/qwen", "saved": "emb/qwen", "dimension": 1024,
        "apply_mode": "restart", "note": data["embedding"]["note"],
    }
    assert {item["id"] for item in data["providers"]} == set(context.providers)
    assert data["families"]["tag"]["apply_mode"] == "restart"
    assert {item["rides_on"] for item in data["piggyback"]} == {"bookkeeping"}


@pytest.mark.asyncio
async def test_llm_routing_overview_without_router(config_app):
    app, container = config_app
    container.plugin_config = {}
    container.llm_routing = None
    container.embedding_service = None
    data = await (await app.test_client().get("/api/config/llm-routing")).get_json()
    assert data["available"] is False and data["tasks"] == []
    assert data["providers_error"]


def test_inventory_lists_bot_model_overrides():
    from services.config.channel_config import build_default_channel_config
    from services.config.inventory import build_inventory

    bot = _bot(model="bot/default", models={"diary": "bot/diary", "jargon": "bot/jargon"})
    defaults = build_default_channel_config()
    inv = build_inventory(
        settings_payload={"groups": []}, hot_params=[], default_channel_config=defaults,
        effective_channel_config=defaults, bot_profiles=[bot], schema={}, saved_config={},
    )
    rows = {row["key"]: row for row in inv["items"]}
    assert rows["bot:bot_a.model"]["effective"] == "bot/default" and rows["bot:bot_a.model"]["layer"] == "bot"
    assert rows["bot:bot_a.models.diary"]["warning"] is None
    assert "不支持按 Bot 覆盖" in rows["bot:bot_a.models.jargon"]["warning"]


@pytest.fixture
def bots_client(tmp_path):
    from quart import Quart

    from domain import bot_identity
    from engine.db.bot_profile_repo import BotProfileRepo
    from engine.db.connection import ConnectionManager
    from services.bot_registry import BotRegistry
    from webui.blueprints.bots import bots_bp
    from webui.container import get_container

    cm = ConnectionManager(str(tmp_path / "bots.sqlite3"))
    registry = BotRegistry({"MetaThinking_Bot1": {"qq_id": "10001", "name": "甲", "db_id": "bot_a"}})
    registry.attach(BotProfileRepo(cm), connection=cm)
    container = get_container()
    container.bot_registry = registry
    container.password = ""
    app = Quart(__name__)
    app.register_blueprint(bots_bp)
    yield app.test_client(), registry
    container.bot_registry = None
    bot_identity.publish()
    cm.close()


@pytest.mark.asyncio
async def test_bot_api_saves_model_overrides_and_rejects_non_bot_tasks(bots_client):
    client, registry = bots_client
    listing = await (await client.get("/api/bots")).get_json()
    assert listing["model_tasks"] == list(BOT_MODEL_TASKS)
    current = registry.get("bot_a")

    bad = await client.put("/api/bots/bot_a", json={"version": current.version, "item": {**current.to_dict(), "models": {"jargon": "x"}}})
    assert bad.status_code == 400
    assert (await bad.get_json())["error"]["code"] == "invalid_models"

    ok = await client.put("/api/bots/bot_a", json={"version": current.version, "item": {**current.to_dict(), "model": "bot/default", "models": {"diary": "bot/diary"}}})
    assert ok.status_code == 200, await ok.get_data()
    saved = registry.get("bot_a")
    assert saved.model == "bot/default" and saved.models == {"diary": "bot/diary"}
    assert saved.version == current.version + 1

    # 版本冲突检查不受影响
    stale = await client.put("/api/bots/bot_a", json={"version": current.version, "item": {**current.to_dict(), "models": {}}})
    assert stale.status_code == 409

    # 导出/导入保留模型覆盖；路由立即按新 Profile 解析
    exported = await (await client.get("/api/bots/export")).get_json()
    item = dict(exported["items"][0], db_id="bot_b", qq_id="20002", name="乙")
    res = await client.post("/api/bots/import", json={"items": [item]})
    assert (await res.get_json())["imported"] == ["bot_b"]
    assert registry.get("bot_b").models == {"diary": "bot/diary"}
    router = LLMRouter(ONLINE, profile_lookup=registry.get)
    assert router.chain("diary", "bot_b") == ["bot/diary", "bot/default", "nug/antigravity:gemini-3.8-flash-medium"]
