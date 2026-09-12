"""Bot 级广域黑话：仓储幂等、墓碑不复活、提升门禁、以及三段注入互不挤占。"""

from __future__ import annotations

import sys
import types

import pytest

# services/jargon/inference.py 依赖 astrbot.api.logger；聚焦测试里注入轻量替身。
if "astrbot.api" not in sys.modules:
    _astrbot_mod = types.ModuleType("astrbot")
    _api_mod = types.ModuleType("astrbot.api")
    _api_mod.logger = types.SimpleNamespace(
        debug=lambda *a, **k: None, info=lambda *a, **k: None, warning=lambda *a, **k: None,
    )
    sys.modules["astrbot"] = _astrbot_mod
    sys.modules["astrbot.api"] = _api_mod

from domain.scope import RuntimeScope, SessionRef
from engine.db.bot_jargon_repo import BotJargonRepository, BotJargonScopeError
from engine.db.connection import ConnectionManager
from engine.db.migrations.bot_jargon import ensure_bot_jargon_schema
from engine.db.migrations.scoped_derived_knowledge import ensure_scoped_derived_knowledge_schema
from engine.db.scoped_knowledge_repo import ScopedKnowledgeRepo
from services.jargon.inference import JargonInjector


def _group_scope(bot: str = "bot-alpha", session: str = "qq:group:g1") -> RuntimeScope:
    return RuntimeScope(bot, "group", SessionRef(session, "qq", "group", session.split(":")[-1]))


@pytest.fixture
def repo(tmp_path):
    manager = ConnectionManager(str(tmp_path / "bot-jargon.sqlite3"))
    ensure_scoped_derived_knowledge_schema(manager)
    ensure_bot_jargon_schema(manager)
    try:
        yield BotJargonRepository(manager), ScopedKnowledgeRepo(manager), manager
    finally:
        manager.close()


def test_upsert_is_idempotent_per_word_and_requires_bot_id(repo):
    bot_repo, _, _ = repo
    first = bot_repo.upsert_bot_jargon("bot-alpha", word="绝绝子", meaning="赞叹", confidence=0.9)
    second = bot_repo.upsert_bot_jargon("bot-alpha", word="绝绝子", meaning="赞叹升级", confidence=0.95)

    assert first == second
    rows = bot_repo.list_bot_jargon("bot-alpha")
    assert len(rows) == 1
    assert rows[0]["meaning"] == "赞叹升级"

    with pytest.raises(BotJargonScopeError):
        bot_repo.list_bot_jargon("")


def test_bot_level_jargon_is_shared_across_groups(repo):
    """广域黑话的核心诉求：同一个 Bot 的其它群也必须能读到。"""
    bot_repo, _, _ = repo
    bot_repo.upsert_bot_jargon("bot-alpha", word="抽象", meaning="反串调侃", confidence=0.8)

    assert bot_repo.list_active_for_prompt("bot-alpha")[0]["word"] == "抽象"
    # 另一个 Bot 不共享
    assert bot_repo.list_active_for_prompt("bot-beta") == []


def test_delete_leaves_tombstone_so_reference_import_cannot_revive(repo):
    """用户删掉的广域词条，下一次资产导入不能把它复活。"""
    bot_repo, _, _ = repo
    bot_repo.upsert_bot_jargon(
        "bot-alpha", word="v我50", meaning="要钱梗", source="holyman_import", confidence=0.98,
    )
    bot_repo.delete_bot_jargon("bot-alpha", word="v我50")

    assert bot_repo.list_active_for_prompt("bot-alpha") == []
    tombstone = bot_repo.find_bot_jargon("bot-alpha", word="v我50")
    assert tombstone is not None and tombstone["source"] == "manual_deleted"

    # 模拟导入：overwrite_meaning=False 且不应把 status 改回 active
    bot_repo.upsert_bot_jargon(
        "bot-alpha", word="v我50", meaning="要钱梗", source="holyman_import",
        confidence=0.98, reference_key="v我50", overwrite_meaning=False,
    )
    still = bot_repo.find_bot_jargon("bot-alpha", word="v我50")
    assert still["status"] == "inactive"
    assert bot_repo.list_active_for_prompt("bot-alpha") == []


def test_import_does_not_overwrite_manual_meaning(repo):
    bot_repo, _, _ = repo
    bot_repo.upsert_bot_jargon("bot-alpha", word="叠甲", meaning="我自己的解释", confidence=0.1)

    bot_repo.upsert_bot_jargon(
        "bot-alpha", word="叠甲", meaning="资产的解释", source="holyman_import",
        confidence=0.96, reference_key="叠甲", overwrite_meaning=False,
    )

    row = bot_repo.find_bot_jargon("bot-alpha", word="叠甲")
    assert row["meaning"] == "我自己的解释"
    assert row["confidence"] == pytest.approx(0.96)


def test_promote_requires_confirmed_and_meaning(repo):
    """服务层门禁：只有已生效（confirmed）且带释义的群黑话才能提升。"""
    _, scoped_repo, manager = repo
    from services.jargon.service import JargonService

    service = JargonService(type("DB", (), {"scoped_knowledge": scoped_repo, "bot_jargon": BotJargonRepository(manager)})())
    scope = _group_scope()
    pending_id = scoped_repo.upsert_scoped_jargon(
        scope, word="pending词", meaning="还没生效", status="pending",
    )
    with pytest.raises(ValueError):
        service.promote_to_global(scope, pending_id)

    confirmed_id = scoped_repo.upsert_scoped_jargon(
        scope, word="confirmed词", meaning="已生效", status="confirmed", is_jargon=True,
    )
    result = service.promote_to_global(scope, confirmed_id)

    assert result["word"] == "confirmed词"
    assert result["bot_id"] == "bot-alpha"
    rows = service.list_global_jargon("bot-alpha")
    # 提升的词是 Bot 级覆盖层，必须排在默认内置条目之前。
    assert rows[0]["word"] == "confirmed词"
    assert rows[0]["source"] == "promoted"
    assert rows[0]["is_builtin"] is False


def test_injection_sections_use_independent_budgets(repo):
    """两段各自独立名额：广域与参考条目不能挤掉本群私域黑话。"""
    bot_repo, scoped_repo, manager = repo
    scope = _group_scope()
    scoped_repo.upsert_scoped_jargon(scope, word="本群梗", meaning="本群含义", status="confirmed", is_jargon=True)
    bot_repo.upsert_bot_jargon("bot-alpha", word="广域梗", meaning="广域含义", confidence=0.9)

    class _Holyman:
        def runtime_matchable_entries(self):
            return {"内置梗": {"meaning": "内置含义", "confidence": 0.9}}

    db = type("DB", (), {
        "scoped_knowledge": scoped_repo,
        "bot_jargon": bot_repo,
        "is_jargon_blocked": staticmethod(lambda word: False),
    })()
    injector = JargonInjector(db, max_inject=1, holyman_reference=_Holyman(), global_limit=2)

    text = "本群梗 广域梗 内置梗"
    output = injector.get_injection(text, scope)

    assert "本群私域黑话" in output and "本群梗" in output
    # 内置资产与 Bot 级自定义现在同属「广域黑话」一段、同一个名额池。
    assert "【广域黑话" in output and "广域梗" in output and "内置梗" in output

    signals = injector.detect_signals(text, scope)
    assert signals["has_scoped_jargon"] is True
    assert signals["has_bot_global"] is True
    assert signals["has_global_irony"] is True


def test_disabling_builtin_phrase_removes_it_from_injection(repo):
    """修复的开关：内置口癖默认启用，WebUI 停用后必须真的不再注入。"""
    bot_repo, scoped_repo, manager = repo
    from services.jargon.service import JargonService

    class _Holyman:
        def runtime_matchable_entries(self):
            return {"v我50": {"meaning": "要钱梗", "confidence": 0.98}}

    service = JargonService(type("DB", (), {
        "scoped_knowledge": scoped_repo, "bot_jargon": bot_repo,
    })())
    service._holyman = _Holyman()

    db = type("DB", (), {
        "scoped_knowledge": scoped_repo,
        "bot_jargon": bot_repo,
        "is_jargon_blocked": staticmethod(lambda word: False),
    })()
    injector = JargonInjector(db, max_inject=1, holyman_reference=_Holyman(), global_limit=2)
    scope = _group_scope()

    # 默认启用：内置条目无需任何落库记录就能命中。
    assert "v我50" in injector.get_injection("v我50", scope)

    service.set_global_jargon_status("bot-alpha", word="v我50", status="inactive")

    # 注入器有 60 秒 scoped 缓存；这里直接换新实例模拟缓存过期后的真实读取。
    fresh = JargonInjector(db, max_inject=1, holyman_reference=_Holyman(), global_limit=2)
    assert "v我50" not in fresh.get_injection("v我50", scope)
    listed = {row["word"]: row for row in service.list_global_jargon("bot-alpha")}
    assert listed["v我50"]["status"] == "inactive"

    # 重新启用后必须恢复注入（墓碑也要一并清掉）。
    service.set_global_jargon_status("bot-alpha", word="v我50", status="active")
    reenabled = JargonInjector(db, max_inject=1, holyman_reference=_Holyman(), global_limit=2)
    assert "v我50" in reenabled.get_injection("v我50", scope)


def test_removing_builtin_phrase_survives_asset_reload(repo):
    """内置词无法物理删除；移除只落墓碑，且不会被内置资产原样复活。"""
    bot_repo, scoped_repo, manager = repo
    from services.jargon.service import JargonService

    class _Holyman:
        def runtime_matchable_entries(self):
            return {"叠甲": {"meaning": "免责声明", "confidence": 0.96}}

    service = JargonService(type("DB", (), {
        "scoped_knowledge": scoped_repo, "bot_jargon": bot_repo,
    })())
    service._holyman = _Holyman()

    service.delete_global_jargon("bot-alpha", word="叠甲")

    db = type("DB", (), {
        "scoped_knowledge": scoped_repo,
        "bot_jargon": bot_repo,
        "is_jargon_blocked": staticmethod(lambda word: False),
    })()
    injector = JargonInjector(db, max_inject=1, holyman_reference=_Holyman(), global_limit=2)
    assert "叠甲" not in injector.get_injection("叠甲", _group_scope())
    assert all(row["word"] != "叠甲" for row in service.list_global_jargon("bot-alpha"))


def test_injection_dedupes_word_across_sections(repo):
    """同词只出现在最靠前的一段，不会在三段里重复解释。"""
    bot_repo, scoped_repo, manager = repo
    scope = _group_scope()
    scoped_repo.upsert_scoped_jargon(scope, word="重复词", meaning="本群含义", status="confirmed", is_jargon=True)
    bot_repo.upsert_bot_jargon("bot-alpha", word="重复词", meaning="广域含义", confidence=0.9)

    db = type("DB", (), {
        "scoped_knowledge": scoped_repo,
        "bot_jargon": bot_repo,
        "is_jargon_blocked": staticmethod(lambda word: False),
    })()
    injector = JargonInjector(db, max_inject=1, holyman_reference=None, bot_global_limit=1, reference_limit=1)

    output = injector.get_injection("重复词", scope)

    assert output.count("重复词") == 1
    assert "本群含义" in output
