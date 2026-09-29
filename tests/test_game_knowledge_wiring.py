"""游戏知识库的装配：启动时建服务、Runtime 准备器记当前游戏、通道与工具从插件取服务、管理台容器拿得到。"""

from __future__ import annotations

import ast
import logging
from types import SimpleNamespace

from plugin_source import plugin_method, plugin_source_text

from domain.game_knowledge import parse_game_context
from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from services.game_knowledge import GameKnowledgeService
from services.injection.channel_registry import ChannelRegistry, ChannelSpec
from services.injection.channels.game_knowledge import GameKnowledgeChannel


def _load(name: str):
    method = plugin_method(name)
    source = ast.unparse(method).replace("from ..services.game_knowledge import", "from services.game_knowledge import")
    namespace = {"logger": logging.getLogger("test"), "_record_err": lambda *args: namespace.setdefault("errors", []).append(args)}
    exec(compile(source, "bootstrap.py", "exec"), namespace)
    return namespace[name], namespace


def test_bootstrap_builds_service_and_records_active_game(tmp_path):
    build, _ = _load("_build_game_knowledge")
    note, _ = _load("_note_active_game")
    db = WaveMemoryDB(str(tmp_path / "wave.sqlite"), dimension=3)
    try:
        plugin = SimpleNamespace(db=db)
        plugin.game_knowledge = build(plugin)
        assert isinstance(plugin.game_knowledge, GameKnowledgeService)
        scope = RuntimeScope("yushu", "group", SessionRef("bilibili:group:20000001", "bilibili", "group", "20000001"))
        note(plugin, scope, parse_game_context({"id": "minecraft"}))
        assert plugin.game_knowledge.active_game(scope) == ("minecraft", "session")
        note(SimpleNamespace(game_knowledge=None), scope, parse_game_context({"id": "oni"}))  # 服务没起来时不报错
    finally:
        db.close()


def test_bootstrap_failure_only_disables_game_knowledge():
    build, namespace = _load("_build_game_knowledge")
    assert build(SimpleNamespace(db=SimpleNamespace())) is None
    assert namespace["errors"][0][0] == "GameKnowledge"


def test_channel_factory_takes_service_from_plugin():
    # 其他内置通道模块依赖 AstrBot，这里静态取出 game_knowledge 的工厂再执行（与 test_channel_registry 同一做法）
    from pathlib import Path

    tree = ast.parse(Path("services/injection/channel_registry.py").read_text(encoding="utf-8"))
    specs = [
        node for node in ast.walk(tree)
        if isinstance(node, ast.Call) and getattr(node.func, "id", "") == "ChannelSpec"
        and node.args and isinstance(node.args[0], ast.Constant) and node.args[0].value == "game_knowledge"
    ]
    assert len(specs) == 1
    factory = eval(compile(ast.Expression(specs[0].args[1]), "channel_registry.py", "eval"), {"GameKnowledgeChannel": GameKnowledgeChannel})
    registry = ChannelRegistry()
    registry.register(ChannelSpec("game_knowledge", factory))
    service = object()
    channels = registry.build(SimpleNamespace(game_knowledge=service))
    assert [channel.name for channel in channels] == ["game_knowledge"] and channels[0].service is service
    assert registry.build(SimpleNamespace())[0].service is None, "插件还没有服务时通道照样构造，运行时报告不可用"


def test_startup_wiring_in_source():
    source = plugin_source_text()
    assert "self.game_knowledge = self._build_game_knowledge()" in source
    assert "game_context_sink=self._note_active_game" in source
    assert 'get_container().game_knowledge = getattr(self, "game_knowledge", None)' in source
