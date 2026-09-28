"""ABA 连续对话：回复记录的写入与读取必须用同一个键（此前两边格式不同，规则从未触发）。"""

from __future__ import annotations

import ast
from pathlib import Path

SOURCE = Path("app/ingress.py").read_text(encoding="utf-8")
TREE = ast.parse(SOURCE)


def _function(name: str) -> ast.AST:
    for node in ast.walk(TREE):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"找不到 {name}")


def _calls(node: ast.AST, name: str) -> int:
    return sum(
        1 for item in ast.walk(node)
        if isinstance(item, ast.Call) and isinstance(item.func, ast.Name) and item.func.id == name
    )


def test_write_and_read_share_reply_tracker_key():
    assert _calls(_function("_process_bot_reply"), "reply_tracker_key") == 1
    assert _calls(_function("_should_engage"), "reply_tracker_key") == 1


def test_key_is_bot_group_sender():
    namespace: dict = {}
    exec(ast.get_source_segment(SOURCE, _function("reply_tracker_key")), namespace)
    key = namespace["reply_tracker_key"]
    assert key("yushu", "398291136", "10000002") == "yushu:398291136:10000002"
    assert key("yushu", "398291136", "1") != key("baizz", "398291136", "1")


def test_bot_reply_uses_its_own_idempotency_key():
    """回复不能拿触发它的群友消息号当幂等号，否则与那条消息撞键被当重放丢弃。"""
    from services.inbound_message_handler import bot_reply_event_id

    assert _calls(_function("_process_bot_reply"), "bot_reply_event_id") == 1
    user_message_id = 123456
    first = bot_reply_event_id(user_message_id, "那是，本座随手推演的法相")
    second = bot_reply_event_id(user_message_id, "都说了这是赛博法相")
    assert first != str(user_message_id) and first.startswith("self:123456:")
    assert first != second  # 同一条消息引出的多段回复各自入库
    assert first == bot_reply_event_id(user_message_id, "那是，本座随手推演的法相")  # 重复投递仍去重
    assert bot_reply_event_id(None, "x") is None and bot_reply_event_id("", "x") is None
