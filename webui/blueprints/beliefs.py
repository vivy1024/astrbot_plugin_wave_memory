"""Beliefs Blueprint — 信念 v2：Bot 自己从经历里形成的看法（按状态分组查看、否决 / 恢复）。

- ``GET /api/beliefs/?bot_id=<db_id>``：按状态分组（萌芽 / 已形成 / 稳固 / 动摇中 / 已淡出 / 已否决 /
  已归档），每条带依据日记与完整历史；旧版 scoped 信念只读列在「已归档（旧版人物判断）」里。
- ``POST /api/beliefs/<id>/veto`` / ``restore``：带上读到的 ``revision``，版本对不上返回 409。
  写入全部委托 ``BeliefLifecycleService``，本文件不写 SQL。
- 形成的看法默认自动生效，不再有逐条审核；旧的 scoped 审核/归档接口与旧 beliefs 表的批量改动一律 410。
"""

from __future__ import annotations

import json
import time
from functools import wraps
from typing import Any

from quart import Blueprint, jsonify, request

from ..api_contract import error_payload, mutation_response
from ..container import get_container
from ..middleware.auth import require_auth

try:
    from ...engine.db.bot_belief_repo import BeliefRevisionConflict
    from ...services.belief_engine import describe_experiences
    from ...services.belief_lifecycle import (
        CATEGORY_LABELS,
        EVENT_LABELS,
        LEGACY_ARCHIVED,
        STATUS_LABELS,
        STATUSES,
        VETOED,
        BeliefAdminError,
        BeliefLifecycleService,
        BeliefPolicy,
        compute_strength,
    )
except ImportError:  # pragma: no cover - plugin root may be imported directly
    from engine.db.bot_belief_repo import BeliefRevisionConflict
    from services.belief_engine import describe_experiences
    from services.belief_lifecycle import (
        CATEGORY_LABELS,
        EVENT_LABELS,
        LEGACY_ARCHIVED,
        STATUS_LABELS,
        STATUSES,
        VETOED,
        BeliefAdminError,
        BeliefLifecycleService,
        BeliefPolicy,
        compute_strength,
    )

beliefs_bp = Blueprint("beliefs", __name__, url_prefix="/api/beliefs")

GROUP_ORDER = (*STATUSES, LEGACY_ARCHIVED)
GROUP_DESCRIPTIONS = {
    "sprout": "只出现过一次，还不会在聊天里浮现",
    "formed": "在不同日子的经历里被印证过，话题相关时会浮现",
    "stable": "持续被印证，话题相关时会浮现",
    "shaken": "遇到反例或很久没被印证，暂不浮现",
    "faded": "被推翻、长期没被印证或超出上限，不再浮现",
    "vetoed": "管理员否决；之后不会再生成相似的看法",
    LEGACY_ARCHIVED: "旧版由事后记账从事实归纳的人物判断，只读保留，不注入",
}
_LEGACY_TYPE_LABELS = {
    "self_identity": "自我身份",
    "person_judgment": "人物判断",
    "world_view": "世界观",
    "preference": "偏好",
}
_EVIDENCE_LIMIT = 12


def _table_exists(conn, table: str) -> bool:
    """检查表是否存在。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# 需要排除的 archived_reason（身份污染 / 清理标记）
_EXCLUDED_REASONS = ("identity_roleplay_contamination", "identity_cleanup_full")
_LEGACY_BELIEF_TYPE_MAP = {
    "self": "self_identity",
    "other": "person_judgment",
    "world": "world_view",
    "value": "preference",
}
_NEW_TO_LEGACY_BELIEF_TYPE_MAP = {v: k for k, v in _LEGACY_BELIEF_TYPE_MAP.items()}


def _normalize_belief_type(value) -> str:
    raw = str(value or "").strip()
    if not raw:
        return ""
    return _LEGACY_BELIEF_TYPE_MAP.get(raw, raw)


def _append_belief_type_filter(where_parts: list[str], params: list, belief_type) -> None:
    normalized = _normalize_belief_type(belief_type)
    if not normalized:
        return
    legacy = _NEW_TO_LEGACY_BELIEF_TYPE_MAP.get(normalized)
    if legacy:
        where_parts.append("type IN (?, ?)")
        params.extend([normalized, legacy])
    else:
        where_parts.append("type = ?")
        params.append(normalized)


def _belief_level(strength) -> str:
    try:
        val = float(strength or 0)
    except (TypeError, ValueError):
        val = 0.0
    if val >= 0.75:
        return "核心"
    if val >= 0.55:
        return "稳定"
    if val >= 0.35:
        return "候选"
    return "微弱"


def _scope_error(code: str, status: int):
    return jsonify({"error": {"code": code}}), status


def _legacy_mutation_disabled(handler):
    """Keep unresolved legacy rows auditable but permanently non-mutable in formal APIs."""
    @wraps(handler)
    async def reject(*args, **kwargs):
        return _scope_error("legacy_mutation_disabled", 410)
    return reject


# ─────────────────────────── 信念 v2 ───────────────────────────

def _error(code: str, message: str, status: int):
    return jsonify(error_payload(code, message)), status


def _repo(container):
    db = getattr(container, "db", None)
    return getattr(db, "bot_beliefs", None) if db is not None else None


def _policy(container) -> BeliefPolicy:
    policy = getattr(container, "belief_policy", None)
    return policy if isinstance(policy, BeliefPolicy) else BeliefPolicy()


def _bot_id(value: Any) -> str:
    return str(value or "").strip()


def _history_item(event: dict) -> dict:
    return {
        "id": event.get("id"),
        "event_type": event.get("event_type"),
        "label": EVENT_LABELS.get(str(event.get("event_type") or ""), str(event.get("event_type") or "")),
        "from_status": event.get("from_status") or None,
        "to_status": event.get("to_status") or None,
        "from_label": STATUS_LABELS.get(str(event.get("from_status") or "")),
        "to_label": STATUS_LABELS.get(str(event.get("to_status") or "")),
        "reason": event.get("reason") or "",
        "actor": event.get("actor") or "",
        "occurred_at": event.get("occurred_at"),
        "evidence": list(event.get("evidence") or []),
    }


def _evidence_from_history(history: list[dict]) -> list[dict]:
    """依据日记（最新在前，按日记去重）；标出是印证还是反例。"""
    seen: set[Any] = set()
    out: list[dict] = []
    for event in reversed(history):
        polarity = "challenge" if event.get("event_type") == "challenged" else "support"
        for ref in event.get("evidence") or []:
            key = (ref.get("diary_id"), polarity)
            if key in seen:
                continue
            seen.add(key)
            out.append({**ref, "polarity": polarity})
            if len(out) >= _EVIDENCE_LIMIT:
                return out
    return out


def _serialize(belief: dict, history: list[dict], *, policy: BeliefPolicy, now: float) -> dict:
    status = str(belief.get("status") or "")
    items = [_history_item(event) for event in history]
    dates = list(belief.get("support_dates") or [])
    return {
        "id": int(belief["id"]),
        "key": f"belief:{belief['id']}",
        "legacy": False,
        "bot_id": belief.get("bot_id"),
        "content": belief.get("content"),
        "category": belief.get("category"),
        "category_label": CATEGORY_LABELS.get(str(belief.get("category") or ""), "关于世界"),
        "status": status,
        "status_label": STATUS_LABELS.get(status, status),
        "strength": compute_strength(belief, now=now, policy=policy),
        "support_days": len(dates),
        "support_dates": dates,
        "confirm_runs": int(belief.get("confirm_runs") or 0),
        "challenge_count": int(belief.get("challenge_count") or 0),
        "triggers": list(belief.get("triggers") or []),
        "experience_phrase": describe_experiences(dates, now=now) if dates else None,
        "first_seen_at": belief.get("first_seen_at"),
        "last_confirmed_at": belief.get("last_confirmed_at"),
        "status_changed_at": belief.get("status_changed_at"),
        "updated_at": belief.get("updated_at"),
        "revision": int(belief.get("revision") or 1),
        "evidence": _evidence_from_history(items),
        "history": items,
        "actions": {
            "veto": {"available": status != VETOED, "reason_code": None if status != VETOED else "belief_already_vetoed"},
            "restore": {"available": status == VETOED, "reason_code": None if status == VETOED else "belief_not_vetoed"},
        },
    }


def _serialize_legacy(row: dict) -> dict:
    provenance = row.get("provenance") if isinstance(row.get("provenance"), dict) else {}
    belief_type = _normalize_belief_type(row.get("belief_type")) or "world_view"
    return {
        "id": int(row["id"]),
        "key": f"legacy:{row['id']}",
        "legacy": True,
        "bot_id": row.get("bot_id"),
        "content": row.get("content"),
        "category": None,
        "category_label": None,
        "legacy_type": belief_type,
        "legacy_type_label": _LEGACY_TYPE_LABELS.get(belief_type, belief_type),
        "legacy_status": provenance.get("legacy_status") or None,
        "session_id": row.get("session_id"),
        "status": LEGACY_ARCHIVED,
        "status_label": STATUS_LABELS[LEGACY_ARCHIVED],
        "created_at": row.get("created_at"),
        "updated_at": row.get("updated_at"),
        "evidence": [],
        "history": [],
        "actions": {
            "veto": {"available": False, "reason_code": "legacy_readonly"},
            "restore": {"available": False, "reason_code": "legacy_readonly"},
        },
    }


def _legacy_rows(container, bot_id: str) -> list[dict]:
    repo = getattr(getattr(container, "db", None), "scoped_knowledge", None)
    lister = getattr(repo, "list_bot_scoped_beliefs", None)
    if not callable(lister):
        return []
    try:
        return [dict(row, bot_id=bot_id) for row in lister(bot_id, status=LEGACY_ARCHIVED, limit=500) or []]
    except Exception:
        return []


def _reflection_summary(container, repo, bot_id: str) -> dict:
    service = getattr(container, "belief_reflection", None)
    runs = []
    try:
        runs = repo.list_runs(bot_id, limit=5)
    except Exception:
        runs = []
    return {
        "enabled": bool(service is not None and getattr(service, "enabled", False)),
        "every_n_days": getattr(service, "every_n_days", None) if service is not None else None,
        "last_run": runs[0] if runs else None,
        "recent_runs": runs,
    }


def build_beliefs_payload(container, bot_id: str, *, now: float | None = None) -> dict:
    """按状态分组的只读视图；也供测试直接调用。"""
    repo = _repo(container)
    policy = _policy(container)
    stamp = float(now if now is not None else time.time())
    beliefs = repo.list_beliefs(bot_id, limit=2000)
    events = repo.list_events(bot_id, belief_ids=[b["id"] for b in beliefs], limit=10000) if beliefs else []
    by_belief: dict[int, list[dict]] = {}
    for event in events:
        by_belief.setdefault(int(event["belief_id"]), []).append(event)
    items_by_status: dict[str, list[dict]] = {status: [] for status in GROUP_ORDER}
    for belief in beliefs:
        item = _serialize(belief, by_belief.get(int(belief["id"]), []), policy=policy, now=stamp)
        items_by_status.setdefault(item["status"], []).append(item)
    for status in GROUP_ORDER:
        items_by_status[status].sort(key=lambda item: (-(item.get("strength") or 0.0), -(item.get("updated_at") or 0.0)))
    items_by_status[LEGACY_ARCHIVED] = [_serialize_legacy(row) for row in _legacy_rows(container, bot_id)]
    groups = [
        {
            "status": status,
            "label": STATUS_LABELS[status],
            "description": GROUP_DESCRIPTIONS[status],
            "count": len(items_by_status.get(status, [])),
            "items": items_by_status.get(status, []),
        }
        for status in GROUP_ORDER
    ]
    return {
        "bot_id": bot_id,
        "groups": groups,
        "counts": {group["status"]: group["count"] for group in groups},
        "policy": {
            "form_min_days": policy.form_min_days,
            "stable_min_days": policy.stable_min_days,
            "decay_days": policy.decay_days,
            "max_active": policy.max_active,
        },
        "reflection": _reflection_summary(container, repo, bot_id),
        "capabilities": {
            "veto": {"available": True, "reason_code": None},
            "restore": {"available": True, "reason_code": None},
            "create": {"available": False, "reason_code": "belief_formed_by_reflection_only"},
            "edit": {"available": False, "reason_code": "belief_edit_command_unavailable"},
            "physical_delete": {"available": False, "reason_code": "physical_delete_disabled"},
        },
        "generated_at": stamp,
    }


@beliefs_bp.route("/", methods=["GET"])
@require_auth
async def list_beliefs():
    """Bot 的看法，按状态分组；每条带依据日记与历史。"""
    bot_id = _bot_id(request.args.get("bot_id"))
    if not bot_id:
        return _error("bot_id_required", "bot_id 必填（BotProfile.db_id）", 400)
    container = get_container()
    if _repo(container) is None:
        return _error("belief_repository_unavailable", "信念数据还没准备好", 503)
    return jsonify(build_beliefs_payload(container, bot_id))


@beliefs_bp.route("/<int:belief_id>", methods=["GET"])
@require_auth
async def get_belief(belief_id: int):
    bot_id = _bot_id(request.args.get("bot_id"))
    if not bot_id:
        return _error("bot_id_required", "bot_id 必填（BotProfile.db_id）", 400)
    container = get_container()
    repo = _repo(container)
    if repo is None:
        return _error("belief_repository_unavailable", "信念数据还没准备好", 503)
    belief = repo.get_belief(bot_id, belief_id)
    if belief is None:
        return _error("belief_not_found", "找不到这条看法", 404)
    item = _serialize(belief, repo.list_events(bot_id, belief_ids=[belief_id]), policy=_policy(container), now=time.time())
    return jsonify({"item": item})


async def _transition(belief_id: int, action: str):
    body = await request.get_json(silent=True) or {}
    bot_id = _bot_id(body.get("bot_id"))
    if not bot_id:
        return _error("bot_id_required", "bot_id 必填（BotProfile.db_id）", 400)
    container = get_container()
    repo = _repo(container)
    if repo is None:
        return _error("belief_repository_unavailable", "信念数据还没准备好", 503)
    service = BeliefLifecycleService(repo, _policy(container))
    try:
        if action == "veto":
            belief = service.veto(bot_id, belief_id, revision=body.get("revision"),
                                  reason=str(body.get("reason") or "")[:200])
        else:
            belief = service.restore(bot_id, belief_id, revision=body.get("revision"))
    except BeliefRevisionConflict:
        return _error("belief_revision_conflict", "这条看法刚被改过，请刷新后再操作", 409)
    except BeliefAdminError as exc:
        status = {"belief_not_found": 404, "belief_revision_required": 400}.get(exc.code, 409)
        return _error(exc.code, "当前状态不允许这个操作", status)
    item = _serialize(belief, repo.list_events(bot_id, belief_ids=[belief_id]), policy=_policy(container), now=time.time())
    return jsonify(mutation_response(
        operation_kind=f"belief.{action}", status="succeeded", revision=item["revision"],
        item=item, include_item=True,
    ))


@beliefs_bp.route("/<int:belief_id>/veto", methods=["POST"])
@require_auth
async def veto_belief(belief_id: int):
    """一键否决：不再浮现，内容被记住，以后不再生成相似的看法。"""
    return await _transition(belief_id, "veto")


@beliefs_bp.route("/<int:belief_id>/restore", methods=["POST"])
@require_auth
async def restore_belief(belief_id: int):
    """撤销否决：回到否决前的状态。"""
    return await _transition(belief_id, "restore")


@beliefs_bp.route("/", methods=["POST"])
@require_auth
async def create_belief():
    """看法只能从 Bot 自己的经历里反思形成，WebUI 不能手工创建。"""
    return _scope_error("anchored_belief_command_unavailable", 503)


@beliefs_bp.route("/<int:belief_id>", methods=["PUT"])
@require_auth
async def edit_belief(belief_id: int):
    """自由编辑会让看法脱离经历依据，因此正式 API 禁用。"""
    return _scope_error("belief_edit_command_unavailable", 503)


@beliefs_bp.route("/<int:belief_id>", methods=["DELETE"])
@require_auth
async def delete_belief(belief_id: int):
    """看法只允许否决，不允许物理删除（历史与否决记忆都要保留）。"""
    return _scope_error("physical_delete_disabled", 410)


# ─────────────────────────── 旧数据：只读 / 已停用 ───────────────────────────

@beliefs_bp.route("/<int:belief_id>/approve", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def approve_belief(belief_id: int):
    """旧 scoped 信念的人工审核已停用：信念 v2 形成即生效，旧数据只读归档。"""
    return _scope_error("legacy_mutation_disabled", 410)


@beliefs_bp.route("/<int:belief_id>/archive", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def archive_belief(belief_id: int):
    """旧 scoped 信念已统一归档为只读。"""
    return _scope_error("legacy_mutation_disabled", 410)


@beliefs_bp.route("/commands/batch-lifecycle", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def batch_transition_scoped_beliefs():
    """旧 scoped 信念批量审核/归档已停用。"""
    return _scope_error("legacy_mutation_disabled", 410)


@beliefs_bp.route("/legacy/audit", methods=["GET"])
@require_auth
async def legacy_list_beliefs():
    """旧 beliefs 表的只读审查视图；不属于正式 Scope API。"""
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"items": [], "total": 0, "pending_count": 0})

    status = request.args.get("status")  # pending / active / archived / pending_legacy
    bot_id = request.args.get("bot_id")
    belief_type = request.args.get("type") # self_identity / person_judgment / world_view / preference
    search_q = request.args.get("search") # 搜索内容

    try:
        limit = int(request.args.get("limit") or request.args.get("size") or 50)
    except (ValueError, TypeError):
        limit = 50
    limit = max(1, min(limit, 500))
    try:
        if request.args.get("offset") is not None:
            offset = int(request.args.get("offset", 0))
        elif request.args.get("page") is not None:
            offset = (max(1, int(request.args.get("page", 1))) - 1) * limit
        else:
            offset = 0
    except (ValueError, TypeError):
        offset = 0

    where_parts = ["1=1"]
    params = []
    if status:
        where_parts.append("status = ?")
        params.append(status)
    if bot_id:
        where_parts.append("bot_id = ?")
        params.append(bot_id)
    if belief_type:
        _append_belief_type_filter(where_parts, params, belief_type)
    if search_q:
        where_parts.append("content LIKE ?")
        params.append(f"%{search_q.strip()}%")

    # 排除身份污染 / 清理标记的信念
    reason_excl = ",".join("?" for _ in _EXCLUDED_REASONS)
    where_parts.append(f"COALESCE(archived_reason, '') NOT IN ({reason_excl})")
    params.extend(_EXCLUDED_REASONS)

    where_sql = " AND ".join(where_parts)
    cols = {r[1] for r in c.db.conn.execute("PRAGMA table_info(beliefs)").fetchall()}
    evidence_type_expr = "evidence_type" if "evidence_type" in cols else "'memory' AS evidence_type"
    evidence_ids_expr = "evidence_ids" if "evidence_ids" in cols else "'[]' AS evidence_ids"
    sql = f"SELECT id, content, type, strength, bot_id, sources, status, created_at, last_reinforced, {evidence_type_expr}, {evidence_ids_expr} FROM beliefs WHERE {where_sql} ORDER BY created_at DESC LIMIT ? OFFSET ?"
    params.extend([limit, offset])

    rows = c.db.conn.execute(sql, params).fetchall()

    # total COUNT 加 WHERE 条件（和列表查询一致）
    count_sql = f"SELECT COUNT(*) FROM beliefs WHERE {where_sql}"
    total = c.db.conn.execute(count_sql, params[:-2]).fetchone()[0]
    pending_count = c.db.conn.execute(
        f"SELECT COUNT(*) FROM beliefs WHERE status = 'pending' AND COALESCE(archived_reason, '') NOT IN ({reason_excl})",
        list(_EXCLUDED_REASONS),
    ).fetchone()[0]

    items = []
    for r in rows:
        sources = json.loads(r[5] or "[]")
        evidence_ids = json.loads(r[10] or "[]")
        evidence_type = r[9] or "memory"
        items.append({
            "id": r[0], "content": r[1], "type": _normalize_belief_type(r[2]) or "world_view", "confidence": r[3],
            "bot_id": r[4], "source": evidence_type, "evidence_type": evidence_type,
            "sources": evidence_ids or sources, "raw_sources": sources, "status": r[6],
            "created_at": r[7], "updated_at": r[8], "level": _belief_level(r[3]),
            "legacy": True, "unresolved_legacy": True, "scope": None,
        })
    return jsonify({
        "items": items, "total": total, "pending_count": pending_count,
        "legacy": True, "unresolved_legacy": True, "scope": None, "readonly": True,
        "page": {"number": offset // limit + 1, "page_size": limit, "total": total,
                 "total_status": "exact", "has_next": offset + len(items) < total},
    })


@beliefs_bp.route("/legacy/<int:belief_id>/evidence", methods=["GET"])
@require_auth
async def legacy_belief_evidence(belief_id: int):
    """旧 belief 的只读证据审查路径。"""
    c = get_container()
    if not _table_exists(c.db.conn, "beliefs"):
        return jsonify({"ok": False, "error": "beliefs table not found"}), 500

    cols = {r[1] for r in c.db.conn.execute("PRAGMA table_info(beliefs)").fetchall()}
    evidence_type_expr = "evidence_type" if "evidence_type" in cols else "'memory' AS evidence_type"
    evidence_ids_expr = "evidence_ids" if "evidence_ids" in cols else "'[]' AS evidence_ids"
    row = c.db.conn.execute(
        f"SELECT sources, {evidence_type_expr}, {evidence_ids_expr} FROM beliefs WHERE id = ?", (belief_id,)
    ).fetchone()
    if not row:
        return jsonify({"ok": False, "error": "belief not found"}), 404

    raw_sources = json.loads(row[0] or "[]")
    evidence_type = row[1] or "memory"
    evidence_ids = json.loads(row[2] or "[]") or raw_sources
    episodes = []
    memories = []
    relationship_events = []

    def _load_memories(ids: list[int]):
        if not ids or not _table_exists(c.db.conn, "memories"):
            return []
        placeholders = ",".join("?" * len(ids))
        rows = c.db.conn.execute(
            f"SELECT id, content, sender_name, sender_id, timestamp, group_id FROM memories WHERE id IN ({placeholders}) ORDER BY timestamp ASC",
            ids,
        ).fetchall()
        return [
            {"id": r[0], "content": r[1], "sender_name": r[2] or "", "sender_id": r[3] or "", "timestamp": r[4], "group_id": r[5]}
            for r in rows
        ]

    if evidence_ids and evidence_type == "relationship_event" and _table_exists(c.db.conn, "relationship_events"):
        placeholders = ",".join("?" * len(evidence_ids))
        ev_rows = c.db.conn.execute(
            f"""SELECT id, bot_id, group_id, user_id, event_type, dimension, delta, reason,
                       source_episode_id, source_memory_id, created_at
                  FROM relationship_events WHERE id IN ({placeholders}) ORDER BY created_at ASC""",
            evidence_ids,
        ).fetchall()
        memory_ids = []
        episode_ids = []
        for r in ev_rows:
            relationship_events.append({
                "id": r[0], "bot_id": r[1], "group_id": r[2], "user_id": r[3],
                "event_type": r[4], "dimension": r[5], "delta": r[6], "reason": r[7],
                "source_episode_id": r[8], "source_memory_id": r[9], "created_at": r[10],
            })
            if r[8]:
                episode_ids.append(int(r[8]))
            if r[9]:
                memory_ids.append(int(r[9]))
        memories = _load_memories(list(dict.fromkeys(memory_ids)))
        if episode_ids:
            evidence_ids = list(dict.fromkeys(episode_ids))
            evidence_type = "episode"

    if evidence_ids and evidence_type in {"episode", "experience_episode"} and _table_exists(c.db.conn, "experience_episodes"):
        placeholders = ",".join("?" * len(evidence_ids))
        ep_rows = c.db.conn.execute(
            f"SELECT id, trigger_text, outcome, bot_inner_thought, bot_reply, source_memory_ids, created_at, episode_type FROM experience_episodes WHERE id IN ({placeholders}) ORDER BY created_at ASC",
            evidence_ids,
        ).fetchall()
        all_memory_ids = set()
        for r in ep_rows:
            try:
                mem_ids = json.loads(r[5] or "[]")
                if isinstance(mem_ids, list):
                    all_memory_ids.update(int(x) for x in mem_ids if x)
            except Exception:
                pass
            episodes.append({
                "id": r[0], "trigger": r[1] or "", "outcome": r[2] or "",
                "bot_inner_thought": r[3] or "", "bot_reply": r[4] or "",
                "created_at": r[6], "episode_type": r[7] or "",
            })
        memories = memories or _load_memories(list(all_memory_ids))

    if raw_sources and not memories and evidence_type == "memory":
        memories = _load_memories(raw_sources)

    return jsonify({
        "ok": True,
        "belief_id": belief_id,
        "sources": evidence_ids,
        "raw_sources": raw_sources,
        "evidence_type": evidence_type,
        "episodes": episodes,
        "relationship_events": relationship_events,
        "memories": memories,
        "items": memories,
        "legacy": True,
        "unresolved_legacy": True,
        "scope": None,
        "readonly": True,
    })


@beliefs_bp.route("/batch-archive", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def batch_archive():
    """旧 beliefs 批量归档没有 scoped lifecycle 命令，永久禁用。"""
    return _scope_error("legacy_mutation_disabled", 410)


@beliefs_bp.route("/batch-archive-selected", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def batch_archive_selected_beliefs():
    """旧 beliefs 勾选/跨页批量归档没有 scoped lifecycle 命令，永久禁用。"""
    return _scope_error("legacy_mutation_disabled", 410)

@beliefs_bp.route("/batch-approve", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def batch_approve_beliefs():
    """旧 beliefs 批量审核绕过 ObjectRef、证据门禁与 lifecycle，永久禁用。"""
    return _scope_error("legacy_mutation_disabled", 410)

@beliefs_bp.route("/batch-delete", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def batch_delete_beliefs():
    """旧 beliefs 物理删除永久禁用。"""
    return _scope_error("legacy_mutation_disabled", 410)
