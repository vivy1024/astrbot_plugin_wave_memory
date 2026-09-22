"""Person timeline helpers.

Formal history lives in ``person_timeline_events``. Unsettled energy lives in
``person_unsettled_state``. Leftover JSON fields are projected into tables once.
"""

from __future__ import annotations

import time
from collections.abc import Mapping, Sequence
from typing import Any


AFFINITY_STEP_CAP = 2.0
AFFINITY_HOSTILITY_STEP_CAP = 3.0
UNSETTLED_ENERGY_FULL = 10.0
IMPACT_CAP = 5.0

# 模块级动态上限配置（可由 main.py 从 Social_Settings 注入）
_DYNAMIC_LIMITS: dict[str, float] = {}


def configure_social_limits(
    *,
    step_cap: float | None = None,
    hostility_step_cap: float | None = None,
    impact_cap: float | None = None,
) -> None:
    """由外部配置更新社交步长与冲击度上限，非法/非正值回退默认。"""
    if step_cap is not None and step_cap > 0:
        _DYNAMIC_LIMITS["step_cap"] = float(step_cap)
    if hostility_step_cap is not None and hostility_step_cap > 0:
        _DYNAMIC_LIMITS["hostility_step_cap"] = float(hostility_step_cap)
    if impact_cap is not None and impact_cap > 0:
        _DYNAMIC_LIMITS["impact_cap"] = float(impact_cap)


def get_social_limits() -> dict[str, float]:
    return {
        "step_cap": _DYNAMIC_LIMITS.get("step_cap", AFFINITY_STEP_CAP),
        "hostility_step_cap": _DYNAMIC_LIMITS.get("hostility_step_cap", AFFINITY_HOSTILITY_STEP_CAP),
        "impact_cap": _DYNAMIC_LIMITS.get("impact_cap", IMPACT_CAP),
    }
_REAL_UNIX_TS = 1_000_000_000.0
ALLOWED_AFFINITY_DIMENSIONS = ("trust", "fun", "depth", "hostility", "familiarity")
DIMENSION_KEYS = ("familiarity", "trust", "fun", "depth", "hostility")
MEANINGFUL_EVENT_TYPES = frozenset({
    "deep_talk",
    "bot_praised",
    "bot_attacked",
    "confession",
    "joke",
    "gift_or_feed",
    "ignored_boundary",
    "direct_reply",
    "correction",
    "relationship.manual_calibration",
})
NOISY_EVENT_TYPES = frozenset({"message_seen"})
_TIMELINE_JSON_KEYS = (
    "impression",
    "impression_history",
    "impression_ledger",
    "impression_event",
    "impression_snapshot",
    "impression_updated_at",
    "impression_cleared_at",
    "impression_cleared_reason",
    "unsettled_traces",
    "unsettled_energy",
    "unsettled_interaction_count",
)


def _as_mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, Mapping) else {}


def _finite(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if number != number or number in {float("inf"), float("-inf")}:
        return None
    return number


def normalize_timeline_half_life(value: Any = None) -> float:
    """旧配置缺失或非法时保留 21 天默认值；bool 不是天数。"""
    number = None if isinstance(value, bool) else _finite(value)
    return number if number is not None and number > 0 else 21.0


def timeline_decay_weight(item: Mapping[str, Any], *, now: float, half_life_days: Any = None) -> float | None:
    """只计算当前印象的时间权重，不裁掉记录、不修改关系分数。"""
    timestamp = _item_timestamp(item)
    if timestamp <= 0 or _finite(now) is None:
        return None
    age_days = max(0.0, (now - timestamp) / 86400.0)
    return 2.0 ** (-age_days / normalize_timeline_half_life(half_life_days))


def snapshot_from_relationship(relationship: Mapping[str, Any] | None) -> dict[str, float]:
    payload = _as_mapping(relationship)
    values = _as_mapping(payload.get("values"))
    dimensions = _as_mapping(payload.get("dimensions"))
    snapshot: dict[str, float] = {}
    for key in DIMENSION_KEYS:
        item = _as_mapping(values.get(key))
        number = _finite(item.get("effective_value"))
        if number is None:
            number = _finite(dimensions.get(key))
        if number is not None:
            snapshot[key] = round(number, 1)
    affinity = _finite(payload.get("affinity"))
    if affinity is not None:
        snapshot["affinity"] = round(affinity, 1)
    return snapshot


def meaningful_event_anchor(history: Sequence[Any] | None) -> dict[str, Any] | None:
    for item in history or ():
        if not isinstance(item, Mapping):
            continue
        event_type = str(item.get("event_type") or "").strip()
        if event_type in NOISY_EVENT_TYPES:
            continue
        reason = str(item.get("reason") or "").strip().replace("\n", " ")
        if event_type not in MEANINGFUL_EVENT_TYPES and not reason:
            continue
        if event_type == "direct_reply" and (not reason or reason == "看见一条群友消息"):
            continue
        delta = _finite(item.get("delta"))
        anchor = {
            "event_type": event_type or "互动",
            "reason": reason[:80],
            "dimension": str(item.get("dimension") or "").strip(),
        }
        if delta is not None:
            anchor["delta"] = round(delta, 2)
        event_id = item.get("event_id") or item.get("id")
        if event_id not in {None, ""}:
            anchor["event_id"] = event_id
        return {key: value for key, value in anchor.items() if value not in {None, ""}}
    return None


def _context_fields(*, snapshot: Mapping[str, Any] | None, event: Mapping[str, Any] | None) -> dict[str, Any]:
    fields: dict[str, Any] = {}
    cleaned_snapshot = {
        str(key): round(number, 1)
        for key, value in _as_mapping(snapshot).items()
        if (number := _finite(value)) is not None
    }
    if cleaned_snapshot:
        fields["snapshot"] = cleaned_snapshot
    cleaned_event = {
        str(key): value
        for key, value in _as_mapping(event).items()
        if value not in {None, ""}
    }
    if cleaned_event:
        fields["event"] = cleaned_event
    return fields


def synthesize_milestone_phrase(
    *,
    event_type: str,
    reason: str,
    before_affinity: int,
    after_affinity: int,
    dimension: str,
    delta: float,
    is_leap: bool = False,
) -> str:
    """基于真实好感变动与事件因果，合成客观可信的交往里程碑短语。"""
    direction = "升至" if after_affinity > before_affinity else "降至" if after_affinity < before_affinity else "保持在"
    verb = "跃迁" if is_leap else ""
    diff_text = f"好感从 {before_affinity} {verb}{direction} {after_affinity}"
    normalized_reason = str(reason or "").strip().replace("\n", " ")[:60]
    prefix = "【关系跃迁】" if is_leap else ""
    if event_type == "bot_attacked":
        return f"{prefix}发生言语冲突（{normalized_reason}），{diff_text}"
    if event_type == "ignored_boundary":
        return f"{prefix}触犯互动边界（{normalized_reason}），{diff_text}"
    if event_type == "bot_praised":
        return f"{prefix}获得正面赞赏（{normalized_reason}），{diff_text}"
    if event_type == "deep_talk":
        return f"{prefix}进行深入长谈（{normalized_reason}），{diff_text}"
    if event_type == "joke":
        return f"{prefix}共同玩梗接梗（{normalized_reason}），{diff_text}"
    if event_type == "gift_or_feed":
        return f"{prefix}收到投喂或心意（{normalized_reason}），{diff_text}"
    if event_type == "correction":
        return f"{prefix}纠正事实误读（{normalized_reason}），{diff_text}"
    if event_type == "direct_reply":
        if "久别" in normalized_reason:
            return f"{prefix}隔别多日后重新对上话（{normalized_reason}），{diff_text}"
        if "首次" in normalized_reason:
            return f"{prefix}破冰初次直接交流（{normalized_reason}），{diff_text}"
        return f"{prefix}日常直接互动（{normalized_reason}），{diff_text}"
    return f"{prefix}关系发生变化（{normalized_reason}），{diff_text}"


def strip_timeline_json(metadata: Any) -> dict[str, Any]:
    payload = _as_mapping(metadata)
    for key in _TIMELINE_JSON_KEYS:
        payload.pop(key, None)
    return payload


def persist_timeline_event(
    db: Any,
    *,
    bot_id: str,
    user_id: str,
    group_id: str,
    kind: str,
    summary: str,
    detail: str = "",
    occurred_at: float | None = None,
    provenance: Mapping[str, Any] | None = None,
    connection=None,
) -> None:
    repo = getattr(db, "person_timeline", None)
    if repo is None:
        return
    repo.add_event(
        bot_id=bot_id,
        user_id=user_id,
        group_id=group_id,
        kind=kind,
        summary=summary,
        detail=detail or summary,
        occurred_at=occurred_at,
        provenance=provenance,
        connection=connection,
    )


def load_timeline_events(
    db: Any,
    *,
    bot_id: str,
    user_id: str | None = None,
    group_id: str | None = None,
    query: str = "",
    kind: str = "",
    limit: int | None = 50,
    offset: int = 0,
    connection=None,
    strict: bool = False,
) -> list[dict[str, Any]]:
    repo = getattr(db, "person_timeline", None)
    if repo is None or not hasattr(repo, "list_events"):
        if strict:
            raise RuntimeError("person_timeline_repository_unavailable")
        return []
    return repo.list_events(
        bot_id=bot_id,
        user_id=user_id,
        group_id=group_id,
        query=query,
        kind=kind,
        limit=limit,
        offset=offset,
        connection=connection,
        **({"strict": True} if strict else {}),
    )


def page_timeline_events(
    db: Any,
    *,
    bot_id: str,
    user_id: str | None = None,
    group_id: str | None = None,
    query: str = "",
    kind: str = "",
    limit: int = 50,
    offset: int = 0,
    connection=None,
) -> dict[str, Any]:
    repo = getattr(db, "person_timeline", None)
    if repo is None or not hasattr(repo, "page_events"):
        return {"items": [], "total": 0}
    return repo.page_events(
        bot_id=bot_id,
        user_id=user_id,
        group_id=group_id,
        query=query,
        kind=kind,
        limit=limit,
        offset=offset,
        connection=connection,
    )


def migrate_and_strip_profile_metadata(
    db: Any,
    *,
    bot_id: str,
    user_id: str,
    group_id: str,
    metadata: Any,
    connection=None,
) -> dict[str, Any]:
    payload = _as_mapping(metadata)
    if not any(key in payload for key in _TIMELINE_JSON_KEYS):
        return payload
    repo = getattr(db, "person_timeline", None)
    if repo is None or not hasattr(repo, "migrate_profile_metadata"):
        return payload
    return repo.migrate_profile_metadata(
        bot_id=bot_id,
        user_id=user_id,
        group_id=group_id,
        metadata=payload,
        connection=connection,
    )


def current_impression_text(events: Sequence[Any] | None, metadata: Any = None) -> str:
    for item in events or ():
        if not isinstance(item, Mapping):
            continue
        if str(item.get("kind") or "") in {"impression", "affinity"}:
            text = str(item.get("detail") or item.get("summary") or item.get("text") or "").strip()
            if text:
                return text
    return str(_as_mapping(metadata).get("impression") or "").strip()



def append_impression(
    metadata: Any,
    text: str,
    *,
    now: float | None = None,
    actor: str = "llm",
    snapshot: Mapping[str, Any] | None = None,
    event: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    payload = _as_mapping(metadata)
    current = str(text or "").strip()[:120]
    if not current:
        return payload
    previous = str(payload.get("impression") or "").strip()
    stamp = float(now if now is not None else time.time())
    history = payload.get("impression_history")
    if not isinstance(history, list):
        history = []
        if previous:
            history.append({"text": previous, "updated_at": payload.get("impression_updated_at"), "actor": "legacy"})
    context = _context_fields(snapshot=snapshot, event=event)
    if previous and previous != current:
        archived = {
            "text": previous,
            "updated_at": payload.get("impression_updated_at"),
            "superseded_at": stamp,
            "actor": actor,
        }
        archived.update(context)
        history.append(archived)
    payload["impression_history"] = history
    payload["impression"] = current
    payload["impression_updated_at"] = stamp
    if context.get("snapshot"):
        payload["impression_snapshot"] = context["snapshot"]
    if context.get("event"):
        payload["impression_event"] = context["event"]
    payload.pop("impression_cleared_at", None)
    payload.pop("impression_cleared_reason", None)
    return payload


def clear_impression(metadata: Any, *, reason: str, now: float | None = None, actor: str = "webui") -> dict[str, Any]:
    payload = _as_mapping(metadata)
    previous = str(payload.get("impression") or "").strip()
    if not previous:
        raise LookupError("impression_not_found")
    stamp = float(now if now is not None else time.time())
    history = payload.get("impression_history")
    if not isinstance(history, list):
        history = []
    history.append({
        "text": previous,
        "updated_at": payload.get("impression_updated_at"),
        "cleared_at": stamp,
        "cleared_reason": str(reason or "").strip(),
        "actor": actor,
        **_context_fields(
            snapshot=payload.get("impression_snapshot") if isinstance(payload.get("impression_snapshot"), Mapping) else None,
            event=payload.get("impression_event") if isinstance(payload.get("impression_event"), Mapping) else None,
        ),
    })
    payload["impression_history"] = history
    payload["impression"] = ""
    payload["impression_updated_at"] = stamp
    payload["impression_cleared_at"] = stamp
    payload["impression_cleared_reason"] = str(reason or "").strip()
    return payload


def _format_event(event: Mapping[str, Any] | None) -> str:
    payload = _as_mapping(event)
    event_type = str(payload.get("event_type") or "互动").strip()
    reason = str(payload.get("reason") or "").strip()
    dimension = str(payload.get("dimension") or "").strip()
    delta = _finite(payload.get("delta"))
    parts = [event_type]
    if reason:
        parts.append(reason[:80])
    if dimension and delta is not None:
        sign = "+" if delta > 0 else ""
        parts.append(f"{dimension}{sign}{delta:g}")
    return "：".join(parts[:2]) + (f"（{parts[2]}）" if len(parts) > 2 else "")





LEAP_DELTA_CAP = 5.0  # 跃迁时允许的阶跃上限


def affinity_shift_range(
    metadata: Any | None = None,
    *,
    dimension: str = "trust",
    energy: float | None = None,
    is_leap: bool = False,
) -> dict[str, Any]:
    """本轮允许的好感增量区间：平时小步积累，蓄满能量触发定向跃迁。"""
    dim = str(dimension or "trust").strip() or "trust"
    if dim not in ALLOWED_AFFINITY_DIMENSIONS:
        dim = "trust"
    limits = get_social_limits()
    base_step = limits["step_cap"]
    base_hostility = limits["hostility_step_cap"]
    cap = base_hostility if dim == "hostility" else base_step
    amount = energy if energy is not None else unsettled_energy(_as_mapping(metadata))
    leap_active = is_leap or (amount >= UNSETTLED_ENERGY_FULL)
    if leap_active:
        cap = LEAP_DELTA_CAP
    return {"min": round(-cap, 2), "max": round(cap, 2), "dimension": dim, "is_leap": leap_active}


def propose_affinity_shift(
    metadata: Any,
    *,
    dimension: str,
    requested_delta: Any,
    energy: float | None = None,
    is_leap: bool = False,
) -> dict[str, Any]:
    """校验模型提交的增量。越界不落盘。"""
    bounds = affinity_shift_range(metadata, dimension=dimension, energy=energy, is_leap=is_leap)
    dim = str(bounds["dimension"])
    amount = _finite(requested_delta)
    if amount is None:
        return {"ok": False, "error": "affinity_delta_invalid", **bounds, "requested": requested_delta}
    if amount < bounds["min"] or amount > bounds["max"]:
        return {
            "ok": False,
            "error": "affinity_delta_out_of_range",
            **bounds,
            "requested": round(amount, 2),
        }
    return {
        "ok": True,
        "dimension": dim,
        "delta": round(amount, 2),
        "min": bounds["min"],
        "max": bounds["max"],
        "is_leap": bounds.get("is_leap", False),
    }


def event_type_for_shift(dimension: str, delta: float) -> str:
    if dimension == "hostility" and delta > 0:
        return "ignored_boundary"
    if dimension == "depth" and delta > 0:
        return "deep_talk"
    if dimension == "fun" and delta > 0:
        return "joke"
    if delta < 0:
        return "direct_reply"
    return "direct_reply"


def _item_summary(item: Mapping[str, Any], *, fallback_text: str = "") -> str:
    # 只注入记录自己的 summary；没有独立摘要就不合成、不截取详情。
    return str(item.get("summary") or "").strip()


def _item_detail(item: Mapping[str, Any]) -> str:
    detail = str(item.get("detail") or "").strip()
    if detail:
        return detail
    return str(item.get("text") or "").strip()


def _item_timestamp(item: Mapping[str, Any]) -> float:
    # 若记录显式给出事件时间但无效，不用创建时间冒充事件发生时间。
    for key in ("occurred_at", "at", "updated_at", "superseded_at", "cleared_at", "created_at", "ts"):
        raw = item.get(key)
        if raw is None or raw == "":
            continue
        value = None if isinstance(raw, bool) else _finite(raw)
        if value is None or value <= 0:
            return 0.0
        try:
            time.localtime(value)
        except (OverflowError, OSError, ValueError):
            return 0.0
        return value
    return 0.0


def _when_label(item: Mapping[str, Any], *, now: float) -> str:
    ts = _item_timestamp(item)
    if ts <= 0:
        return "时间未知"
    age_days = max(0.0, (now - ts) / 86400.0)
    if ts > now:
        relative = "未来时间"
    elif age_days < 1:
        relative = "今天"
    elif age_days < 2:
        relative = "昨天"
    elif age_days < 7:
        relative = f"{int(age_days)}天前"
    elif age_days < 45:
        relative = f"{max(1, int(round(age_days / 7.0)))}周前"
    else:
        relative = f"{max(1, int(round(age_days / 30.0)))}个月前"
    if ts >= _REAL_UNIX_TS:
        return f"{time.strftime('%Y-%m-%d', time.localtime(ts))}（{relative}）"
    return relative


def impression_timeline_lines(
    metadata: Any,
    *,
    now: float | None = None,
    query: str = "",
    events: Sequence[Any] | None = None,
    half_life_days: Any = None,
) -> list[str]:
    """印象时间线全量注入：每条一行摘要（早→近），近事权重高，旧事只作背景。"""
    payload = _as_mapping(metadata)
    items = [item for item in (events or ()) if isinstance(item, Mapping)]
    if not items:
        stored = payload.get("impression_history")
        items = [item for item in stored if isinstance(item, Mapping)] if isinstance(stored, list) else []
        current_event = payload.get("impression_event") if isinstance(payload.get("impression_event"), Mapping) else {}
        current_text = str(payload.get("impression") or "").strip()
        if current_event or current_text:
            current = {
                "kind": "impression",
                "summary": current_text,
                "detail": current_text,
                "text": current_text,
                "event": current_event,
                "at": payload.get("impression_updated_at"),
            }
            if current["summary"] and all(_item_summary(item) != current["summary"] for item in items):
                items.append(current)
    if not items:
        return []
    stamp = _finite(now) if now is not None else time.time()
    if stamp is None:
        stamp = time.time()
    half_life = normalize_timeline_half_life(half_life_days)
    lines = [
        f"印象时间线（全量摘要，早→近；时间权重每 {half_life:g} 天减半）：",
        "按时间权重理解当前印象；旧事仅作历史背景，不代表近期事实，不据此改写真实关系分数。",
        "详情按需调用 wave_memory_person_search(query_type=timeline, person=当前人物, event_id=事件编号)；"
        "非当前群记录须显式 scope=all_groups。",
    ]
    header_count = len(lines)
    # query 仅为旧调用兼容保留；自动注入不筛选历史，也不拼接详情。
    for item in sorted(items, key=lambda row: (_item_timestamp(row), str(row.get("id") or ""))):
        summary = _item_summary(item)
        if not summary:
            continue
        weight = timeline_decay_weight(item, now=stamp, half_life_days=half_life)
        weight_text = f"{weight:.6g}" if weight is not None else "未知"
        if weight == 0.0:
            age_days = max(0.0, (stamp - _item_timestamp(item)) / 86400.0)
            weight_text = f"2^(-{age_days:.6g}/{half_life:.6g})"
        when = _when_label(item, now=stamp) or "时间未知"
        event_id = str(item.get("id") or "")
        identity = f"事件#{event_id} " if event_id else ""
        group_id = str(item.get("group_id") or "").strip()
        if group_id:
            identity += f"来源群={group_id} "
        # 每条一行，不删除摘要中的文字；详情保留在正式仓库供按需读取。
        summary = summary.replace("\r\n", " / ").replace("\n", " / ").replace("\r", " / ")
        lines.append(f"- {identity}{when} [时间权重={weight_text}] {summary}")
    return lines if len(lines) > header_count else []


def injection_lines(
    metadata: Any,
    *,
    history: Sequence[Any] | None = None,
    now: float | None = None,
    query: str = "",
    events: Sequence[Any] | None = None,
    half_life_days: Any = None,
    summary_only: bool = False,
) -> list[str]:
    payload = _as_mapping(metadata)
    lines: list[str] = []

    # 1. 默认包含规范的印象时间线摘要列表
    timeline = impression_timeline_lines(
        payload,
        now=now,
        query=query,
        events=events,
        half_life_days=half_life_days,
    )
    if timeline:
        lines.extend(timeline)

    # 2. 当前最新印象定性
    stamp = _finite(now) if now is not None else time.time()
    stamp = stamp if stamp is not None else time.time()
    current_item = next((
        item for item in sorted(
            (item for item in (events or ()) if isinstance(item, Mapping)),
            key=_item_timestamp, reverse=True,
        ) if str(item.get("kind") or "") in {"impression", "affinity"}
    ), {"summary": payload.get("impression"), "at": payload.get("impression_updated_at")})
    current = _item_summary(current_item)
    if current:
        weight = timeline_decay_weight(current_item, now=stamp, half_life_days=half_life_days)
        weight_text = f"{weight:.6g}" if weight is not None else "未知"
        lines.append(f"你对这个人的印象：{current}（最近存档，{_when_label(current_item, now=stamp)}；时间权重={weight_text}，非实时判断）")

    # 3. 最近关系线索
    event = payload.get("impression_event") if isinstance(payload.get("impression_event"), Mapping) else meaningful_event_anchor(history)
    formatted = _format_event(event)
    if formatted and formatted != "互动" and not timeline:
        lines.append(f"最近关系线索：{formatted}")

    # 4. 羽书自主 Tool Calling 查阅全貌引导提示：仅当存在实质印象/线索时附带
    if lines:
        lines.append("（如需查阅交往全貌或历史借还，可自主调用 wave_memory_person_search(query_type='timeline', person=...) 查看）")
    return lines


def parse_impression_mark(raw: str) -> tuple[str, float]:
    """Parse `观感 | impact: 3` or a bare impression sentence. Empty/none marks are dropped."""
    text = str(raw or "").strip().replace("\n", " ")
    if not text:
        return "", 0.0
    lowered = text.casefold()
    if lowered in {"无", "无新看法", "none", "n/a", "没有", "无变动"}:
        return "", 0.0
    impact = 1.0
    body = text
    for separator in ("|", "｜"):
        if separator in text:
            left, right = text.split(separator, 1)
            body = left.strip()
            digits = "".join(ch for ch in right if ch.isdigit() or ch in ".-")
            parsed = _finite(digits)
            if parsed is not None:
                impact = parsed
            break
    if len(body) < 4:
        return "", 0.0
    impact_limit = get_social_limits()["impact_cap"]
    return body[:120], max(0.0, min(impact_limit, impact))


def load_unsettled_state(
    db: Any,
    *,
    bot_id: str,
    user_id: str,
    group_id: str = "",
    connection=None,
) -> dict[str, Any]:
    repo = getattr(db, "person_timeline", None)
    if repo is None or not hasattr(repo, "get_unsettled_state"):
        return {"energy": 0.0, "interaction_count": 0, "traces": []}
    return repo.get_unsettled_state(bot_id=bot_id, user_id=user_id, group_id=group_id, connection=connection)


def persist_unsettled_trace(
    db: Any,
    *,
    bot_id: str,
    user_id: str,
    group_id: str,
    text: str,
    impact: float | int | None = 1.0,
    now: float | None = None,
    connection=None,
) -> dict[str, Any]:
    cleaned = str(text or "").strip().replace("\n", " ")[:120]
    amount = _finite(impact)
    if amount is None:
        amount = 1.0
    impact_limit = get_social_limits()["impact_cap"]
    amount = max(0.0, min(impact_limit, amount))
    if not cleaned or amount <= 0:
        return load_unsettled_state(db, bot_id=bot_id, user_id=user_id, group_id=group_id, connection=connection)
    repo = getattr(db, "person_timeline", None)
    if repo is None:
        return {"energy": amount, "interaction_count": 1, "traces": [{"text": cleaned, "impact": amount}]}
    return repo.add_unsettled_trace(
        bot_id=bot_id,
        user_id=user_id,
        group_id=group_id,
        text=cleaned,
        impact=amount,
        now=now,
        connection=connection,
    )


def parse_dimensional_energy(traces: Any) -> dict[str, float]:
    """从 traces 中聚合各维度的累积能量分账。"""
    res = {d: 0.0 for d in DIMENSION_KEYS}
    if not isinstance(traces, list):
        return res
    for item in traces:
        if isinstance(item, Mapping):
            dim = str(item.get("dim") or item.get("dimension") or "trust").strip().lower()
            if dim in res:
                impact = _finite(item.get("impact"))
                res[dim] += max(0.0, impact if impact is not None else 1.0)
    return {d: round(v, 2) for d, v in res.items()}


def record_dimensional_unsettled_energy(
    db: Any,
    *,
    bot_id: str,
    user_id: str,
    group_id: str = "",
    dimension: str = "trust",
    text: str = "",
    impact: float | int | None = 1.0,
    now: float | None = None,
    connection=None,
) -> dict[str, Any]:
    """跨群累加特定维度的未决能量，写入 traces 轨迹中。"""
    cleaned = str(text or "").strip().replace("\n", " ")[:120]
    amount = _finite(impact)
    if amount is None or amount <= 0:
        amount = 1.0
    impact_limit = get_social_limits()["impact_cap"]
    amount = max(0.0, min(impact_limit, amount))

    repo = getattr(db, "person_timeline", None)
    if repo is None:
        return {"energy": amount, "interaction_count": 1, "traces": []}

    # 跨群主体性：以 (bot_id, user_id, "") 作为跨群统一累积单元
    state = repo.get_unsettled_state(bot_id=bot_id, user_id=user_id, group_id="", connection=connection)
    stamp = float(now if now is not None else time.time())
    traces = list(state.get("traces") or [])
    traces.append({
        "text": cleaned,
        "summary": cleaned,
        "impact": amount,
        "dim": dimension,
        "group_id": group_id,
        "ts": stamp,
    })
    total_energy = round(float(state.get("energy") or 0.0) + amount, 2)
    count = int(state.get("interaction_count") or 0) + 1

    repo.set_unsettled_state(
        bot_id=bot_id,
        user_id=user_id,
        group_id="",
        energy=total_energy,
        interaction_count=count,
        traces=traces,
        connection=connection,
    )
    dim_energy = parse_dimensional_energy(traces)
    return {
        "energy": total_energy,
        "interaction_count": count,
        "traces": traces,
        "dimensional_energy": dim_energy,
    }


def consume_dimensional_energy(
    db: Any,
    *,
    bot_id: str,
    user_id: str,
    dimension: str,
    connection=None,
) -> None:
    """定向跃迁结算：仅清空已跃迁维度的未决能量轨迹，保留其余维度能量。"""
    repo = getattr(db, "person_timeline", None)
    if repo is None:
        return
    state = repo.get_unsettled_state(bot_id=bot_id, user_id=user_id, group_id="", connection=connection)
    traces = list(state.get("traces") or [])
    remaining_traces = [
        item for item in traces
        if isinstance(item, Mapping) and str(item.get("dim") or item.get("dimension") or "trust").strip().lower() != dimension
    ]
    new_total = sum(float(_finite(item.get("impact")) or 1.0) for item in remaining_traces if isinstance(item, Mapping))
    repo.set_unsettled_state(
        bot_id=bot_id,
        user_id=user_id,
        group_id="",
        energy=round(new_total, 2),
        interaction_count=len(remaining_traces),
        traces=remaining_traces,
        connection=connection,
    )


def clear_unsettled_state(
    db: Any,
    *,
    bot_id: str,
    user_id: str,
    group_id: str,
    connection=None,
) -> None:
    repo = getattr(db, "person_timeline", None)
    if repo is None:
        return
    repo.clear_unsettled_state(bot_id=bot_id, user_id=user_id, group_id=group_id, connection=connection)


def unsettled_energy(metadata: Any, state: Mapping[str, Any] | None = None) -> float:
    if isinstance(state, Mapping) and "energy" in state:
        return max(0.0, float(_finite(state.get("energy")) or 0.0))
    payload = _as_mapping(metadata)
    stored = _finite(payload.get("unsettled_energy"))
    if stored is not None:
        return max(0.0, stored)
    traces = payload.get("unsettled_traces")
    if not isinstance(traces, list):
        return 0.0
    total = 0.0
    for item in traces:
        if not isinstance(item, Mapping):
            continue
        total += float(_finite(item.get("impact")) or 1.0)
    return total


def unsettled_energy_line(
    metadata: Any = None,
    *,
    energy: float | None = None,
    traces: Sequence[Any] | None = None,
) -> str:
    payload = _as_mapping(metadata)
    amount = energy if energy is not None else unsettled_energy(payload)
    recent: list[str] = []
    source = traces if traces is not None else payload.get("unsettled_traces")
    if isinstance(source, list):
        for item in source[-3:]:
            if not isinstance(item, Mapping):
                continue
            text = str(item.get("text") or item.get("summary") or "").strip()
            if text:
                recent.append(text)
    bounds = affinity_shift_range(payload, energy=amount)
    line = (
        f"【感知】未结算能量 {amount:g}/{UNSETTLED_ENERGY_FULL:g}。"
        f"每轮在回复末尾写 <<impression:当下观感 | impact:1-5>>；无新看法不要写。"
        f"若你认为该做阶段性定性，调用 wave_memory_record_social_impression，"
        f"本轮增量须在 [{bounds['min']}, {bounds['max']}]，无变动填 0。"
    )
    if recent:
        line += " 最近观感：" + " / ".join(recent)
    return line




def should_trigger_affinity_transition(
    metadata: Any,
    dimensions: Mapping[str, Any] | None = None,
    *,
    energy: float | None = None,
) -> bool:
    """能量攒满，或敌意/信任极端时点亮结算提示。模型也可提前自决。"""
    payload = _as_mapping(metadata)
    amount = energy if energy is not None else unsettled_energy(payload)
    if amount >= UNSETTLED_ENERGY_FULL:
        return True
    dims = _as_mapping(dimensions)
    hostility = _finite(dims.get("hostility"))
    if hostility is not None and hostility >= 10.0:
        return True
    trust = _finite(dims.get("trust"))
    if trust is not None and trust <= -10.0:
        return True
    return False


_CUE_STOPWORDS = frozenset({
    "一个", "我们", "他们", "你们", "什么", "这个", "那个", "还是", "没有",
    "就是", "可以", "自己", "因为", "所以", "如果", "不是", "真的", "已经",
    "还没", "一下", "怎么", "为啥", "然后", "现在", "之前", "之后",
})
_CUE_NOISY_KINDS = frozenset({"message_seen", "unsettled"})


def _cue_tokens(text: str) -> set[str]:
    import re

    raw = str(text or "").casefold()
    tokens: set[str] = set()
    for part in re.findall(r"[A-Za-z0-9_]{2,24}", raw):
        if part not in _CUE_STOPWORDS:
            tokens.add(part)
    chars = re.findall(r"[\u4e00-\u9fff]", raw)
    for width in (2, 3, 4):
        for index in range(0, max(0, len(chars) - width + 1)):
            gram = "".join(chars[index:index + width])
            if gram not in _CUE_STOPWORDS:
                tokens.add(gram)
    return tokens


def _event_blob(item: Mapping[str, Any]) -> str:
    return " ".join(
        str(item.get(key) or "").strip()
        for key in ("summary", "detail", "text", "kind")
        if str(item.get(key) or "").strip()
    )


def match_timeline_cue(
    events: Sequence[Any] | None,
    message: str,
    *,
    min_hits: int = 2,
) -> dict[str, Any] | None:
    """本轮消息与该人时间线的词重叠。只返回最高分 1 条，不写库、不建关切。"""
    text = str(message or "").strip()
    if len(text) < 4:
        return None
    try:
        from .identity_safety import is_identity_contamination
    except ImportError:  # pragma: no cover
        from services.identity_safety import is_identity_contamination
    if is_identity_contamination(text):
        return None
    message_tokens = _cue_tokens(text)
    if not message_tokens:
        return None
    best: dict[str, Any] | None = None
    best_score = 0
    for raw in events or ():
        if not isinstance(raw, Mapping):
            continue
        kind = str(raw.get("kind") or raw.get("event_type") or "").strip()
        if kind in _CUE_NOISY_KINDS or kind in NOISY_EVENT_TYPES:
            continue
        blob = _event_blob(raw)
        if not blob or is_identity_contamination(blob):
            continue
        hits = message_tokens & _cue_tokens(blob)
        strong = {hit for hit in hits if len(hit) >= 3}
        score = len(hits) + len(strong)
        if len(strong) < 1 and len(hits) < min_hits:
            continue
        if score > best_score:
            best_score = score
            summary = str(raw.get("summary") or raw.get("detail") or raw.get("text") or "").strip()[:80]
            best = {
                "summary": summary,
                "hits": sorted(hits),
                "score": score,
                "kind": kind,
                "event_id": raw.get("id") or raw.get("event_id"),
                "occurred_at": _item_timestamp(raw),
            }
    if not best or not best["summary"]:
        return None
    return best


def timeline_cue_prompt(hit: Mapping[str, Any] | None) -> str:
    """最多两行。明确不是必须回复的指令。"""
    if not isinstance(hit, Mapping):
        return ""
    summary = str(hit.get("summary") or "").strip()
    if not summary:
        return ""
    when = _when_label(hit, now=time.time())
    prefix = f"{when}，" if when else "此前"
    return (
        f"【印象线索】{prefix}与他有过：{summary}。\n"
        "本轮提到了相关内容。若自然相关可接话；不是必须回复的指令。旧事不要当成刚发生。"
    )


def load_timeline_cue(
    db: Any,
    *,
    bot_id: str,
    user_id: str,
    group_id: str,
    message: str,
    limit: int = 20,
) -> dict[str, Any] | None:
    """规则门热路径：读该人最近时间线，匹配本轮消息。失败则视为未命中。"""
    bot_id = str(bot_id or "").strip()
    user_id = str(user_id or "").strip()
    group_id = str(group_id or "").strip()
    if not bot_id or not user_id or not group_id:
        return None
    try:
        events = load_timeline_events(
            db, bot_id=bot_id, user_id=user_id, group_id=group_id, limit=limit
        )
    except Exception:
        return None
    return match_timeline_cue(events, message)
