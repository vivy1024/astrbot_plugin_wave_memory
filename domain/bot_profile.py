"""Bot Profile v2：一个 Bot 的全部身份、绑定、人设与行为设置。

v5 之前 Bot 只能来自 AstrBot 静态配置里的 ``MetaThinking_Bot1/2`` 两个槽位，
人设细节（自称词、书设、日记署名、经历来源）写死在各个服务里。v6 起 Bot 存进
WaveMemory 自己的 ``bot_profiles`` 表，本模块只定义纯数据结构与序列化，不依赖
任何其他层。

兼容约定：
- ``db_id`` 是稳定主键，历史数据全部按它归属，永不改名。
- ``qq_id`` 仍是 AstrBot 主账号，旧代码按它查注册表；其他平台身份放在 ``bindings``。
- ``session_prefix`` 为空时沿用事件里的平台实例 id（v5 行为）。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

from .character_experience import CharacterExperienceSettings
from .mood_policy import MoodSettings

PROFILE_SCHEMA_VERSION = 2

# 绑定的宿主类型。astrbot=QQ 等 AstrBot 平台账号；cortico=Cortico 部署；bilibili=直播间。
BINDING_HOSTS = ("astrbot", "cortico", "bilibili")

# 不能作为 db_id 的保留值：会和库内哨兵、默认值混淆。
RESERVED_DB_IDS = frozenset({"bot", "default", "system", "all", "none"})

# 允许按 Bot 覆盖模型的后台任务：只有以 Bot 自己口吻/视角产出内容的任务。
# 任务 id 与 services/llm_routing.py 的 TASKS 一致（那边有测试核对）。
BOT_MODEL_TASKS = ("meta_thinking", "bookkeeping", "diary", "portrait", "belief_reflection", "character_experience", "game_lesson")


class BotProfileError(ValueError):
    """Profile 数据不合法；``code`` 供 API 返回稳定错误码。"""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _text_list(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple, set, frozenset)) else str(value).split(",")
    out: list[str] = []
    for item in items:
        text = _text(item)
        if text and text not in out:
            out.append(text)
    return out


def _bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on", "是", "开启"}:
            return True
        if normalized in {"0", "false", "no", "off", "否", "关闭"}:
            return False
    return bool(value)


def _int(value: Any, default: int, minimum: int | None = None) -> int:
    try:
        parsed = int(float(value))
    except (TypeError, ValueError):
        return default
    if minimum is not None and parsed < minimum:
        return default
    return parsed


def validate_db_id(db_id: Any) -> str:
    text = _text(db_id)
    if not text:
        raise BotProfileError("db_id_required", "db_id 不能为空")
    if text.isdecimal():
        raise BotProfileError("invalid_db_id", "db_id 不能是纯数字（容易和 QQ 号混淆）")
    if text.casefold() in RESERVED_DB_IDS:
        raise BotProfileError("invalid_db_id", f"db_id 不能使用保留值 {text!r}")
    if len(text) > 64 or any(ch in text for ch in " :/\\\t\n"):
        raise BotProfileError("invalid_db_id", "db_id 只能用字母、数字、下划线、短横线，最长 64")
    return text


@dataclass
class BotBinding:
    """Bot 在某个宿主上的一个身份。"""

    host: str
    self_id: str = ""        # astrbot：平台账号（QQ 号）
    platform: str = ""       # astrbot：平台实例 id（可选，用于会话前缀映射和群名预取）
    deployment: str = ""     # cortico：部署名，如 yushu-live
    room: str = ""           # bilibili：直播间号

    def key(self) -> tuple[str, str]:
        if self.host == "astrbot":
            return ("astrbot", self.self_id)
        if self.host == "cortico":
            return ("cortico", self.deployment)
        return ("bilibili", self.room)

    @classmethod
    def from_dict(cls, data: Any) -> "BotBinding":
        if not isinstance(data, dict):
            raise BotProfileError("invalid_binding", "binding 必须是对象")
        host = _text(data.get("host")).lower()
        if host not in BINDING_HOSTS:
            raise BotProfileError("invalid_binding", f"未知宿主 {host!r}，可选 {', '.join(BINDING_HOSTS)}")
        binding = cls(
            host=host,
            self_id=_text(data.get("self_id")),
            platform=_text(data.get("platform")),
            deployment=_text(data.get("deployment")),
            room=_text(data.get("room")),
        )
        if not binding.key()[1]:
            required = {"astrbot": "self_id", "cortico": "deployment", "bilibili": "room"}[host]
            raise BotProfileError("invalid_binding", f"{host} 绑定缺少 {required}")
        return binding


PREFERENCE_CONTEXTS = ("all", "group", "private")


def _preference_strength(value: Any) -> int:
    parsed = _int(value, 3, minimum=1)
    return max(1, min(5, parsed))


def _preference_context(value: Any) -> str:
    text = _text(value).lower()
    return text if text in PREFERENCE_CONTEXTS else "all"


@dataclass
class PreferenceItem:
    """一条稳定的喜恶：她喜欢或不喜欢什么，以及什么场合适用。"""

    text: str = ""
    tags: list[str] = field(default_factory=list)
    strength: int = 3
    context: str = "all"

    @classmethod
    def from_dict(cls, data: Any) -> "PreferenceItem":
        if isinstance(data, str):
            data = {"text": data}
        data = data if isinstance(data, dict) else {}
        return cls(
            text=_text(data.get("text") or data.get("topic") or data.get("name")),
            tags=_text_list(data.get("tags")),
            strength=_preference_strength(data.get("strength")),
            context=_preference_context(data.get("context")),
        )


@dataclass
class ReactionExample:
    """一个情景里她通常会怎么反应；用于让模型抓住「她的脾气」，不是台词模板。"""

    situation: str = ""
    reaction: str = ""
    tags: list[str] = field(default_factory=list)
    strength: int = 3
    context: str = "all"

    @classmethod
    def from_dict(cls, data: Any) -> "ReactionExample":
        data = data if isinstance(data, dict) else {}
        return cls(
            situation=_text(data.get("situation")),
            reaction=_text(data.get("reaction")),
            tags=_text_list(data.get("tags")),
            strength=_preference_strength(data.get("strength")),
            context=_preference_context(data.get("context")),
        )


def _preference_items(value: Any) -> list[PreferenceItem]:
    items = value if isinstance(value, (list, tuple)) else [value] if value else []
    out: list[PreferenceItem] = []
    seen: set[str] = set()
    for raw in items:
        item = PreferenceItem.from_dict(raw)
        if not item.text or item.text in seen:
            continue
        seen.add(item.text)
        out.append(item)
    return out


@dataclass
class PreferenceSettings:
    """Bot 自己的喜恶预设；跨群、跨平台稳定。群里的说话风格另走风格样例。"""

    likes: list[PreferenceItem] = field(default_factory=list)
    dislikes: list[PreferenceItem] = field(default_factory=list)
    reaction_examples: list[ReactionExample] = field(default_factory=list)

    @classmethod
    def from_dict(cls, data: Any) -> "PreferenceSettings":
        data = data if isinstance(data, dict) else {}
        examples: list[ReactionExample] = []
        seen: set[tuple[str, str]] = set()
        for raw in data.get("reaction_examples") or ():
            item = ReactionExample.from_dict(raw)
            key = (item.situation, item.reaction)
            if not item.situation or not item.reaction or key in seen:
                continue
            seen.add(key)
            examples.append(item)
        return cls(
            likes=_preference_items(data.get("likes")),
            dislikes=_preference_items(data.get("dislikes")),
            reaction_examples=examples,
        )

    def is_empty(self) -> bool:
        return not (self.likes or self.dislikes or self.reaction_examples)


@dataclass
class PersonaSettings:
    """人设里原本写死在代码中的部分。"""

    # 额外的自称词（名字、别名之外），用于身份安全检测和黑话过滤。
    self_terms: list[str] = field(default_factory=list)
    # 身份安全防护（防猫娘化、防认主）；关闭会放开防护，默认开启。
    identity_guard_enabled: bool = True
    # 追加到身份安全提示里的角色专属规则，每行一条。
    identity_guard_rules: list[str] = field(default_factory=list)
    # 日记署名，空则用显示名。
    diary_signature: str = ""
    # 常驻书设（Runtime 注入的灵魂底色），每行一条。
    lore_lines: list[str] = field(default_factory=list)
    # 书设标题，例如「羽书出自《某书》」。
    lore_title: str = ""
    # （旧版）第一人称经历记忆在记忆表里的 source 值。新的「角色经历」是独立功能，
    # 见 Bot Profile 的 character_experience 段；这里只为旧配置保留。
    experience_source: str = ""
    # 自省输出里需要剥掉的前缀（如「角色名：」），名字本身会自动加入。
    reflection_prefixes: list[str] = field(default_factory=list)
    # 书设语料：空=用部署默认语料；none=这个 Bot 不用书设；其他=语料 id（须与部署加载的一致）。
    lore_corpus: str = ""
    # 喜恶预设：跨群稳定的喜欢/不喜欢/典型反应。None=还没有配置过（迁移可补默认值）；
    # 空对象=用户明确清空，迁移不再回填。
    preferences: PreferenceSettings | None = None

    @classmethod
    def from_dict(cls, data: Any) -> "PersonaSettings":
        data = data if isinstance(data, dict) else {}
        raw_preferences = data.get("preferences")
        return cls(
            self_terms=_text_list(data.get("self_terms")),
            identity_guard_enabled=_bool(data.get("identity_guard_enabled"), True),
            identity_guard_rules=_lines(data.get("identity_guard_rules")),
            diary_signature=_text(data.get("diary_signature")),
            lore_lines=_lines(data.get("lore_lines")),
            lore_title=_text(data.get("lore_title")),
            experience_source=_text(data.get("experience_source")),
            reflection_prefixes=_text_list(data.get("reflection_prefixes")),
            lore_corpus=_text(data.get("lore_corpus")),
            preferences=None if raw_preferences is None else PreferenceSettings.from_dict(raw_preferences),
        )


def _model_map(value: Any) -> dict[str, str]:
    """任务 → provider 的覆盖表；空值丢弃。未知任务名原样保留（新版本的任务），由路由层决定是否生效。"""
    if not isinstance(value, dict):
        return {}
    out: dict[str, str] = {}
    for key, raw in value.items():
        task, provider = _text(key), _text(raw)
        if task and provider:
            out[task] = provider
    return out


def _lines(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).splitlines()
    return [_text(item) for item in items if _text(item)]


@dataclass
class BotProfile:
    """配置驱动的 Bot 身份描述。

    前 10 个字段与 v5 的 ``BotProfile`` 同名同义，旧代码无需改动即可继续读取。
    """

    qq_id: str
    name: str
    db_id: str = ""
    aliases: list[str] = field(default_factory=list)
    meta_prompt: str = ""
    proactive_enabled: bool = True
    proactive_interval_seconds: int = 600
    proactive_max_per_hour: int = 3
    exclude_sources: list[str] = field(default_factory=list)
    interest_keywords: list[str] = field(default_factory=list)
    # ---- v6 ----
    enabled: bool = True
    # 这个 Bot 的默认模型：只作用于 BOT_MODEL_TASKS（内心判断、事后记账、每日日记、关系小传、信念反思、角色经历、游戏经验），
    # 优先级低于下面按任务的覆盖、高于全局「模型分工」。留空跟随全局。
    model: str = ""
    # 按任务的模型覆盖 {任务 id: provider id}，只有 BOT_MODEL_TASKS 里的任务生效。
    models: dict[str, str] = field(default_factory=dict)
    session_prefix: str = ""
    bindings: list[BotBinding] = field(default_factory=list)
    persona: PersonaSettings = field(default_factory=PersonaSettings)
    # 心情画像：基线、敏感度、恢复半衰期、群氛围权重、疲劳、说话倾向（见 domain/mood_policy.py）。
    mood: MoodSettings = field(default_factory=MoodSettings)
    # 角色经历：进群之前「过去的人生」的召回设置（是否启用、当前时间点、召回条数、注入上限等）；
    # 没有这一段 = 关闭（见 domain/character_experience.py）。
    character_experience: CharacterExperienceSettings = field(default_factory=CharacterExperienceSettings)
    # 注入通道覆盖：{通道名: {enabled/budget_tokens/...}}，叠加在全局通道配置之上。
    channels: dict[str, dict[str, Any]] = field(default_factory=dict)
    # 工具开关：allow 非空时只开放列出的工具；deny 里的工具总是关闭。
    tools_allow: list[str] = field(default_factory=list)
    tools_deny: list[str] = field(default_factory=list)
    version: int = 0
    origin: str = "config"   # config=从旧静态配置迁移；webui=在 9876 创建；import=导入

    @property
    def all_keywords(self) -> list[str]:
        """该 bot 的所有兴趣关键词（名字 + 别名 + 自定义词）。"""
        words = [self.name] + self.aliases + self.interest_keywords
        return [w for w in words if w]

    @property
    def identity_terms(self) -> list[str]:
        """名字、别名与额外自称词，去重保序。"""
        out: list[str] = []
        for word in [self.name, *self.aliases, *self.persona.self_terms]:
            text = _text(word)
            if text and text not in out:
                out.append(text)
        return out

    @property
    def experience_source(self) -> str:
        return self.persona.experience_source

    @property
    def self_ids(self) -> list[str]:
        """全部 AstrBot 平台账号（主账号在前）。"""
        out = [self.qq_id] if self.qq_id else []
        for binding in self.bindings:
            if binding.host == "astrbot" and binding.self_id and binding.self_id not in out:
                out.append(binding.self_id)
        return out

    @property
    def platform_ids(self) -> list[str]:
        """已知属于该 Bot 的 AstrBot 平台实例 id。"""
        out: list[str] = []
        for value in [self.session_prefix, *(b.platform for b in self.bindings if b.host == "astrbot"), self.name]:
            text = _text(value)
            if text and text not in out:
                out.append(text)
        return out

    def binding_for(self, host: str) -> list[BotBinding]:
        return [binding for binding in self.bindings if binding.host == host]

    def tool_enabled(self, tool_name: str) -> bool:
        if tool_name in self.tools_deny:
            return False
        return not self.tools_allow or tool_name in self.tools_allow

    def to_dict(self) -> dict[str, Any]:
        data = asdict(self)
        data["schema_version"] = PROFILE_SCHEMA_VERSION
        return data

    @classmethod
    def from_dict(cls, data: Any, *, require_qq: bool = False) -> "BotProfile":
        if not isinstance(data, dict):
            raise BotProfileError("invalid_profile", "profile 必须是对象")
        db_id = validate_db_id(data.get("db_id"))
        name = _text(data.get("name"))
        if not name:
            raise BotProfileError("name_required", "显示名不能为空")
        qq_id = _text(data.get("qq_id"))
        if require_qq and not qq_id:
            raise BotProfileError("qq_id_required", "qq_id 不能为空")
        if qq_id and not qq_id.isdecimal():
            raise BotProfileError("invalid_qq_id", "qq_id 必须是纯数字账号")
        bindings: list[BotBinding] = []
        seen: set[tuple[str, str]] = set()
        for raw in data.get("bindings") or ():
            binding = BotBinding.from_dict(raw)
            if binding.key() in seen or binding.key() == ("astrbot", qq_id):
                continue
            seen.add(binding.key())
            bindings.append(binding)
        channels = data.get("channels") if isinstance(data.get("channels"), dict) else {}
        return cls(
            qq_id=qq_id,
            name=name,
            db_id=db_id,
            aliases=_text_list(data.get("aliases")),
            meta_prompt=_text(data.get("meta_prompt")),
            proactive_enabled=_bool(data.get("proactive_enabled"), True),
            proactive_interval_seconds=_int(data.get("proactive_interval_seconds"), 600, minimum=30),
            proactive_max_per_hour=_int(data.get("proactive_max_per_hour"), 3, minimum=0),
            exclude_sources=_text_list(data.get("exclude_sources")),
            interest_keywords=_text_list(data.get("interest_keywords")),
            enabled=_bool(data.get("enabled"), True),
            model=_text(data.get("model")),
            models=_model_map(data.get("models")),
            session_prefix=_text(data.get("session_prefix")),
            bindings=bindings,
            persona=PersonaSettings.from_dict(data.get("persona")),
            mood=MoodSettings.from_dict(data.get("mood")),
            character_experience=CharacterExperienceSettings.from_dict(data.get("character_experience")),
            channels={str(k): dict(v) for k, v in channels.items() if isinstance(v, dict)},
            tools_allow=_text_list(data.get("tools_allow")),
            tools_deny=_text_list(data.get("tools_deny")),
            version=_int(data.get("version"), 0, minimum=0),
            origin=_text(data.get("origin")) or "config",
        )


def profile_from_legacy_config(cfg: Any) -> BotProfile:
    """解析 v5 的 ``MetaThinking_BotN`` 配置；缺 qq_id 或 db_id 时拒绝。"""
    cfg = cfg if isinstance(cfg, dict) else {}
    if not _text(cfg.get("qq_id")) or not _text(cfg.get("db_id")):
        raise BotProfileError("legacy_incomplete", "BotProfile requires explicit qq_id and stable db_id")
    data = dict(cfg)
    data["origin"] = "config"
    return BotProfile.from_dict(data, require_qq=True)


__all__ = [
    "BINDING_HOSTS",
    "BOT_MODEL_TASKS",
    "PROFILE_SCHEMA_VERSION",
    "BotBinding",
    "BotProfile",
    "BotProfileError",
    "LORE_CORPUS_DISABLED",
    "PREFERENCE_CONTEXTS",
    "PersonaSettings",
    "PreferenceItem",
    "PreferenceSettings",
    "ReactionExample",
    "bot_catalog_scope",
    "profile_from_legacy_config",
    "validate_db_id",
]


LORE_CORPUS_DISABLED = frozenset({"none", "off", "-"})


def bot_catalog_scope(profile: Any, default: Any) -> tuple[Any, str]:
    """按 Bot 的书设语料选择 CatalogScope，返回 (scope 或 None, 原因)。

    一个部署只加载一份书设（``book_lore.db`` + 向量索引），Bot 指定的语料与之不同就不读，
    避免把别的世界观当成自己的设定。
    """
    persona = getattr(profile, "persona", None)
    corpus = str(getattr(persona, "lore_corpus", "") or "").strip()
    if not corpus:
        return default, ""
    if corpus.lower() in LORE_CORPUS_DISABLED:
        return None, "bot_book_lore_disabled"
    if default is None:
        return None, "catalog_scope_required"
    if corpus != getattr(default, "corpus_id", None):
        return None, f"corpus_not_loaded:{corpus}"
    return default, ""

