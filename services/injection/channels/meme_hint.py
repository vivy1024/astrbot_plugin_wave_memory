"""用梗提示通道（可选，默认关闭）。

话题命中梗百科词条的 ``hint_topics`` 时，提示 1–2 个“这里可以用的梗”及其意思。
它只给选项，不下指令：措辞明确“可以用，不是必须用”，也不附任何例句或 ``神言.txt`` 语料。

开关（都沿用现有机制，不另起配置体系）：

- **总开关 / 按 Bot**：通道配置 ``channels.meme_hint.enabled``，默认 ``False``；
  可在 9876 通道配置的全局、Bot 层或会话层打开，也可写进 Bot 资料的通道覆盖
  ``BotProfile.channels = {"meme_hint": {"enabled": true}}``。
- **按场合**：``Jargon_Settings.meme_hint_group``（群聊，默认开）、
  ``meme_hint_private``（私聊，默认关）、``meme_hint_live``（Cortico 直播 / Runtime API
  ``/context/prepare`` 来源，默认关）。场合关闭时本轮记为 ``skipped`` 并写明原因。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from typing import Any

try:
    from ....domain.scope import validate_formal_command_scope
except ImportError:  # pragma: no cover - 顶层导入回退
    from domain.scope import validate_formal_command_scope
from ...identity_safety import is_identity_contamination
from ..channel_base import InjectionResult
from .safety import is_channel_allowed_in_mode

SCENES = ("group", "private", "live")
DEFAULT_SCENE_SWITCHES: dict[str, bool] = {"group": True, "private": False, "live": False}
SCENE_CONFIG_KEYS: dict[str, str] = {
    "group": "meme_hint_group",
    "private": "meme_hint_private",
    "live": "meme_hint_live",
}
SCENE_LABELS: dict[str, str] = {"group": "群聊", "private": "私聊", "live": "直播/Runtime"}
MAX_HINTS = 2
HINT_HEADER = (
    "[可以用的梗：以下梗和当前话题有关，可以用，不是必须用；"
    "语境不合适、对方在认真说事时就别用，也不要为了用梗硬凹]"
)


def _mapping(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _as_bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off"}:
            return False
        return default
    return bool(value)


def _as_int(value: Any, default: int) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def meme_hint_scene_switches(plugin_config: Any) -> dict[str, bool]:
    """从 ``Jargon_Settings`` 读场合开关；缺失或非法值回落到默认（群聊开、私聊与直播关）。"""
    getter = getattr(plugin_config, "get", None)
    raw = getter("Jargon_Settings") if callable(getter) else None
    jargon_cfg = _mapping(raw)
    return {
        scene: _as_bool(jargon_cfg.get(key), DEFAULT_SCENE_SWITCHES[scene])
        for scene, key in SCENE_CONFIG_KEYS.items()
    }


def resolve_scene(ctx: Any) -> str:
    """AstrBot 消息钩子来源按群聊/私聊区分；其余来源（cortico、runtime……）都算直播/Runtime。"""
    source = str(getattr(ctx, "source", "astrbot") or "astrbot").strip().lower()
    if source != "astrbot":
        return "live"
    scope = getattr(ctx, "scope", None)
    if str(getattr(scope, "visibility", "") or "") == "private":
        return "private"
    return "group"


def _channel_cfg(ctx: Any) -> Mapping[str, Any]:
    options = _mapping(getattr(ctx, "channel_options", {})).get("meme_hint")
    if isinstance(options, Mapping):
        return options
    config = _mapping(getattr(ctx, "config", {}))
    return _mapping(_mapping(config.get("channels", {})).get("meme_hint", {}))


def _render_line(entry: Any) -> str:
    return f"- 「{entry.word}」：{entry.meaning}"


class MemeHintChannel:
    """话题命中时提示 1–2 个可以用的梗。"""

    name = "meme_hint"

    def __init__(self, *, knowledge_base: Any = None, scene_switches: Mapping[str, Any] | None = None):
        self._knowledge_base = knowledge_base
        switches = dict(DEFAULT_SCENE_SWITCHES)
        for scene, value in _mapping(scene_switches).items():
            if scene in switches:
                switches[scene] = _as_bool(value, switches[scene])
        self.scene_switches = switches

    @property
    def knowledge_base(self) -> Any:
        if self._knowledge_base is None:
            from ...jargon.meme_encyclopedia import get_meme_knowledge_base

            self._knowledge_base = get_meme_knowledge_base()
        return self._knowledge_base

    async def build(self, ctx: Any) -> InjectionResult:
        started = time.perf_counter()
        mode = str(getattr(ctx, "mode", "full") or "full")
        if not is_channel_allowed_in_mode(self.name, mode):
            return InjectionResult.disabled(self.name, reason=f"meme_hint channel disabled in {mode} mode")
        cfg = _channel_cfg(ctx)
        # 默认关闭：没有显式打开就不提示。
        if not _as_bool(cfg.get("enabled"), False):
            return InjectionResult.disabled(self.name, reason="meme_hint disabled by default")

        scene = resolve_scene(ctx)
        if not self.scene_switches.get(scene, False):
            key = SCENE_CONFIG_KEYS[scene]
            return InjectionResult.skipped(
                self.name,
                reason=f"meme_hint_scene_disabled:{scene}（{SCENE_LABELS[scene]}场合未开启，见 Jargon_Settings.{key}）",
            )

        runtime_scope = getattr(ctx, "scope", None)
        scope_decision = validate_formal_command_scope("meme_hint.inject", runtime_scope)
        if not scope_decision.allowed:
            return InjectionResult.empty(self.name, reason=scope_decision.reason_code or "scope_rejected")

        max_items = min(_as_int(cfg.get("max_items"), MAX_HINTS), MAX_HINTS)
        if max_items <= 0:
            return InjectionResult.empty(self.name, reason="meme_hint max_items is zero")

        try:
            message = str(getattr(ctx, "message", "") or "")
            entries = self.knowledge_base.hint_candidates(message, max_items=max_items)
            if not entries:
                return InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="no topic matched")
            lines = [_render_line(entry) for entry in entries]
            text = HINT_HEADER + "\n" + "\n".join(lines)
            if is_identity_contamination(text):
                result = InjectionResult.empty(self.name, latency_ms=self._latency_ms(started), reason="no safe meme hint")
                result.filtered = [{"filter_reason": "identity_contamination", "filter_channel": self.name}]
                return result
            items = [
                {
                    "word": entry.word,
                    "meaning": entry.meaning,
                    "source": entry.source,
                    "source_layer": "meme_encyclopedia",
                    "entry_id": entry.entry_id,
                    "scene": scene,
                    "reference_only": True,
                    "matched_by": "topic",
                    "rendered_text": line,
                    "dedupe_key": f"meme_hint:{entry.word}",
                    "preview": line,
                }
                for entry, line in zip(entries, lines)
            ]
            return InjectionResult.hit(self.name, text, items=items, latency_ms=self._latency_ms(started))
        except Exception as exc:  # pragma: no cover - 防御性错误通道
            result = InjectionResult.error_result(self.name, exc)
            result.latency_ms = self._latency_ms(started)
            return result

    @staticmethod
    def _latency_ms(started: float) -> float:
        return round((time.perf_counter() - started) * 1000, 1)


__all__ = [
    "DEFAULT_SCENE_SWITCHES",
    "HINT_HEADER",
    "MemeHintChannel",
    "SCENE_CONFIG_KEYS",
    "meme_hint_scene_switches",
    "resolve_scene",
]
