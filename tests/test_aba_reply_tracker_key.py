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
