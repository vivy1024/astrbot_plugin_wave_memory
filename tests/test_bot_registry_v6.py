"""v6 多 Bot：Profile、仓储、注册表迁移与热重载、会话前缀。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from domain import bot_identity
from domain.bot_profile import BotProfile, BotProfileError
from engine.db.bot_profile_repo import BotProfileConflict, BotProfileRepo
from engine.db.connection import ConnectionManager
from services.bot_registry import BotConflictError, BotRegistry, legacy_profiles_from_config
from services.scopes import BotIdentityBinding, ScopeResolutionError, ScopeResolver, bindings_from_profiles

LEGACY_CONFIG = {
    "MetaThinking_Bot1": {"qq_id": "2500447291", "name": "羽书", "db_id": "yushu", "aliases": "羽书bot,器灵"},
    "MetaThinking_Bot2": {"qq_id": "1336495069", "name": "白真真", "db_id": "baizz", "aliases": "阿真"},
    "MetaThinking_Bot3": {"qq_id": "", "name": "空槽位", "db_id": "empty"},
}


@pytest.fixture(autouse=True)
def reset_identity_snapshot():
    yield
    bot_identity.publish()


@pytest.fixture
def cm(tmp_path):
    manager = ConnectionManager(str(tmp_path / "bots.sqlite3"))
    with manager.migration_transaction() as tx:
        tx.execute(
            "CREATE TABLE memories (id INTEGER PRIMARY KEY, bot_id TEXT, session_id TEXT, content TEXT)"
        )
        rows = [("yushu", "羽书:group:1")] * 5 + [("yushu", "renamed:group:2")] + [("baizz", "白真真:private:9")] * 3
        tx.executemany("INSERT INTO memories(bot_id, session_id, content) VALUES (?, ?, 'x')", rows)
    yield manager
    manager.close()


def _profile(db_id="newbot", name="新角色", qq_id="30003", **extra) -> BotProfile:
    return BotProfile.from_dict({"db_id": db_id, "name": name, "qq_id": qq_id, **extra})


# ---------------------------------------------------------------- Profile


def test_profile_round_trip_and_validation():
    profile = _profile(
        bindings=[{"host": "cortico", "deployment": "live-a"}, {"host": "bilibili", "room": "123"}],
        persona={"self_terms": "小助手", "lore_lines": "第一条\n第二条", "experience_source": "newbot_experience"},
        tools_deny=["wave_memory_search"],
    )
    again = BotProfile.from_dict(profile.to_dict())
    assert again == profile
    assert again.persona.lore_lines == ["第一条", "第二条"]
    assert again.identity_terms == ["新角色", "小助手"]
    assert not again.tool_enabled("wave_memory_search")
    assert again.tool_enabled("wave_memory_facts")

    for bad in ("", "123", "bot", "a b", "x:y"):
        with pytest.raises(BotProfileError):
            _profile(db_id=bad)
    with pytest.raises(BotProfileError):
        _profile(qq_id="abc")
    with pytest.raises(BotProfileError):
        BotProfile.from_dict({"db_id": "x1", "name": "x", "bindings": [{"host": "cortico"}]})


def test_legacy_slots_are_numbered_and_incomplete_ones_skipped():
    profiles = legacy_profiles_from_config(LEGACY_CONFIG)
    assert [p.db_id for p in profiles] == ["yushu", "baizz"]
    assert profiles[0].aliases == ["羽书bot", "器灵"]


# ---------------------------------------------------------------- Repo


def test_repo_optimistic_lock_and_history(cm):
    repo = BotProfileRepo(cm)
    saved = repo.save(_profile(), expected_version=0)
    assert saved.version == 1
    data = saved.to_dict()
    data["aliases"] = ["别名"]
    updated = repo.save(BotProfile.from_dict(data), expected_version=1, changed_by="tester", reason="改别名")
    assert updated.version == 2 and updated.aliases == ["别名"]
    with pytest.raises(BotProfileConflict):
        repo.save(BotProfile.from_dict(data), expected_version=1)
    history = repo.history("newbot")
    assert history[0]["version"] == 1 and history[0]["changed_by"] == "tester"


# ---------------------------------------------------------------- Registry


def test_registry_migrates_legacy_slots_once_with_detected_prefix(cm):
    repo = BotProfileRepo(cm)
    registry = BotRegistry(LEGACY_CONFIG)
    report = registry.attach(repo, connection=cm)
    assert report["migrated"] == ["yushu", "baizz"]

    yushu = registry.get("yushu")
    baizz = registry.get("baizz")
    # 最常用的历史前缀，历史数据零迁移。
    assert yushu.session_prefix == "羽书"
    assert baizz.session_prefix == "白真真"
    # v5 写死在代码里的人设片段按 db_id 迁进 Profile。
    assert "器灵" in yushu.persona.self_terms and yushu.persona.lore_lines
    assert baizz.experience_source == "bzz_experience"

    # 第二次启动：数据库为准，静态配置改名不覆盖。
    changed = dict(LEGACY_CONFIG)
    changed["MetaThinking_Bot1"] = dict(changed["MetaThinking_Bot1"], name="改了名")
    again = BotRegistry(changed)
    assert again.attach(repo, connection=cm)["migrated"] == []
    assert again.get("yushu").name == "羽书"

    # 身份快照供无 Bot 上下文的模块使用。
    assert "2500447291" in bot_identity.bot_sender_ids()
    assert "器灵" in bot_identity.identity_terms()
    assert bot_identity.is_experience_source("bzz_experience")


def test_registry_hot_reload_updates_shared_dict_and_notifies(cm):
    registry = BotRegistry(LEGACY_CONFIG)
    registry.attach(BotProfileRepo(cm), connection=cm)
    shared = registry.profiles  # v5 调用方持有的引用
    seen: list[int] = []
    registry.add_listener(lambda reg: seen.append(len(reg.all())))

    third = registry.save(
        _profile(bindings=[{"host": "cortico", "deployment": "live-c"}]),
        expected_version=0,
        changed_by="webui",
    )
    assert third.version == 1
    assert "30003" in shared and shared["30003"].db_id == "newbot"
    assert registry.resolve(deployment="live-c").db_id == "newbot"
    assert seen == [3]

    registry.set_enabled("newbot", False, expected_version=1)
    assert "30003" not in shared
    assert registry.get("newbot") is None and registry.get_any("newbot") is not None
    assert "新角色" not in bot_identity.identity_terms()


def test_registry_rejects_shared_accounts_and_prefixes(cm):
    registry = BotRegistry(LEGACY_CONFIG)
    registry.attach(BotProfileRepo(cm), connection=cm)
    with pytest.raises(BotConflictError):
        registry.save(_profile(qq_id="2500447291"), expected_version=0)
    with pytest.raises(BotConflictError):
        registry.save(_profile(session_prefix="羽书"), expected_version=0)


def test_registry_import_export(cm):
    registry = BotRegistry(LEGACY_CONFIG)
    registry.attach(BotProfileRepo(cm), connection=cm)
    exported = registry.export()
    assert {item["db_id"] for item in exported} == {"yushu", "baizz"}
    result = registry.import_profiles([_profile().to_dict(), {"db_id": "bot", "name": "x"}])
    assert result["imported"] == ["newbot"]
    assert "bot" in result["errors"]


def test_registry_without_database_uses_legacy_slots():
    registry = BotRegistry(LEGACY_CONFIG)
    assert not registry.attached
    assert set(registry.profiles) == {"2500447291", "1336495069"}
    with pytest.raises(BotProfileError):
        registry.save(_profile(), expected_version=0)


# ---------------------------------------------------------------- Scope


def _event(self_id="2500447291", platform="羽书平台改名后", kind="GroupMessage", group="100", sender="u1"):
    return SimpleNamespace(
        get_self_id=lambda: self_id,
        get_sender_id=lambda: sender,
        get_platform_id=lambda: platform,
        get_message_type=lambda: kind,
        get_group_id=lambda: group,
        get_session_id=lambda: sender,
    )


def test_scope_resolver_uses_canonical_prefix_after_platform_rename():
    profile = BotProfile.from_dict({
        "db_id": "yushu", "name": "羽书", "qq_id": "2500447291", "session_prefix": "羽书",
        "bindings": [{"host": "astrbot", "self_id": "2500447292"}],
    })
    resolver = ScopeResolver(bindings_from_profiles([profile]))
    resolved = resolver.resolve_event(_event())
    assert resolved.scope.session.id == "羽书:group:100"
    assert resolved.scope.subject_principal_id == "羽书:user:u1"
    assert resolved.host_platform_id == "羽书平台改名后"
    # 同一个 Bot 的第二个 QQ 账号落到同一个 db_id、同一条会话线。
    second = resolver.resolve_event(_event(self_id="2500447292"))
    assert second.scope.bot_id == "yushu" and second.scope.session.id == "羽书:group:100"


def test_scope_resolver_still_rejects_ambiguous_duplicates():
    with pytest.raises(ScopeResolutionError):
        ScopeResolver([
            BotIdentityBinding(self_id="1", db_id="a"),
            BotIdentityBinding(self_id="2", db_id="a"),
        ])
    with pytest.raises(ScopeResolutionError):
        BotIdentityBinding(self_id="1", db_id="a", session_prefix="bad:prefix")


# ---------------------------------------------------------------- 身份安全


def test_identity_safety_follows_registered_bot_names():
    from services.identity_safety import is_identity_contamination

    text = "新角色签了奴隶契约"
    bot_identity.publish(identity_terms=["新角色"])
    assert is_identity_contamination(text)
    bot_identity.publish(identity_terms=[])
    # 名字不在注册表里时，"新角色"不再被当成指向 Bot 的词。
    assert not is_identity_contamination("新角色的剧情设定里有契约")


def test_person_identity_accepts_chinese_session_prefix(cm):
    from engine.db.person_identity_repo import PersonIdentityRepo

    repo = PersonIdentityRepo(cm)
    key = repo.link("yushu", ["羽书:user:123", "bilibili:user:456"])
    assert key.startswith("person:")
    assert set(repo.linked_principals("yushu", "羽书:user:123")) == {"羽书:user:123", "bilibili:user:456"}
