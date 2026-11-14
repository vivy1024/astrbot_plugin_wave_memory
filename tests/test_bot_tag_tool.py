"""羽书在对话里顺手标记「这位发言者是不是 Bot」：工具、social 族 mode、边界与不再有规则判定。"""

from __future__ import annotations

import asyncio
import sqlite3
from types import SimpleNamespace

from domain.scope import RuntimeScope, SessionRef
from services.bot_discovery import BotDiscoveryService
from tools.bot_tag import BOT_TAG_SOURCE, WaveMemoryNoteBotTagTool


def _scope(group: str = "g1") -> RuntimeScope:
    return RuntimeScope(
        "bot-alpha",
        "group",
        SessionRef(f"qq:group:{group}", "qq", "group", group),
        subject_principal_id="qq:user:10001",
    )


def _ctx(scope: RuntimeScope) -> SimpleNamespace:
    event = SimpleNamespace(_wave_memory_runtime_scope=scope, get_sender_id=lambda: "10001", get_group_id=lambda: "g1")
    return SimpleNamespace(context=SimpleNamespace(event=event))


def _setup(tmp_path, *, system_bots=()):
    conn = sqlite3.connect(str(tmp_path / "tag.db"), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute(
        "CREATE TABLE person_registry (qq_id TEXT PRIMARY KEY, display_name TEXT, aliases TEXT, "
        "first_seen REAL, last_seen REAL, message_count INTEGER DEFAULT 0, groups TEXT, metadata TEXT)"
    )
    conn.executemany(
        "INSERT INTO person_registry (qq_id, display_name, aliases, groups, message_count) VALUES (?, ?, '[]', ?, 5)",
        [("10001", "小明", '["g1"]'), ("20002", "复读姬", '["g1"]'), ("30003", "路人", '["g1"]')],
    )
    # 目标必须在当前群里出现过（resolve_user_id 的既有边界），这里用 memories 记录表示「说过话」
    conn.execute(
        "CREATE TABLE memories (id INTEGER PRIMARY KEY, sender_id TEXT, sender_name TEXT, bot_id TEXT, group_id TEXT)"
    )
    conn.executemany(
        "INSERT INTO memories (sender_id, bot_id, group_id) VALUES (?, 'bot-alpha', 'g1')",
        [("10001",), ("20002",), ("30003",)],
    )
    conn.commit()
    discovery = BotDiscoveryService(conn, system_bot_ids=set(system_bots))
    db = SimpleNamespace(conn=conn)
    tool = WaveMemoryNoteBotTagTool(db=db, discovery_provider=lambda: discovery)
    return conn, discovery, tool


def _call(tool, **kwargs):
    return asyncio.run(tool.call(_ctx(_scope()), **kwargs))


def test_model_can_mark_a_speaker_as_bot_and_it_is_stored(tmp_path):
    conn, discovery, tool = _setup(tmp_path)
    try:
        result = _call(tool, target_user="20002", verdict="bot", reason="三次回得一字不差")
        assert "记下了" in result and "不主动接" in result
        tag = discovery.get_tag("20002")
        assert tag["entity_kind"] == "suspected_bot" and tag["is_bot"] is True
        assert tag["source"] == BOT_TAG_SOURCE and tag["reason"] == "三次回得一字不差"
    finally:
        conn.close()


def test_model_can_take_it_back(tmp_path):
    conn, discovery, tool = _setup(tmp_path)
    try:
        _call(tool, target_user="20002", verdict="bot", reason="像机器人")
        result = _call(tool, target_user="20002", verdict="human", reason="后来发现是真人在玩梗")
        assert "是真人" in result
        assert discovery.get_tag("20002")["entity_kind"] == "human"
    finally:
        conn.close()


def test_admin_decisions_and_system_accounts_are_never_overwritten(tmp_path):
    conn, discovery, tool = _setup(tmp_path, system_bots={"30003"})
    try:
        discovery.set_tag("20002", entity_kind="verified_human", source="admin", reason="真人朋友")
        assert "管理员已确认" in _call(tool, target_user="20002", verdict="bot", reason="觉得像")
        assert discovery.get_tag("20002")["entity_kind"] == "verified_human"

        assert "本系统自己的 Bot 账号" in _call(tool, target_user="30003", verdict="human", reason="看走眼")
        assert discovery.get_tag("30003")["entity_kind"] == "confirmed_bot"
    finally:
        conn.close()


def test_required_fields_verdict_and_unknown_target_are_rejected(tmp_path):
    conn, discovery, tool = _setup(tmp_path)
    try:
        assert "均为必填项" in _call(tool, target_user="20002", verdict="bot")
        assert "verdict 必须是" in _call(tool, target_user="20002", verdict="maybe", reason="不确定")
        assert "没有在当前群找到" in _call(tool, target_user="根本没这个人", verdict="bot", reason="随便")
        assert discovery.get_tag("20002")["entity_kind"] == "human"
    finally:
        conn.close()


def test_private_chat_is_rejected(tmp_path):
    conn, discovery, tool = _setup(tmp_path)
    try:
        private = RuntimeScope(
            "bot-alpha", "private", SessionRef("qq:private:10001", "qq", "private", "10001"),
            subject_principal_id="qq:user:10001",
        )
        result = asyncio.run(tool.call(_ctx(private), target_user="20002", verdict="bot", reason="像"))
        assert "记下了" not in result
        assert discovery.get_tag("20002")["entity_kind"] == "human"
    finally:
        conn.close()


def test_tool_is_not_ready_without_discovery_service(tmp_path):
    conn, _, _ = _setup(tmp_path)
    try:
        tool = WaveMemoryNoteBotTagTool(db=SimpleNamespace(conn=conn), discovery_provider=lambda: None)
        assert "未就绪" in _call(tool, target_user="20002", verdict="bot", reason="像")
    finally:
        conn.close()


def test_tool_is_resolved_lazily_because_discovery_is_built_after_tools(tmp_path):
    conn, discovery, _ = _setup(tmp_path)
    try:
        holder = {"svc": None}
        tool = WaveMemoryNoteBotTagTool(db=SimpleNamespace(conn=conn), discovery_provider=lambda: holder["svc"])
        assert "未就绪" in _call(tool, target_user="20002", verdict="bot", reason="像")
        holder["svc"] = discovery  # 启动后期才创建的服务
        assert "记下了" in _call(tool, target_user="20002", verdict="bot", reason="像")
    finally:
        conn.close()


def test_merged_social_family_exposes_bot_check_mode():
    from domain.tool_names import LEGACY_TO_MERGED, NOTE_BOT_TAG, SOCIAL, normalize_tool_ref
    from tools import merged_families as F
    from tools.merged import union_schema

    assert LEGACY_TO_MERGED[NOTE_BOT_TAG] == (SOCIAL, "bot_check")
    assert normalize_tool_ref(NOTE_BOT_TAG) == (SOCIAL, "bot_check")
    assert normalize_tool_ref(f"{SOCIAL}:bot_check") == (SOCIAL, "bot_check")

    tool = WaveMemoryNoteBotTagTool(db=None, discovery_provider=lambda: None)
    spec = next(s for s in F.SOCIAL_MODES if s.mode == "bot_check")
    schema = union_schema([(spec, tool.parameters)], mode_description="x")
    assert set(schema["properties"]) >= {"mode", "target_user", "verdict", "reason"}
    assert schema["properties"]["verdict"]["enum"] == ["bot", "human"]
    assert "bot_check" in schema["properties"]["mode"]["enum"]


def test_tool_is_registered_in_merged_surface():
    from tools.builtin_registry import builtin_tool_specs

    merged = builtin_tool_specs()
    merged_keys = {spec.key for spec in merged}
    # 合并形态下它是内部子工具（不直接给模型），模型通过 wave_memory_social 的 bot_check 调用
    hidden = {spec.key for spec in merged if not spec.llm_visible}
    assert "wave_memory_note_bot_tag" in merged_keys and "wave_memory_note_bot_tag" in hidden
    assert "wave_memory_social" in merged_keys
