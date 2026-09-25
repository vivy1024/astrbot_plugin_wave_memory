"""注入通道注册表：通道按登记顺序实例化，新增通道不用改 ``main.py``。

内置通道见 :func:`builtin_channel_specs`（与 v5 在 ``main.py`` 里手写的顺序、参数一致）。
外部扩展在 ``register(tool_registry, channel_registry=...)`` 里调用
:meth:`ChannelRegistry.register` 登记自己的通道，并提供默认 ``ChannelConfig``；
外部通道不参与 9876 分层通道配置（那套配置只认内置通道），开关由注册表管理。
"""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any

from ..config.channel_config import KNOWN_CHANNELS, ChannelConfig, ChannelConfigSet

logger = logging.getLogger("astrbot")

ChannelFactory = Callable[[Any, dict[str, Any]], Any]


@dataclass(frozen=True)
class ChannelSpec:
    name: str
    factory: ChannelFactory
    # 外部通道必须给默认配置；内置通道的默认值在 channel_config.build_default_channel_config。
    default_config: ChannelConfig | None = None


class ChannelRegistryError(ValueError):
    pass


class ChannelRegistry:
    def __init__(self) -> None:
        self._specs: dict[str, ChannelSpec] = {}
        self._instances: dict[str, Any] = {}
        self._disabled_external: set[str] = set()
        self._build_errors: dict[str, str] = {}
        # 通道名 → 来源（builtin 或扩展文件名），由 ToolRegistry.load_extensions 在加载期间设置
        self._origins: dict[str, str] = {}
        self._loading_origin: str | None = None

    def register(self, spec: ChannelSpec) -> None:
        if spec.name in self._specs:
            raise ChannelRegistryError(f"channel {spec.name!r} already registered")
        if spec.name not in KNOWN_CHANNELS and spec.default_config is None:
            raise ChannelRegistryError(f"external channel {spec.name!r} needs a default_config")
        self._specs[spec.name] = spec
        self._origins[spec.name] = self._loading_origin or "builtin"

    def register_many(self, specs: list[ChannelSpec]) -> None:
        for spec in specs:
            self.register(spec)

    def build(self, deps: Any) -> list[Any]:
        """按登记顺序实例化；后面的工厂可以通过 ``built`` 拿到前面的实例（如 safety）。"""
        self._instances.clear()
        self._build_errors.clear()
        built: dict[str, Any] = {}
        for spec in self._specs.values():
            try:
                channel = spec.factory(deps, built)
            except Exception as exc:
                self._build_errors[spec.name] = f"{type(exc).__name__}: {exc}"
                logger.warning("[WaveMemory] 注入通道 %s 构造失败: %s", spec.name, exc)
                continue
            if channel is None:
                continue
            built[spec.name] = channel
        self._instances = built
        return list(built.values())

    def unload_extensions(self) -> list[str]:
        """卸载扩展登记的通道（实例与登记都去掉）；停用状态按名字保留，重载后仍停用。"""
        names = [name for name, origin in self._origins.items() if origin != "builtin"]
        for name in names:
            self._specs.pop(name, None)
            self._instances.pop(name, None)
            self._origins.pop(name, None)
            self._build_errors.pop(name, None)
        return names

    def build_extensions(self, deps: Any) -> list[str]:
        """只实例化扩展登记、尚未实例化的通道，接在已有通道后面。"""
        added: list[str] = []
        for name, spec in list(self._specs.items()):
            if self._origins.get(name, "builtin") == "builtin" or name in self._instances:
                continue
            try:
                channel = spec.factory(deps, dict(self._instances))
            except Exception as exc:
                self._build_errors[name] = f"{type(exc).__name__}: {exc}"
                logger.warning("[WaveMemory] 注入通道 %s 构造失败: %s", name, exc)
                continue
            if channel is not None:
                self._instances[name] = channel
                added.append(name)
        return added

    def instances(self) -> list[Any]:
        """全部已实例化的通道（含停用的外部通道，停用由配置层过滤），按登记顺序。"""
        return list(self._instances.values())

    def channels(self) -> list[Any]:
        return [ch for name, ch in self._instances.items() if name not in self._disabled_external]

    def external_names(self) -> list[str]:
        return [name for name in self._specs if name not in KNOWN_CHANNELS]

    def set_external_enabled(self, name: str, enabled: bool) -> None:
        if name not in self.external_names():
            raise ChannelRegistryError(f"{name!r} 不是外部通道（内置通道在「通道配置」页开关）")
        if enabled:
            self._disabled_external.discard(name)
        else:
            self._disabled_external.add(name)

    def with_external_defaults(self, config: ChannelConfigSet) -> ChannelConfigSet:
        """给本次注入配置补上外部通道的默认配置（编排器据此取超时、预算、优先级）。"""
        extras = {
            name: self._specs[name].default_config
            for name in self.external_names()
            if name not in config.channels and name not in self._disabled_external
        }
        if not extras:
            return config
        return replace(config, channels={**config.channels, **extras})

    def status(self) -> dict[str, Any]:
        return {
            "registered": list(self._specs),
            "built": list(self._instances),
            "external": self.external_names(),
            "disabled_external": sorted(self._disabled_external),
            "build_errors": dict(self._build_errors),
        }


def builtin_channel_specs() -> list[ChannelSpec]:
    """v5 ``_setup_injection_shadow_pipeline`` 里的 12 个通道，顺序与参数不变。"""
    from ..jargon.holyman_persona import HolymanPersonaPack
    from ..persona_composer import PersonaComposer
    from .channels.belief import BeliefChannel
    from .channels.book_lore import BookLoreChannel
    from .channels.facts import FactsChannel
    from .channels.fewshot import FewShotChannel
    from .channels.fts5 import FTS5Channel
    from .channels.holyman_persona import HolymanPersonaChannel
    from .channels.jargon import JargonChannel
    from .channels.memory_recall import MemoryRecallChannel
    from .channels.persona import PersonaChannel
    from .channels.relationship import RelationshipChannel
    from .channels.safety import SafetyChannel
    from .channels.soul_state import SoulStateChannel

    return [
        ChannelSpec("safety", lambda d, b: SafetyChannel()),
        ChannelSpec("memory", lambda d, b: MemoryRecallChannel(query_engine=d.query_engine, safety_channel=b.get("safety"))),
        ChannelSpec(
            "fts5",
            lambda d, b: FTS5Channel(
                db=d.db,
                cross_group_enabled=d.cross_group_enabled,
                shared_memory_grants_enabled=d.shared_memory_grants_enabled,
            ),
        ),
        ChannelSpec("facts", lambda d, b: FactsChannel(db=d.db, facts_decay_rate=getattr(d, "_facts_decay_rate", 0.005))),
        ChannelSpec(
            "persona",
            lambda d, b: PersonaChannel(
                composer=PersonaComposer(db=d.db, query_engine=d.query_engine, bot_profiles=d._bot_registry)
            ),
        ),
        ChannelSpec("belief", lambda d, b: BeliefChannel(belief_engine=getattr(d, "belief_engine", None))),
        ChannelSpec("jargon", lambda d, b: JargonChannel(jargon_service=getattr(d, "jargon_service", None))),
        # 可选风格人格包：默认关闭，需在通道配置里显式开启。
        ChannelSpec("holyman_persona", lambda d, b: HolymanPersonaChannel(persona_pack=HolymanPersonaPack())),
        ChannelSpec("fewshot", lambda d, b: FewShotChannel(few_shot_service=getattr(d, "few_shot_service", None))),
        ChannelSpec("affinity", lambda d, b: RelationshipChannel(repository=d.db.soul_repository, db=d.db)),
        ChannelSpec("soul_state", lambda d, b: SoulStateChannel(repository=d.db.soul_repository)),
        ChannelSpec(
            "book_lore",
            lambda d, b: BookLoreChannel(
                book_lore_index=d.book_lore_index,
                embedding_service=d.embedding_service,
                lore_db_path=d.lore_db_path,
                catalog_scope=d.book_lore_catalog_scope,
                profile_lookup=lambda bot_id: d.bot_registry.get(bot_id),
            ),
        ),
    ]


__all__ = ["ChannelRegistry", "ChannelRegistryError", "ChannelSpec", "builtin_channel_specs"]
