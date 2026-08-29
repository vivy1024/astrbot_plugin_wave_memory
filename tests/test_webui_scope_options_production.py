from __future__ import annotations

import sqlite3
from types import SimpleNamespace

from domain.scope import RuntimeScope
from services.config.channel_config import build_default_channel_config
from webui import WaveMemoryWebUI
from webui.container import ServiceContainer, get_container
from webui.scope_options import ExplicitRequestScopeProvider, RuntimeScopeOptionsSource


def _database():
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE memories (
            id INTEGER PRIMARY KEY,
            bot_id TEXT,
            session_id TEXT,
            resolution_state TEXT
        );
        CREATE TABLE user_profiles (
            id INTEGER PRIMARY KEY,
            user_id TEXT,
            group_id TEXT,
            bot_id TEXT
        );
        CREATE TABLE injection_traces (
            trace_id TEXT PRIMARY KEY,
            bot_id TEXT,
            bot_profile_id TEXT,
            group_id TEXT,
            metadata_json TEXT
        );
        CREATE TABLE injection_trace_channels (
            id INTEGER PRIMARY KEY,
            channel TEXT
        );
        """
    )
    connection.execute(
        "INSERT INTO memories VALUES (1,'bot-alpha','qq:group:g1','resolved')"
    )
    connection.execute(
        "INSERT INTO user_profiles VALUES (1,'u1','g1','bot-alpha')"
    )
    connection.execute(
        "INSERT INTO injection_traces VALUES ('t1','900000001','bot-alpha','g1',?)",
        ('{"runtime_scope":{"bot_id":"bot-alpha","visibility":"group","session":{"id":"discord:group:full-42","platform_id":"discord","kind":"group","conversation_id":"full-42"},"subject_principal_id":null}}',),
    )
    connection.execute("INSERT INTO injection_trace_channels VALUES (1,'facts')")
    connection.commit()
    return SimpleNamespace(conn=connection)


def _registry():
    return {
        "bot-alpha": SimpleNamespace(
            db_id="bot-alpha",
            qq_id="900000001",
            name="Synthetic Bot",
            aliases=["Alpha"],
        )
    }


def test_runtime_scope_options_use_registry_and_real_database_sources():
    db = _database()
    try:
        source = RuntimeScopeOptionsSource(
            db=db,
            bot_registry=_registry(),
            channel_config=build_default_channel_config(),
            group_name_resolver=lambda bot_id, group_id: "全名群聊" if (bot_id, group_id) == ("bot-alpha", "full-42") else None,
        )
        payload = source.get_scope_options()
    finally:
        db.conn.close()

    assert payload["bots"] == [
        {
            "db_id": "bot-alpha",
            "name": "Synthetic Bot",
            "qq_id": "900000001",
            "aliases": ["Alpha"],
            "status": "active",
        }
    ]
    assert [item["id"] for item in payload["sessions"]] == ["discord:group:full-42", "qq:group:g1"]
    assert payload["sessions"][0]["source"] == "traces"
    assert payload["sessions"][0]["group_name"] == "全名群聊"
    assert payload["sessions"][0]["label"] == "全名群聊（full-42）"
    assert payload["sessions"][0]["platform_id"] == "discord"
    assert payload["legacy_groups"] == []
    channels = {item["id"]: item for item in payload["channels"]}
    assert channels["facts"]["source"] == "runtime-config,traces"
    assert channels["facts"]["trace_count"] == 1
    assert payload["source"]["providers"] == [
        "bot_registry",
        "resolved_memories",
        "trace_runtime_scope",
        "formal_scoped_tables",
        "legacy_profiles",
        "legacy_relationships",
        "channel_registry",
    ]


def test_scope_options_formal_tables_report_capabilities_without_legacy_guess():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE scoped_facts (bot_id TEXT, session_id TEXT, visibility TEXT);
        CREATE TABLE scoped_soul_mood (bot_id TEXT, session_id TEXT, visibility TEXT);
        CREATE TABLE memories (bot_id TEXT, session_id TEXT, resolution_state TEXT);
        CREATE TABLE scoped_few_shot_examples (runtime_scope_json TEXT);
    """)
    session_id = "discord:group:formal-1"
    connection.execute("INSERT INTO memories VALUES ('bot-alpha',?, 'resolved')", (session_id,))
    connection.execute("INSERT INTO scoped_facts VALUES ('bot-alpha',?, 'group')", (session_id,))
    connection.execute("INSERT INTO scoped_facts VALUES ('bot-alpha',?, 'group')", (session_id,))
    connection.execute("INSERT INTO scoped_soul_mood VALUES ('bot-alpha',?, 'group')", (session_id,))
    connection.execute("INSERT INTO scoped_few_shot_examples VALUES (?)", ('{"bot_id":"bot-alpha","session":{"id":"discord:group:formal-1","platform_id":"discord","kind":"group","conversation_id":"formal-1"}}',))
    payload = RuntimeScopeOptionsSource(db=SimpleNamespace(conn=connection), bot_registry=_registry()).get_scope_options()
    item = payload["sessions"][0]
    assert item["id"] == session_id
    assert item["capabilities"]["scoped_facts"] == 2
    assert item["capabilities"]["scoped_soul_mood"] == 1
    assert item["capabilities"]["scoped_few_shot_examples"] == 1
    assert payload["legacy_groups"] == []


def test_scope_options_mark_platform_aliases_and_unresolved_numeric_groups():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE memories (bot_id TEXT, session_id TEXT, resolution_state TEXT);
        CREATE TABLE user_profiles (bot_id TEXT, group_id TEXT);
    """)
    connection.execute("INSERT INTO memories VALUES ('bot-alpha','qq:group:g1','resolved')")
    connection.execute("INSERT INTO memories VALUES ('bot-alpha','qq2:group:g1','resolved')")
    connection.execute("INSERT INTO user_profiles VALUES ('bot-alpha','g1')")
    connection.execute("INSERT INTO user_profiles VALUES ('bot-alpha','1015727706')")
    connection.execute("INSERT INTO user_profiles VALUES ('bot-alpha','arc01_炼气高中期')")
    payload = RuntimeScopeOptionsSource(db=SimpleNamespace(conn=connection), bot_registry=_registry()).get_scope_options()
    sessions = {item["id"]: item for item in payload["sessions"]}
    assert sessions["qq:group:g1"]["is_primary_alias"] is True
    assert sessions["qq2:group:g1"]["is_primary_alias"] is False
    assert sessions["qq2:group:g1"]["alias_session_ids"] == ["qq:group:g1", "qq2:group:g1"]
    assert payload["legacy_groups"] == [
        {"bot_id": "bot-alpha", "group_id": "1015727706", "label": "1015727706", "source": "profiles", "count": 1, "binding_status": "unresolved"}
    ]


def test_scope_options_ignore_top_level_qq_bot_and_trace_group_guess():
    connection = sqlite3.connect(":memory:")
    connection.executescript("""
        CREATE TABLE injection_traces (trace_id TEXT, bot_id TEXT, group_id TEXT, metadata_json TEXT);
        CREATE TABLE user_profiles (bot_id TEXT, group_id TEXT);
    """)
    connection.execute("INSERT INTO injection_traces VALUES ('bad','900000001','qq-group',NULL)")
    connection.execute("INSERT INTO user_profiles VALUES ('bot-alpha','1015727706')")
    db = SimpleNamespace(conn=connection)
    payload = RuntimeScopeOptionsSource(db=db, bot_registry=_registry()).get_scope_options()
    assert payload["sessions"] == []
    assert payload["legacy_groups"][0]["group_id"] == "1015727706"


def test_request_scope_provider_requires_explicit_registered_scope(monkeypatch):
    import webui.scope_options as scope_options

    provider = ExplicitRequestScopeProvider(bot_registry=_registry())
    monkeypatch.setattr(scope_options, "has_request_context", lambda: True)

    monkeypatch.setattr(
        scope_options,
        "request",
        SimpleNamespace(
            args={
                "bot_id": "bot-alpha",
                "visibility": "group",
                "session_id": "qq:group:g1",
                "subject_principal_id": "qq:user:42",
            },
            headers={},
        ),
    )
    scope = provider.get_request_scope()
    assert isinstance(scope, RuntimeScope)
    assert scope.bot_id == "bot-alpha"
    assert scope.session is not None and scope.session.id == "qq:group:g1"

    scope_options.request = SimpleNamespace(
        args={"bot_id": "900000001", "visibility": "group", "session_id": "qq:group:g1"},
        headers={},
    )
    assert provider.get_request_scope() is None
    scope_options.request = SimpleNamespace(args={}, headers={})
    assert provider.get_request_scope() is None


def test_wave_memory_webui_composes_production_scope_providers():
    ServiceContainer.reset()
    db = _database()
    try:
        webui = WaveMemoryWebUI(
            db=db,
            query_engine=None,
            embedding_service=None,
            memory_index=None,
            tag_index=None,
            cooccurrence=None,
            bot_registry=_registry(),
            injection_channel_config=build_default_channel_config(),
        )
        container = get_container()
        assert isinstance(container.scope_options_source, RuntimeScopeOptionsSource)
        assert isinstance(container.request_scope_provider, ExplicitRequestScopeProvider)
        assert webui._server is not None
    finally:
        db.conn.close()
        ServiceContainer.reset()
