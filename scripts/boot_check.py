"""插件完整启动自检：真实 AstrBot + 临时空数据目录，不碰运行中的插件与真实数据库。

用法（在装了 AstrBot 的环境里，例如 astrbot 容器内）：

    python boot_check.py <插件父目录> [AstrBot 源码目录，默认 /AstrBot]

插件父目录下要有 astrbot_plugin_wave_memory/（通常是 git archive 解出来的副本）。
WebUI 开在 19876 端口，结束后关闭。任何一步失败退出码非 0。
"""

from __future__ import annotations

import asyncio
import importlib
import json
import os
import sys
import tempfile
import urllib.request
from types import SimpleNamespace

PLUGIN_PARENT = sys.argv[1] if len(sys.argv) > 1 else os.getcwd()
ASTRBOT_ROOT = sys.argv[2] if len(sys.argv) > 2 else "/AstrBot"
sys.path.insert(0, ASTRBOT_ROOT)
sys.path.insert(0, PLUGIN_PARENT)



def _install_hnswlib_shim() -> None:
    """没有 hnswlib 的机器（如 Windows 无编译器）用暴力 numpy 近似代替，只为验证装配。"""
    try:
        import hnswlib  # noqa: F401
        return
    except ImportError:
        pass
    import pickle
    import types

    import numpy as np

    class Index:
        def __init__(self, space="cosine", dim=1024):
            self.dim, self.vectors, self.deleted, self.max = dim, {}, set(), 0

        def init_index(self, max_elements=0, **_):
            self.max = max_elements

        def load_index(self, path, max_elements=0, **_):
            with open(path, "rb") as fh:
                self.vectors = pickle.load(fh)
            self.max = max(max_elements, len(self.vectors))

        def save_index(self, path):
            with open(path, "wb") as fh:
                pickle.dump(self.vectors, fh)

        def add_items(self, data, ids=None, **_):
            data = np.asarray(data, dtype=np.float32).reshape(-1, self.dim)
            for vec, i in zip(data, ids if ids is not None else range(len(self.vectors), len(self.vectors) + len(data))):
                self.vectors[int(i)] = vec

        def knn_query(self, data, k=1, **_):
            data = np.asarray(data, dtype=np.float32).reshape(-1, self.dim)
            ids = [i for i in self.vectors if i not in self.deleted]
            if not ids:
                return np.zeros((len(data), 0), dtype=np.int64), np.zeros((len(data), 0), dtype=np.float32)
            mat = np.stack([self.vectors[i] for i in ids])
            norms = np.linalg.norm(mat, axis=1) * np.linalg.norm(data, axis=1)[:, None] + 1e-9
            dist = 1 - (data @ mat.T) / norms
            order = np.argsort(dist, axis=1)[:, :k]
            return np.array(ids)[order], np.take_along_axis(dist, order, axis=1)

        def mark_deleted(self, i):
            self.deleted.add(int(i))

        def resize_index(self, n):
            self.max = n

        def set_ef(self, _):
            pass

        def get_current_count(self):
            return len(self.vectors)

        def get_max_elements(self):
            return self.max

    sys.modules["hnswlib"] = types.SimpleNamespace(Index=Index)
    print("[WARN] hnswlib 不可用，使用 numpy 替身（只验证装配，不代表向量检索性能）")


_install_hnswlib_shim()

import astrbot.core.utils.astrbot_path as astrbot_path  # noqa: E402

DATA = tempfile.mkdtemp(prefix="wm-boot-check-")
astrbot_path.get_astrbot_data_path = lambda: DATA
PORT = 19876


class FakeContext:
    def __init__(self):
        self.tools = []
        self.platform_manager = SimpleNamespace(get_insts=lambda: [])

    def add_llm_tools(self, *tools):
        self.tools.extend(tools)

    def get_llm_tool_manager(self):
        def remove_func(name):
            for i, tool in enumerate(self.tools):
                if tool.name == name:
                    self.tools.pop(i)
                    break

        return SimpleNamespace(remove_func=remove_func)

    def get_provider_by_id(self, _id):
        return None

    def __getattr__(self, name):
        return lambda *a, **k: None


CONFIG = {
    "MetaThinking_Bot1": {"qq_id": "2500447291", "name": "羽书", "db_id": "yushu", "aliases": "羽书bot,器灵"},
    "MetaThinking_Bot2": {"qq_id": "1336495069", "name": "白真真", "db_id": "baizz"},
    "WebUI_Settings": {"webui_enabled": True, "webui_port": PORT, "webui_host": "127.0.0.1", "webui_password": ""},
}


def _http(path, body=None):
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"http://127.0.0.1:{PORT}{path}",
        data=data,
        headers={"Content-Type": "application/json", "Authorization": "Bearer yushu-dev-token"},
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read())


def check(label, ok, detail=""):
    print(f"[{'OK' if ok else 'FAIL'}] {label} {detail}")
    if not ok:
        raise SystemExit(1)


EXTENSION = """
from astrbot_plugin_wave_memory.services.tool_registry import ToolSpec
from astrbot_plugin_wave_memory.services.injection.channel_registry import ChannelSpec
from astrbot_plugin_wave_memory.services.config.channel_config import ChannelConfig
from astrbot_plugin_wave_memory.services.injection.channel_base import InjectionResult


class Tool:
    name = "boot_ext_tool"
    description = "boot check extension"
    parameters = {"type": "object", "properties": {}}

    async def call(self, context, **kwargs):
        return "VERSION"


class Channel:
    name = "boot_ext_channel"

    async def build(self, ctx):
        return InjectionResult.empty(self.name, reason="boot check")


def register(tools, channel_registry=None, **_):
    tools.register(ToolSpec("boot_ext_tool", lambda deps: Tool()))
    channel_registry.register(ChannelSpec(
        "boot_ext_channel", lambda deps, built: Channel(),
        default_config=ChannelConfig(name="boot_ext_channel", enabled=True, priority=90),
    ))
"""


async def _check_extension_reload(plugin, ctx, loop, scope):
    ext_dir = os.path.join(plugin.data_dir, "extensions")
    os.makedirs(ext_dir, exist_ok=True)
    for version in ("v1", "v2"):
        with open(os.path.join(ext_dir, "boot_ext.py"), "w", encoding="utf-8") as f:
            f.write(EXTENSION.replace("VERSION", version))
        result = await loop.run_in_executor(None, _http, "/api/extensions/reload", {})
        called = await loop.run_in_executor(None, _http, "/api/runtime/v1/tools/boot_ext_tool", {"scope": scope, "arguments": {}})
        host_copies = [t for t in ctx.tools if t.name == "boot_ext_tool"]
        names = [getattr(ch, "name", "") for ch in plugin.injection_shadow_channels]
        check(
            f"extension reload {version}",
            result.get("ok") and called.get("result") == version and len(host_copies) == 1
            and names.count("boot_ext_channel") == 1 and len(names) == 13,
            f"{result.get('tools')} {result.get('channels')} -> {called.get('result')}",
        )


async def _check_service_reconfigure(plugin, loop):
    old_dream = plugin.dream_service
    saved = await loop.run_in_executor(None, _http, "/api/config/full", {"Lifecycle_Settings": {"dream_interval_hours": "3.0"}})
    service = (saved.get("apply_modes") or {}).get("service", {})
    check(
        "static config rebuilds dream only",
        saved.get("ok") and service.get("dream", {}).get("action") == "rebuilt"
        and plugin.dream_service is not old_dream and plugin.dream_interval_hours == 3.0
        and list(service) == ["dream"],
        saved.get("message", ""),
    )
    saved = await loop.run_in_executor(None, _http, "/api/config/full", {"Eviction_Settings": {"enabled": False}})
    service = (saved.get("apply_modes") or {}).get("service", {})
    check("static config disables eviction", service.get("eviction", {}).get("action") == "disabled" and plugin.eviction_service is None)
    saved = await loop.run_in_executor(None, _http, "/api/config/full", {"Eviction_Settings": {"enabled": True}})
    service = (saved.get("apply_modes") or {}).get("service", {})
    check("static config re-creates eviction", service.get("eviction", {}).get("action") == "created" and plugin.eviction_service is not None)


async def run():
    main = importlib.import_module("astrbot_plugin_wave_memory.main")
    from astrbot_plugin_wave_memory.domain.bot_profile import BotProfile

    ctx = FakeContext()
    plugin = main.WaveMemoryPlugin(ctx, CONFIG)
    await plugin.initialize()
    loop = asyncio.get_running_loop()
    try:
        check("bots migrated", {p.db_id for p in plugin.bot_registry.all()} == {"yushu", "baizz"}, plugin.bot_registry.source)
        check("tools registered", len(ctx.tools) >= 10, str(len(ctx.tools)))
        check("channels built", len(plugin.injection_shadow_channels) == 12)
        check("services registered", len(plugin.service_registry.status()) >= 5)
        plugin.bot_registry.save(BotProfile.from_dict({"db_id": "bot_c", "name": "丙", "qq_id": "30003"}), expected_version=0)
        event = SimpleNamespace(
            get_self_id=lambda: "30003", get_sender_id=lambda: "u1", get_platform_id=lambda: "丙",
            get_message_type=lambda: "GroupMessage", get_group_id=lambda: "42", get_session_id=lambda: "u1",
        )
        check("hot-added bot resolves", plugin.scope_resolver.resolve_event(event).scope.bot_id == "bot_c")
        caps = await loop.run_in_executor(None, _http, "/api/runtime/v1/capabilities?bot_id=yushu")
        check("capabilities", bool(caps["tools"]) and caps["bot"]["db_id"] == "yushu", f"{len(caps['tools'])} tools")
        scope = {"bot_id": "yushu", "visibility": "group", "session": {"id": "羽书:group:42"}}
        obs = await loop.run_in_executor(None, _http, "/api/runtime/v1/observations/batch", {"events": [
            {"event_id": 9001, "content": "记住 周五一起打尖塔", "sender_id": "u1", "sender_name": "阿一", "scope": scope},
            {"event_id": 9002, "kind": "self", "content": "好呀周五见", "scope": scope},
        ]})
        check("observations", obs["accepted"] == 2)
        prep = await loop.run_in_executor(None, _http, "/api/runtime/v1/context/prepare", {"text": "周五打尖塔吗", "speaker": {"id": "u1"}, "tier": "light", "scope": scope})
        check("context/prepare", prep.get("ok") is True and "book_lore" not in prep["channels"], prep.get("trace_id", ""))
        tool = await loop.run_in_executor(None, _http, "/api/runtime/v1/tools/wave_memory_facts", {"scope": scope, "arguments": {"query": "尖塔"}})
        check("tool call", tool.get("ok") is True)
        await _check_extension_reload(plugin, ctx, loop, scope)
        await _check_service_reconfigure(plugin, loop)
        for path in ("/api/bots", "/api/config/inventory", "/api/services", "/api/health"):
            try:
                await loop.run_in_executor(None, _http, path)
                check(f"GET {path}", True)
            except Exception as exc:  # /api/health 在旧版本里可能没有
                check(f"GET {path}", path == "/api/health", str(exc))
        await asyncio.sleep(3)
        rows = plugin.db.conn.execute("SELECT sender_id, content FROM memories ORDER BY id").fetchall()
        check("memories written", [r[1] for r in rows] == ["[用户要求记住] 周五一起打尖塔", "好呀周五见"], str(rows))
    finally:
        await plugin.terminate()
    print("[OK] terminated cleanly")


asyncio.run(run())
