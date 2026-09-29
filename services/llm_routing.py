"""模型分工：WaveMemory 各后台大模型任务用哪个 AstrBot provider，集中在这里解析。

改造前所有后台任务只绑在两处配置上：

- 标签一族：``tag_llm_provider_id`` + ``llm_fallback_provider_ids``
  （标签提取、黑话、风格样例、纠错自省、信念、标签审计）；
- MetaThinking 一族：``MetaThinking_Settings.default_model``
  （内心判断；事后记账与每日日记直接复用 MetaThinking 的客户端）。

这里给每个任务一个 id，:meth:`LLMRouter.resolve` 按下面的层次拼回退链。高层在前，
逐层去重拼接：前面的 provider 不可用（不存在、报错、返回空）时依次回退到后面的。

1. Bot 覆盖（``bot``）：``BotProfile.models[任务]``、``BotProfile.model``，
   只对以 Bot 自己口吻/视角产出的任务开放（:data:`domain.bot_profile.BOT_MODEL_TASKS`）；
2. 任务配置（``task``）：``Model_Routing_Settings.<任务>``，9876 保存后原地生效；
3. 所属默认（``default``）：该任务改造前的默认链，启动时读取，改了需重启；
   ``legacy`` 表示默认链来自旧兼容路径（如 MetaThinking 未运行时事后记账改用标签模型链）。

兼容硬约束：``Model_Routing_Settings`` 全部留空、Bot 也没有模型覆盖时，每个任务的回退链与
改造前逐字一致（见 tests/test_llm_routing.py）。向量模型（``embedding_provider_id``）不在本模块范围。
"""

from __future__ import annotations

import logging
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

try:
    from ..domain.bot_profile import BOT_MODEL_TASKS
except ImportError:  # 单测里插件根目录是顶层包
    from domain.bot_profile import BOT_MODEL_TASKS

from .llm_fallback import (
    LLMCallStats,
    LLMFallbackClient,
    build_provider_chain,
    parse_provider_ids,
    provider_ids_from_config,
)

logger = logging.getLogger("wavememory")

ROUTING_SECTION = "Model_Routing_Settings"
ROUTING_SERVICE = "llm_routing"
ROUTING_TITLE = "模型分工"

SOURCE_LABELS = {
    "bot": "Bot 覆盖",
    "task": "任务配置",
    "default": "所属默认",
    "legacy": "旧配置",
    "none": "未配置",
}


@dataclass(frozen=True)
class TaskSpec:
    id: str
    title: str
    family: str  # "tag" | "meta"
    description: str
    log_prefix: str
    consumers: tuple[str, ...] = ()
    bot_overridable: bool = False
    # 旧实现只用 tag_llm_provider_id、不走 llm_fallback_provider_ids（标签审计）
    primary_only_default: bool = False
    # 已预留任务 id、调用方还没实现
    reserved: bool = False


TASKS: tuple[TaskSpec, ...] = (
    TaskSpec(
        "tag", "标签提取", "tag",
        "从新记忆里提炼结构化标签与事实线索（TagExtractor）。",
        "[TagExtract]",
        consumers=(
            "TagWorker 后台匀速打标签",
            "维护任务：标签回填、记忆重打标签",
            "导入记忆时打标签",
            "导入字段映射校验（只用首选模型，不回退）",
        ),
    ),
    TaskSpec(
        "jargon", "黑话挖掘", "tag",
        "校验黑话候选、推断黑话含义。",
        "[Jargon]",
        consumers=("黑话候选校验", "黑话含义推断与复核"),
    ),
    TaskSpec(
        "fewshot", "风格样例", "tag",
        "从聊天里提取说话风格样例、检查风格漂移（读取已审核样例不需要模型）。",
        "[FewShot]",
        consumers=("风格样例提取", "风格漂移检查"),
    ),
    TaskSpec(
        "self_reflect", "纠错自省", "tag",
        "检测到 Bot 被纠正后，对照记忆与书设写自省记录。",
        "[SelfReflect]",
        consumers=("被纠正后的自省",),
    ),
    TaskSpec(
        "belief", "信念引擎", "tag",
        "信念引擎持有这个客户端，但当前版本没有发起模型调用（信念改由对话中的模型现场提审）；配置它不会产生调用。",
        "[BeliefEngine]",
        consumers=("BeliefEngine（当前无调用）",),
    ),
    TaskSpec(
        "tag_audit", "标签审计", "tag",
        "标签页手动触发的标签质量审计（维护任务）。旧实现只用标签模型本身、不走回退链，留空时保持不变。",
        "[TagAudit]",
        consumers=("9876 标签页「审计」维护任务",),
        primary_only_default=True,
    ),
    TaskSpec(
        "meta_thinking", "内心判断", "meta",
        "MetaThinking 的回复判断与主动插话。当前入站流程只用它的兴趣词匹配，没有发起模型调用。",
        "[MetaThinking]",
        consumers=("MetaThinking 回复判断 / 主动插话",),
        bot_overridable=True,
    ),
    TaskSpec(
        "bookkeeping", "事后记账", "meta",
        "回复后回看对话，结算印象与好感、关切、事实；表达学习、回复反馈、心情影响搭在同一次调用里。",
        "[Bookkeeping]",
        consumers=(
            "事后记账（印象/好感结算、关切、事实）",
            "表达学习（同一次调用）",
            "回复反馈（同一次调用）",
            "心情影响（同一次调用）",
        ),
        bot_overridable=True,
    ),
    TaskSpec(
        "diary", "每日日记", "meta",
        "每天按群以 Bot 口吻写一篇日记。",
        "[Diary]",
        consumers=("每日日记",),
        bot_overridable=True,
    ),
    TaskSpec(
        "portrait", "关系小传", "meta",
        "为每个群友写关系摘要（预留，功能在另一分支实现）。",
        "[Portrait]",
        consumers=("关系小传（预留）",),
        bot_overridable=True,
        reserved=True,
    ),
)

TASK_BY_ID: dict[str, TaskSpec] = {spec.id: spec for spec in TASKS}
TASK_IDS: tuple[str, ...] = tuple(TASK_BY_ID)
BOT_OVERRIDABLE_TASKS = frozenset(spec.id for spec in TASKS if spec.bot_overridable)

FAMILIES: dict[str, dict[str, Any]] = {
    "tag": {
        "title": "标签模型一族",
        "keys": ("tag_llm_provider_id", "llm_fallback_provider_ids"),
        "description": "tag_llm_provider_id 在前、llm_fallback_provider_ids 依次回退；启动时读取，改了需重启。",
    },
    "meta": {
        "title": "MetaThinking 一族",
        "keys": ("MetaThinking_Settings.default_model",),
        "description": "MetaThinking_Settings.default_model；启动时读取，改了需重启。MetaThinking 未运行时，事后记账等沿用旧逻辑改用标签模型链。",
    },
}

# 表达学习、回复反馈、心情影响没有单独的模型调用，展示时说明它们搭在哪个任务上。
PIGGYBACK_TASKS: tuple[dict[str, str], ...] = (
    {"id": "expression_learning", "title": "表达学习", "rides_on": "bookkeeping"},
    {"id": "reply_feedback", "title": "回复反馈", "rides_on": "bookkeeping"},
    {"id": "mood_impact", "title": "心情影响", "rides_on": "bookkeeping"},
)


def legacy_meta_chain(meta_cfg: Mapping[str, Any] | None, global_fallback_ids: Any = None) -> list[str]:
    """改造前 MetaThinking 的回退链（逐字保留）。

    ``default_model`` 在前；回退优先用 ``MetaThinking_Settings.provider_1..9``，
    其次 ``provider_fallback_ids``，都没有时用顶层 ``meta_thinking_fallback_ids``。
    """
    meta_cfg = meta_cfg or {}
    default_model = meta_cfg.get("default_model", "")
    meta_fallback_ids = (
        provider_ids_from_config(meta_cfg, prefix="provider_")
        or parse_provider_ids(meta_cfg.get("provider_fallback_ids", ""))
    )
    return build_provider_chain(default_model, meta_fallback_ids or parse_provider_ids(global_fallback_ids))


def _dedupe(values: Iterable[str]) -> tuple[str, ...]:
    out: list[str] = []
    for value in values:
        if value and value not in out:
            out.append(value)
    return tuple(out)


def _parse_task_value(raw: Any) -> tuple[list[str], str | None]:
    """任务配置值 → provider 链。留空/None 表示跟随默认；类型不对按留空处理并报告。"""
    if raw is None:
        return [], None
    if isinstance(raw, str):
        return parse_provider_ids(raw), None
    if isinstance(raw, (list, tuple)):
        ids = [item.strip() for item in raw if isinstance(item, str) and item.strip()]
        bad = [item for item in raw if not isinstance(item, str)]
        return ids, (f"列表里有非字符串项 {bad!r}，已忽略" if bad else None)
    return [], f"应为 provider ID 字符串，收到 {type(raw).__name__}；按留空处理"


def _text(value: Any) -> str:
    return value.strip() if isinstance(value, str) else ("" if value is None else str(value).strip())


@dataclass(frozen=True)
class RouteLayer:
    source: str
    providers: tuple[str, ...]
    key: str
    label: str

    def to_dict(self) -> dict[str, Any]:
        return {"source": self.source, "providers": list(self.providers), "key": self.key, "label": self.label}


@dataclass(frozen=True)
class RouteResolution:
    task: str
    bot_id: str | None
    chain: tuple[str, ...]
    source: str
    source_key: str
    layers: tuple[RouteLayer, ...] = field(default_factory=tuple)

    @property
    def primary(self) -> str:
        return self.chain[0] if self.chain else ""

    @property
    def source_label(self) -> str:
        return SOURCE_LABELS.get(self.source, self.source)

    def to_dict(self) -> dict[str, Any]:
        return {
            "task": self.task,
            "bot_id": self.bot_id,
            "chain": list(self.chain),
            "primary": self.primary,
            "source": self.source,
            "source_label": self.source_label,
            "source_key": self.source_key,
            "layers": [layer.to_dict() for layer in self.layers],
        }


class LLMRouter:
    """各任务的 provider 回退链、客户端与调用统计。

    ``config`` 是插件配置：旧配置键（``tag_llm_provider_id`` 等，schema 标记需重启）只在构造时
    读一次；``Model_Routing_Settings`` 由 :meth:`reload` 重新读取（9876 保存后调用）。
    ``profile_lookup(db_id)`` 返回当前 Bot Profile，每次解析时现读，Bot 管理页保存后立即生效。
    """

    def __init__(
        self,
        config: Mapping[str, Any] | None = None,
        *,
        context: Any = None,
        profile_lookup: Callable[[str], Any] | None = None,
    ) -> None:
        cfg = config if isinstance(config, Mapping) else {}
        self.context = context
        self._profile_lookup = profile_lookup
        self._lock = threading.RLock()
        # 旧配置：与改造前 app/bootstrap.py、MetaThinking 的读取方式保持一致（不 strip，门控按原值真假）。
        self._tag_primary = cfg.get("tag_llm_provider_id", "")
        self._tag_fallback = cfg.get("llm_fallback_provider_ids", "")
        meta_cfg = cfg.get("MetaThinking_Settings", {}) or {}
        self._meta_cfg: Mapping[str, Any] = meta_cfg if isinstance(meta_cfg, Mapping) else {}
        self._meta_global_fallback = cfg.get("meta_thinking_fallback_ids", "")
        # 事后记账/日记/关系小传：MetaThinking 在运行时跟它一族，否则沿用旧逻辑改用标签模型链。
        self._meta_active = True
        self._task_values: dict[str, tuple[str, ...]] = {}
        self._invalid: dict[str, str] = {}
        self._stats: dict[str, LLMCallStats] = {spec.id: LLMCallStats(spec.id) for spec in TASKS}
        self._active: dict[str, tuple[bool, str]] = {}
        self._clients: dict[str, LLMFallbackClient] = {}
        self.reloaded_at: float | None = None
        self.reload(cfg)

    # ------------------------------------------------------------------ 配置

    @staticmethod
    def config_paths() -> tuple[str, ...]:
        """保存后可以原地生效的配置路径（``分组.`` 表示整个分组）。"""
        return (f"{ROUTING_SECTION}.",)

    @classmethod
    def affected_by(cls, path: str) -> bool:
        return any(path == key or (key.endswith(".") and path.startswith(key)) for key in cls.config_paths())

    def reload(self, config: Mapping[str, Any] | None) -> dict[str, Any]:
        """重新读取 ``Model_Routing_Settings``；返回变化的任务与无效项。"""
        cfg = config if isinstance(config, Mapping) else {}
        section = cfg.get(ROUTING_SECTION)
        values: dict[str, tuple[str, ...]] = {}
        invalid: dict[str, str] = {}
        if isinstance(section, Mapping):
            for spec in TASKS:
                ids, error = _parse_task_value(section.get(spec.id))
                if ids:
                    values[spec.id] = _dedupe(ids)
                if error:
                    invalid[spec.id] = error
        elif section not in (None, ""):
            invalid["*"] = f"{ROUTING_SECTION} 应为对象，收到 {type(section).__name__}；全部任务按留空处理"
        with self._lock:
            previous = self._task_values
            self._task_values = values
            self._invalid = invalid
            self.reloaded_at = time.time()
        changed = sorted(key for key in set(previous) | set(values) if previous.get(key) != values.get(key))
        if changed:
            logger.info("[WaveMemory] 模型分工已更新: %s", {key: list(values.get(key, ())) for key in changed})
        for task, error in invalid.items():
            logger.warning("[WaveMemory] 模型分工 %s.%s 无效: %s", ROUTING_SECTION, task, error)
        return {"changed": changed, "invalid": dict(invalid)}

    def set_meta_thinking_active(self, active: bool) -> None:
        self._meta_active = bool(active)

    @property
    def meta_thinking_active(self) -> bool:
        return self._meta_active

    def set_active(self, task: str, active: bool, note: str = "") -> None:
        """登记调用方是否在运行（启动时没创建的服务，改模型后需重启才会创建）。"""
        self._spec(task)
        self._active[task] = (bool(active), str(note or ""))

    def configured(self, task: str) -> tuple[str, ...]:
        self._spec(task)
        return self._task_values.get(task, ())

    # ------------------------------------------------------------------ 解析

    @staticmethod
    def _spec(task: str) -> TaskSpec:
        spec = TASK_BY_ID.get(task)
        if spec is None:
            raise ValueError(f"未知的模型分工任务 {task!r}，可选：{', '.join(TASK_IDS)}")
        return spec

    def _tag_layer(self, *, primary_only: bool = False) -> RouteLayer:
        primary = self._tag_primary if isinstance(self._tag_primary, str) else _text(self._tag_primary)
        if primary_only:
            first = primary.strip()
            return RouteLayer(
                "default", (first,) if first else (), "tag_llm_provider_id",
                "标签模型（只用首选，不走回退链）",
            )
        chain = build_provider_chain(primary, self._tag_fallback)
        return RouteLayer("default", tuple(chain), "tag_llm_provider_id", "标签模型 + 回退链")

    def _meta_layer(self) -> RouteLayer:
        chain = tuple(legacy_meta_chain(self._meta_cfg, self._meta_global_fallback))
        if _text(self._meta_cfg.get("default_model", "")) or not chain:
            return RouteLayer("default", chain, "MetaThinking_Settings.default_model", "MetaThinking 默认模型")
        if provider_ids_from_config(dict(self._meta_cfg), prefix="provider_"):
            key = "MetaThinking_Settings.provider_1..9"
        elif parse_provider_ids(self._meta_cfg.get("provider_fallback_ids", "")):
            key = "MetaThinking_Settings.provider_fallback_ids"
        else:
            key = "meta_thinking_fallback_ids"
        return RouteLayer("legacy", chain, key, "MetaThinking 旧回退配置（default_model 为空）")

    def default_layer(self, task: str) -> RouteLayer:
        spec = self._spec(task)
        if spec.family == "tag":
            return self._tag_layer(primary_only=spec.primary_only_default)
        if spec.id == "meta_thinking" or self._meta_active:
            return self._meta_layer()
        tag = self._tag_layer()
        return RouteLayer("legacy", tag.providers, tag.key, "MetaThinking 未运行：沿用旧逻辑改用标签模型链")

    def family_chain(self, family: str) -> list[str]:
        """某一族的默认链（旧配置）；``tag`` 即改造前的 ``WaveMemoryPlugin._llm_chain()``。"""
        if family == "tag":
            return list(self._tag_layer().providers)
        if family == "meta":
            return list(self._meta_layer().providers)
        raise ValueError(f"未知的模型族 {family!r}")

    def _profile(self, bot_id: str) -> Any:
        if not bot_id or self._profile_lookup is None:
            return None
        try:
            return self._profile_lookup(bot_id)
        except Exception as exc:
            logger.debug("[WaveMemory] 模型分工读取 Bot %s 失败: %s", bot_id, exc)
            return None

    def _bot_layers(self, spec: TaskSpec, bot_id: str | None) -> list[RouteLayer]:
        if not bot_id or not spec.bot_overridable or spec.id not in BOT_MODEL_TASKS:
            return []
        profile = self._profile(bot_id)
        if profile is None:
            return []
        db_id = _text(getattr(profile, "db_id", "")) or str(bot_id)
        name = _text(getattr(profile, "name", "")) or db_id
        layers: list[RouteLayer] = []
        models = getattr(profile, "models", None)
        if isinstance(models, Mapping):
            ids, _error = _parse_task_value(models.get(spec.id))
            if ids:
                layers.append(RouteLayer("bot", _dedupe(ids), f"bot:{db_id}.models.{spec.id}", f"{name} 的「{spec.title}」模型"))
        ids, _error = _parse_task_value(getattr(profile, "model", ""))
        if ids:
            layers.append(RouteLayer("bot", _dedupe(ids), f"bot:{db_id}.model", f"{name} 的默认模型"))
        return layers

    def resolve(self, task: str, bot_id: str | None = None) -> RouteResolution:
        spec = self._spec(task)
        layers = self._bot_layers(spec, bot_id)
        configured = self._task_values.get(spec.id, ())
        if configured:
            layers.append(RouteLayer("task", configured, f"{ROUTING_SECTION}.{spec.id}", "任务配置"))
        layers.append(self.default_layer(spec.id))
        chain = _dedupe(provider for layer in layers for provider in layer.providers)
        top = next((layer for layer in layers if layer.providers), None)
        return RouteResolution(
            task=spec.id,
            bot_id=bot_id or None,
            chain=chain,
            source=top.source if top is not None else "none",
            source_key=top.key if top is not None else layers[-1].key,
            layers=tuple(layers),
        )

    def chain(self, task: str, bot_id: str | None = None) -> list[str]:
        return list(self.resolve(task, bot_id).chain)

    def resolver(self, task: str) -> Callable[[str | None], list[str]]:
        """``chain_resolver``：每次调用按当前配置与 Bot Profile 解析。"""
        self._spec(task)
        return lambda bot_id=None: self.chain(task, bot_id)

    def available(self, task: str) -> bool:
        """启动时是否创建这个任务的调用方（与改造前的门控条件一致）。

        - 标签一族：改造前要求 ``tag_llm_provider_id`` 非空；现在任务配置非空也算。
        - 内心判断：不看模型（MetaThinking 按自身开关创建）。
        - 事后记账/日记/关系小传：MetaThinking 在运行（沿用它的客户端，即使链为空）或链非空。
        """
        spec = self._spec(task)
        if spec.family == "tag":
            return bool(self._task_values.get(spec.id)) or bool(self._tag_primary)
        if spec.id == "meta_thinking":
            return True
        return self._meta_active or bool(self.chain(spec.id))

    # ------------------------------------------------------------------ 客户端

    def stats(self, task: str) -> LLMCallStats:
        self._spec(task)
        return self._stats[task]

    def client(self, task: str) -> LLMFallbackClient:
        """任务的路由客户端（同一任务共用一个；按 Bot 覆盖用 ``client(...).for_bot(db_id)``）。"""
        spec = self._spec(task)
        with self._lock:
            existing = self._clients.get(spec.id)
            if existing is None:
                existing = LLMFallbackClient(
                    self.context,
                    self.chain(spec.id),
                    log_prefix=spec.log_prefix,
                    chain_resolver=self.resolver(spec.id),
                    stats=self._stats[spec.id],
                )
                self._clients[spec.id] = existing
            return existing

    # ------------------------------------------------------------------ 展示

    def describe(
        self,
        *,
        bots: Iterable[Any] = (),
        known_providers: Iterable[str] | None = None,
    ) -> dict[str, Any]:
        """模型分工总览（9876 配置中心「模型分工」分组用）。

        ``known_providers``：AstrBot 当前可见的 provider id；给了就标出链上找不到的项
        （找不到的会在调用时被跳过、回退到后面的 provider）。
        """
        known = set(known_providers) if known_providers is not None else None
        profiles = [profile for profile in bots if _text(getattr(profile, "db_id", ""))]

        def mark(chain: Iterable[str]) -> list[dict[str, Any]]:
            return [{"id": provider, "known": (provider in known) if known is not None else None} for provider in chain]

        tasks: list[dict[str, Any]] = []
        for spec in TASKS:
            resolution = self.resolve(spec.id)
            default = self.default_layer(spec.id)
            active, note = self._active.get(spec.id, (None, ""))
            if spec.reserved and active is None:
                active, note = False, "预留：调用方尚未实现"
            bot_routes: list[dict[str, Any]] = []
            if spec.bot_overridable:
                for profile in profiles:
                    bot_route = self.resolve(spec.id, _text(profile.db_id))
                    if bot_route.source != "bot":
                        continue
                    bot_routes.append({
                        **bot_route.to_dict(),
                        "name": _text(getattr(profile, "name", "")) or _text(profile.db_id),
                        "chain_status": mark(bot_route.chain),
                    })
            tasks.append({
                "id": spec.id,
                "title": spec.title,
                "family": spec.family,
                "family_title": FAMILIES[spec.family]["title"],
                "description": spec.description,
                "consumers": list(spec.consumers),
                "bot_overridable": spec.bot_overridable,
                "reserved": spec.reserved,
                "config_key": f"{ROUTING_SECTION}.{spec.id}",
                "configured": list(self._task_values.get(spec.id, ())),
                "invalid": self._invalid.get(spec.id),
                **resolution.to_dict(),
                "chain_status": mark(resolution.chain),
                "missing": [provider for provider in resolution.chain if known is not None and provider not in known],
                "default_chain": list(default.providers),
                "default_key": default.key,
                "default_label": default.label,
                "available": self.available(spec.id),
                "active": active,
                "active_note": note,
                # 调用方在运行时，改任务配置保存即生效；启动时没创建的，需要重启才会按新模型创建。
                "apply_mode": "restart" if active is False and not spec.reserved else "hot",
                "stats": self._stats[spec.id].snapshot(),
                "bots": bot_routes,
            })
        families = {
            name: {
                "title": meta["title"],
                "keys": list(meta["keys"]),
                "description": meta["description"],
                "chain": self.family_chain(name),
                "chain_status": mark(self.family_chain(name)),
                "apply_mode": "restart",
            }
            for name, meta in FAMILIES.items()
        }
        return {
            "section": ROUTING_SECTION,
            "title": ROUTING_TITLE,
            "reloaded_at": self.reloaded_at,
            "meta_thinking_active": self._meta_active,
            "families": families,
            "tasks": tasks,
            "piggyback": [dict(item) for item in PIGGYBACK_TASKS],
            "invalid": dict(self._invalid),
            "providers_checked": known is not None,
        }


__all__ = [
    "BOT_OVERRIDABLE_TASKS",
    "FAMILIES",
    "LLMRouter",
    "PIGGYBACK_TASKS",
    "ROUTING_SECTION",
    "ROUTING_SERVICE",
    "ROUTING_TITLE",
    "RouteLayer",
    "RouteResolution",
    "SOURCE_LABELS",
    "TASKS",
    "TASK_BY_ID",
    "TASK_IDS",
    "TaskSpec",
    "legacy_meta_chain",
]
