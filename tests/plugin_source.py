"""WaveMemoryPlugin 的源码视图（v6 起插件拆成 main.py + app/*.py 的 mixin）。

装配测试按方法名读源码、编译单个方法；这里把分散的文件合成一个视图，
测试不用关心某个方法落在哪个文件。
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PLUGIN_FILES = [ROOT / "main.py", *sorted((ROOT / "app").glob("*.py"))]


def plugin_source_text() -> str:
    """main.py 与全部 mixin 的源码拼接（字符串包含断言用）。"""
    return "\n".join(path.read_text(encoding="utf-8") for path in PLUGIN_FILES)


def plugin_methods() -> dict[str, ast.AST]:
    """WaveMemoryPlugin 及其 mixin 的全部方法（名字 → FunctionDef）。

    钩子在 main.py 里只是一行转发，真正的实现叫 ``_handle_*``；
    ``__init__`` 的组装逻辑在 ``BootstrapMixin._construct``。
    """
    methods: dict[str, ast.AST] = {}
    for path in PLUGIN_FILES:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in tree.body:
            if isinstance(node, ast.ClassDef) and (node.name == "WaveMemoryPlugin" or node.name.endswith("Mixin")):
                for item in node.body:
                    if isinstance(item, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        methods.setdefault(item.name, item)
    return methods


# 旧名字（v5 在 main.py 里的钩子与构造函数）→ v6 真正的实现
IMPLEMENTATION_OF = {
    "__init__": "_construct",
    "on_message": "_handle_message",
    "on_bot_sent": "_handle_bot_sent",
    "on_decorating_result": "_handle_decorating_result",
    "meta_thinking_check": "_handle_meta_thinking_check",
    "filter_bot_tools": "_handle_filter_bot_tools",
    "inject_memory": "_handle_inject_memory",
}


def plugin_method(name: str) -> ast.AST:
    methods = plugin_methods()
    return methods[IMPLEMENTATION_OF.get(name, name)]


def plugin_class_node() -> ast.ClassDef:
    """合成的 ClassDef：实现方法用旧名字，便于按 v5 名字取方法的测试。"""
    methods = plugin_methods()
    body = []
    for name, node in methods.items():
        if name in IMPLEMENTATION_OF and IMPLEMENTATION_OF[name] in methods:
            continue  # main.py 里的一行转发，由下面的实现副本代替
        body.append(node)
    for old, new in IMPLEMENTATION_OF.items():
        if new in methods:
            clone = ast.parse(ast.unparse(methods[new])).body[0]
            clone.name = old
            body.append(clone)
    return ast.ClassDef(name="WaveMemoryPlugin", bases=[], keywords=[], body=body, decorator_list=[], type_params=[])


def helper_nodes() -> dict[str, ast.AST]:
    """app/common.py 里的顶层辅助（_ObservationEvent 等）。"""
    tree = ast.parse((ROOT / "app" / "common.py").read_text(encoding="utf-8"))
    return {node.name: node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
