"""拦截 Agent 用 AstrBot 内置 shell / python / 文件工具直接碰 SQLite 数据库文件。

事故背景（2026-10-21、10-23）：记忆工具拒绝后，模型改用 ``astrbot_execute_shell`` /
``astrbot_execute_python`` 跑 ``sqlite3.connect('.../wave_memory.db')``。普通连接关闭时触发
WAL 检查点并删除 ``-wal``，而 AstrBot 进程仍持有已删除的 WAL 句柄继续写，导致库损坏。

AstrBot 的 ``on_using_llm_tool`` 钩子在工具执行前调用，但不能否决调用：钩子抛出的异常会被
吞掉，返回值也不看。它传入的 ``tool_args`` 与随后 ``**tool_args`` 执行用的是同一个 dict，
所以这里就地把命中的调用改写成无害版本（打印拒绝理由 / 指向一个必然失败的路径），
让模型在工具结果里看到明确理由。

这是尽力而为的字面匹配：字符串拼接、编码等混淆手段可以绕过，不能替代关闭工具或收紧权限。
"""

from __future__ import annotations

import os
from typing import Any

# 命中任一标记（不区分大小写）即拦截。
DB_ACCESS_MARKERS: tuple[str, ...] = (
    "wave_memory.db",
    "data_v4.db",
    ".db-wal",
    ".db-shm",
    "sqlite3",
)

GUARD_REASON = (
    "WaveMemory 已拦截此调用：禁止用 shell、python 或文件工具直接读写 SQLite 数据库文件"
    "（wave_memory.db、data_v4.db 及其 -wal/-shm）。直接连接正在运行的库会删除 WAL 文件并损坏数据库。"
    "需要记忆请用 WaveMemory 记忆工具；工具不可用（例如定时任务没有聊天上下文）时，"
    "请直接基于已有信息完成任务，或如实说明这次拿不到记忆。"
)

SHELL_TOOLS = frozenset({"astrbot_execute_shell"})
PYTHON_TOOLS = frozenset({"astrbot_execute_python", "astrbot_execute_ipython"})
SHELL_SESSION_TOOLS = frozenset({"astrbot_shell_session"})
# 工具名 → 需要改写成“必然失败路径”的参数名
FILE_TOOLS: dict[str, tuple[str, ...]] = {
    "astrbot_file_read_tool": ("path",),
    "astrbot_file_write_tool": ("path",),
    "astrbot_file_edit_tool": ("path",),
    "astrbot_grep_tool": ("path",),
    "astrbot_upload_file": ("local_path",),
    "astrbot_download_file": ("remote_path",),
}
# 文件工具里会被写进文件 / 当作搜索条件的文本参数，命中后一并清空。
_FILE_TEXT_ARGS = ("content", "old", "new", "pattern", "glob")

GUARDED_TOOLS = frozenset({*SHELL_TOOLS, *PYTHON_TOOLS, *SHELL_SESSION_TOOLS, *FILE_TOOLS})


def blocked_path() -> str:
    """一个任何平台上都无法读、写、建目录的路径：父级是本模块文件（普通文件不能当目录）。

    路径本身带着拒绝理由，文件工具报“找不到 / 不是目录”时模型能看到原因。
    """
    return os.path.join(os.path.abspath(__file__), "WAVEMEMORY_BLOCKED_禁止直接访问数据库文件_请改用记忆工具或直接说明拿不到记忆")


def _iter_strings(value: Any, depth: int = 0):
    if depth > 4:
        return
    if isinstance(value, str):
        yield value
    elif isinstance(value, dict):
        for key, item in value.items():
            if isinstance(key, str):
                yield key
            yield from _iter_strings(item, depth + 1)
    elif isinstance(value, (list, tuple, set)):
        for item in value:
            yield from _iter_strings(item, depth + 1)


def find_db_marker(tool_args: Any) -> str | None:
    """返回参数里命中的第一个数据库访问标记，没有则 None。"""
    for text in _iter_strings(tool_args):
        lowered = text.casefold()
        for marker in DB_ACCESS_MARKERS:
            if marker in lowered:
                return marker
    return None


def _shell_echo(message: str) -> str:
    # sh 与 PowerShell 都认单引号字面量；理由文本里没有单引号。
    return "echo '" + message.replace("'", "") + "'"


def guard_tool_call(tool_name: str, tool_args: Any) -> str | None:
    """命中时就地改写 ``tool_args`` 并返回命中的标记；未命中或不在管辖范围返回 None。"""
    name = str(tool_name or "")
    if name not in GUARDED_TOOLS or not isinstance(tool_args, dict):
        return None
    marker = find_db_marker(tool_args)
    if marker is None:
        return None

    if name in SHELL_TOOLS:
        tool_args["command"] = _shell_echo(GUARD_REASON)
        if "env" in tool_args:
            tool_args["env"] = {}
    elif name in PYTHON_TOOLS:
        tool_args["code"] = f"print({GUARD_REASON!r})"
    elif name in SHELL_SESSION_TOOLS:
        # 往已开的 shell 会话里敲命令：改成只列会话，不写任何字符。
        tool_args["action"] = "list"
        tool_args["chars"] = ""
    else:
        target = blocked_path()
        for key in FILE_TOOLS[name]:
            tool_args[key] = target
        for key in _FILE_TEXT_ARGS:
            if key in tool_args:
                tool_args[key] = ""
        if name == "astrbot_grep_tool":
            # 空 pattern 会被 grep 工具当参数错误；给一个无害的字面量。
            tool_args["pattern"] = "WAVEMEMORY_BLOCKED"
            tool_args.pop("glob", None)
    return marker


def tool_name_of(tool: Any) -> str:
    return str(getattr(tool, "name", "") or "")


__all__ = [
    "DB_ACCESS_MARKERS",
    "GUARD_REASON",
    "GUARDED_TOOLS",
    "blocked_path",
    "find_db_marker",
    "guard_tool_call",
    "tool_name_of",
]
