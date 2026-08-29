"""Beliefs Blueprint — 信念管理 CRUD + approve/archive (US-1.2)"""

from __future__ import annotations

import json
from functools import wraps

from quart import Blueprint, current_app, jsonify, request

from ..api_contract import error_payload, mutation_response, page_response
from ..container import get_container
from ..middleware.auth import require_auth

try:
    from ...domain.scope import RuntimeScope, ScopeCodec, ScopeValidationError, SessionRef
    from ...engine.db.scoped_knowledge_repo import ScopedKnowledgeScopeError
    from ...services.belief_confidence import is_activation_eligible
    from ...services.belief_lifecycle import BeliefLifecycleService
except ImportError:  # pragma: no cover - plugin root may be imported directly
    from domain.scope import RuntimeScope, ScopeCodec, ScopeValidationError, SessionRef
    from engine.db.scoped_knowledge_repo import ScopedKnowledgeScopeError
    from services.belief_confidence import is_activation_eligible
    from services.belief_lifecycle import BeliefLifecycleService

beliefs_bp = Blueprint("beliefs", __name__, url_prefix="/api/beliefs")


def _table_exists(conn, table: str) -> bool:
    """检查表是否存在。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


# 需要排除的 archived_reason（身份污染 / 清理标记）
_EXCLUDED_REASONS = ("identity_roleplay_contamination", "identity_cleanup_full")
_VALID_BELIEF_TYPES = {"self_identity", "person_judgment", "world_view", "preference"}
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


def _scope_failure(exc: Exception):
    code = getattr(exc, "reason_code", None) or getattr(exc, "code", None) or "invalid_scope"
    return _scope_error(str(code), 400 if code in {"scope_required", "pagination_required", "object_ref_revision_required"} else 422)


def _legacy_mutation_disabled(handler):
    """Keep unresolved legacy rows auditable but permanently non-mutable in formal APIs."""
    @wraps(handler)
    async def reject(*args, **kwargs):
        return _scope_error("legacy_mutation_disabled", 410)
    return reject


def _scoped_repo(container):
    repo = getattr(getattr(container, "db", None), "scoped_knowledge", None)
    if repo is None:
        raise ScopedKnowledgeScopeError("scoped_repository_unavailable")
    return repo


def _group_scope_from_query() -> RuntimeScope:
    required = ("bot_id", "session_id", "visibility")
    if any(request.args.get(field) is None for field in required):
        raise ScopedKnowledgeScopeError("scope_required")
    bot_id, session_id, visibility = (request.args.get(field) for field in required)
    if visibility != "group":
        raise ScopedKnowledgeScopeError("derived_scope_visibility_unsupported")
    try:
        platform_id, kind, conversation_id = str(session_id).split(":", 2)
    except ValueError as exc:
        raise ScopeValidationError("invalid_session_id", "session_id must be canonical") from exc
    return RuntimeScope(str(bot_id), visibility, SessionRef(str(session_id), platform_id, kind, conversation_id))


def _scope_from_envelope(body: dict) -> RuntimeScope:
    if "scope" not in body:
        raise ScopedKnowledgeScopeError("scope_required")
    scope = ScopeCodec.from_dict(body["scope"])
    if not isinstance(scope, RuntimeScope) or scope.visibility != "group":
        raise ScopedKnowledgeScopeError("derived_scope_visibility_unsupported")
    return scope


def _pagination_from_query() -> tuple[int, int]:
    try:
        limit = int(request.args.get("limit", 25))
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError) as exc:
        raise ScopedKnowledgeScopeError("invalid_pagination") from exc
    if limit not in {25, 50, 100} or offset < 0:
        raise ScopedKnowledgeScopeError("invalid_pagination")
    return limit, offset


def _item_revision(item: dict) -> int:
    return max(1, int(float(item.get("updated_at") or item.get("created_at") or 1) * 1000))


def _object_ref_registry():
    try:
        return current_app.extensions.get("wave_api_contract", {}).get("object_refs")
    except RuntimeError:
        return None


def _scope_query(scope: RuntimeScope) -> dict:
    query = {"bot_id": scope.bot_id, "visibility": scope.visibility}
    if scope.session is not None:
        query["session_id"] = scope.session.id
    if scope.subject_principal_id:
        query["subject_principal_id"] = scope.subject_principal_id
    return query


def _item_object_ref(item: dict, scope: RuntimeScope) -> dict | None:
    registry = _object_ref_registry()
    if registry is None:
        return None
    revision = _item_revision(item)
    locator = int(item["id"])
    ref = registry.issue(kind="belief", locator=locator, scope=scope, revision=revision)
    return {
        "ref": ref,
        "kind": "belief",
        "locator": locator,
        "scope_key": scope.session.id if scope.session else scope.bot_id,
        "scope_query": _scope_query(scope),
        "version": revision,
    }


def _require_object_ref(body: dict, *, kind: str, locator: int, scope: RuntimeScope, item: dict) -> None:
    """变更必须携带列表签发的 opaque ref 与同一 revision。"""
    descriptor = body.get("object_ref") or body.get("ref")
    ref = descriptor.get("ref") if isinstance(descriptor, dict) else descriptor
    try:
        revision = int(body.get("revision"))
    except (TypeError, ValueError) as exc:
        raise ScopedKnowledgeScopeError("object_ref_revision_required") from exc
    registry = _object_ref_registry()
    binding = registry.resolve(ref, kind=kind, locator=locator, request_scope=scope) if registry else None
    if binding is None or binding.revision != revision or _item_revision(item) != revision:
        raise ScopedKnowledgeScopeError("object_ref_stale")


def _find_scoped_belief(repo, scope: RuntimeScope, belief_id: int) -> dict:
    for row in repo.list_scoped_beliefs(scope, limit=10000):
        if row.get("id") == belief_id:
            return row
    raise LookupError("scoped_object_not_found")


def _query_trace_valid(container, scope: RuntimeScope, body: dict) -> bool:
    trace_id = str(body.get("query_trace_id") or "").strip()
    store = getattr(container, "injection_trace_store", None)
    if not trace_id or store is None:
        return False
    try:
        return store.get_for_scope(trace_id, scope) is not None
    except Exception:
        return False


def _memory_evidence_available(container, scope: RuntimeScope, source_memory_id) -> bool:
    if source_memory_id is None or scope.session is None:
        return False
    conn = getattr(getattr(container, "db", None), "conn", None)
    if conn is None:
        return False
    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        required = {"id", "bot_id", "session_id", "visibility", "resolution_state", "quarantine"}
        if not required <= columns:
            return conn.execute(
                "SELECT 1 FROM memories WHERE id=? AND (group_id=? OR session_id=?) "
                "AND COALESCE(quarantine,0)=0 LIMIT 1",
                (int(source_memory_id), scope.session.conversation_id, scope.session.id),
            ).fetchone() is not None
        row = conn.execute(
            "SELECT 1 FROM memories WHERE id=? AND bot_id=? AND session_id=? AND visibility=? "
            "AND resolution_state='resolved' AND COALESCE(quarantine,0)=0 LIMIT 1",
            (int(source_memory_id), scope.bot_id, scope.session.id, scope.visibility),
        ).fetchone()
        if row is None and scope.visibility == "group":
            row = conn.execute(
                "SELECT 1 FROM memories WHERE id=? AND (group_id=? OR session_id=?) "
                "AND COALESCE(visibility,'group')='group' "
                "AND COALESCE(resolution_state,'resolved')='resolved' "
                "AND COALESCE(quarantine,0)=0 LIMIT 1",
                (int(source_memory_id), scope.session.conversation_id, scope.session.id),
            ).fetchone()
        return row is not None
    except Exception:
        return False


def _context_count(value, default: int = 15) -> int:
    try:
        return max(0, min(50, int(value)))
    except (TypeError, ValueError):
        return default


def _scoped_belief_evidence(container, scope: RuntimeScope, item: dict, *, before: int, after: int) -> dict:
    """只在同一 RuntimeScope 内还原 Belief 的支持/反证经历与主锚点上下文。"""
    evidence_polarity: dict[int, str] = {}
    for evidence in item.get("evidence") or []:
        if not isinstance(evidence, dict):
            continue
        try:
            memory_id = int(evidence.get("id"))
        except (TypeError, ValueError):
            continue
        if memory_id > 0:
            evidence_polarity[memory_id] = str(evidence.get("polarity") or "support")
    try:
        source_memory_id = int(item.get("source_memory_id")) if item.get("source_memory_id") is not None else None
    except (TypeError, ValueError):
        source_memory_id = None
    if source_memory_id is not None:
        evidence_polarity.setdefault(source_memory_id, "support")
    anchor_id = source_memory_id or next((memory_id for memory_id, polarity in evidence_polarity.items() if polarity == "support"), None) or next(iter(evidence_polarity), None)
    base_payload = {
        "ok": True,
        "belief": {
            "id": item.get("id"),
            "content": item.get("content"),
            "type": item.get("type"),
            "revision": item.get("revision"),
        },
        "scope": ScopeCodec.to_dict(scope),
        "anchor": None,
        "messages": [],
        "memories": [],
        "support_anchors": [],
        "challenge_anchors": [],
        "relationship_events": [],
        "episodes": [],
        "used_fallback": True,
        "reason_code": "belief_anchor_unavailable",
    }
    conn = getattr(getattr(container, "db", None), "conn", None)
    if conn is None or scope.session is None or anchor_id is None:
        return base_payload

    try:
        columns = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        required = {"id", "content", "timestamp", "bot_id", "session_id", "visibility", "resolution_state", "quarantine"}
        if not required <= columns:
            return base_payload
        group_column = "group_id" if "group_id" in columns else "session_id"
        sender_id_column = "sender_id" if "sender_id" in columns else "NULL"
        sender_name_column = "sender_name" if "sender_name" in columns else "NULL"
        select_columns = f"id, {group_column}, {sender_id_column}, {sender_name_column}, content, timestamp"
        scope_where = (
            "bot_id=? AND session_id=? AND visibility=? "
            "AND resolution_state='resolved' AND COALESCE(quarantine,0)=0"
        )
        scope_params = (scope.bot_id, scope.session.id, scope.visibility)
        anchor = conn.execute(
            f"SELECT {select_columns} FROM memories WHERE id=? AND {scope_where} LIMIT 1",
            (anchor_id, *scope_params),
        ).fetchone()
        if not anchor:
            return base_payload
        message_filter = " AND memory_type='message'" if "memory_type" in columns else ""
        anchor_ts = float(anchor[5])
        before_rows = conn.execute(
            f"SELECT {select_columns} FROM memories WHERE {scope_where} AND timestamp < ?{message_filter} "
            "ORDER BY timestamp DESC LIMIT ?",
            (*scope_params, anchor_ts, before),
        ).fetchall()
        after_rows = conn.execute(
            f"SELECT {select_columns} FROM memories WHERE {scope_where} AND timestamp > ?{message_filter} "
            "ORDER BY timestamp ASC LIMIT ?",
            (*scope_params, anchor_ts, after),
        ).fetchall()
        evidence_rows = []
        if evidence_polarity:
            marks = ",".join("?" for _ in evidence_polarity)
            evidence_rows = conn.execute(
                f"SELECT {select_columns} FROM memories WHERE id IN ({marks}) AND {scope_where} ORDER BY timestamp ASC",
                (*evidence_polarity.keys(), *scope_params),
            ).fetchall()
    except Exception:
        return base_payload

    def row_to_message(row, role: str, polarity: str | None = None) -> dict:
        payload = {
            "id": row[0],
            "group_id": row[1],
            "sender_id": row[2],
            "sender_name": row[3],
            "content": row[4],
            "timestamp": row[5],
            "role": role,
        }
        if polarity:
            payload["polarity"] = polarity
        return payload

    anchor_message = row_to_message(anchor, "anchor", evidence_polarity.get(int(anchor[0]), "support"))
    messages = [
        *[row_to_message(row, "before") for row in reversed(before_rows)],
        anchor_message,
        *[row_to_message(row, "after") for row in after_rows],
    ]
    support_anchors = []
    challenge_anchors = []
    for row in evidence_rows:
        polarity = evidence_polarity.get(int(row[0]), "support")
        target = support_anchors if polarity == "support" else challenge_anchors
        target.append(row_to_message(row, "anchor", polarity))
    return {
        **base_payload,
        "anchor": anchor_message,
        "messages": messages,
        "memories": messages,
        "support_anchors": support_anchors,
        "challenge_anchors": challenge_anchors,
        "used_fallback": False,
        "reason_code": None,
    }


def _belief_observations(repo, scope: RuntimeScope, belief_id: int | None = None) -> list[dict]:
    getter = getattr(repo, "list_scoped_belief_observations", None)
    if not callable(getter):
        return []
    try:
        return list(getter(scope, belief_id=belief_id, limit=1000) or [])
    except (TypeError, ValueError):
        try:
            return list(getter(scope, limit=1000) or [])
        except Exception:
            return []
    except Exception:
        return []


def _formal_belief(
    row: dict,
    scope: RuntimeScope,
    *,
    evidence_available: bool = False,
    observations: list[dict] | None = None,
) -> dict:
    item = dict(row)
    provenance = item.get("provenance") if isinstance(item.get("provenance"), dict) else {}
    source_memory_id = item.get("source_memory_id")
    item["type"] = _normalize_belief_type(item.pop("belief_type", "")) or "world_view"
    item["confidence"] = item.get("strength")
    item["confidence_components"] = provenance.get("confidence_components")
    item["confidence_policy_version"] = provenance.get("confidence_policy_version")
    item["confidence_evidence"] = provenance.get("confidence_evidence")
    item["anchor_sentence"] = provenance.get("anchor_sentence")
    item["evidence_health"] = "available" if evidence_available else "unavailable"
    item["quarantine_reason"] = provenance.get("quarantine_reason")
    item["bot_id"] = scope.bot_id
    item["session_id"] = scope.session.id if scope.session else None
    item["visibility"] = scope.visibility
    item["level"] = _belief_level(item.get("strength"))
    item["revision"] = _item_revision(item)
    evidence: list[dict] = []
    seen_memory_ids: set[int] = set()
    for observation in observations or []:
        polarity = str(observation.get("polarity") or "support")
        summary = "支持经历" if polarity == "support" else "反证经历"
        for raw_memory_id in observation.get("memory_ids") or []:
            try:
                memory_id = int(raw_memory_id)
            except (TypeError, ValueError):
                continue
            if memory_id <= 0 or memory_id in seen_memory_ids:
                continue
            seen_memory_ids.add(memory_id)
            evidence.append({
                "type": "memory",
                "id": str(memory_id),
                "source_scope": scope.session.id if scope.session else scope.bot_id,
                "availability": "available" if evidence_available else "unavailable",
                "summary": f"{summary} · {observation.get('window_key') or '未记录窗口'}",
                "polarity": polarity,
                "object_ref": None,
            })
    if evidence_available and source_memory_id is not None and not evidence:
        evidence.append({
            "type": "memory",
            "id": str(source_memory_id),
            "source_scope": scope.session.id if scope.session else scope.bot_id,
            "availability": "available",
            "summary": item.get("anchor_sentence") or "Belief 的同 Scope 来源锚点",
            "polarity": "support",
            "object_ref": None,
        })
    item["evidence"] = evidence
    item["observation_count"] = len(observations or [])
    item["object_ref"] = _item_object_ref(item, scope)
    can_activate = item.get("status") == "pending" and evidence_available and is_activation_eligible(provenance)
    approve_reason = None if can_activate else (
        "belief_anchor_unavailable" if not evidence_available else "belief_evidence_incomplete"
    )
    item["actions"] = {
        "approve": {"available": can_activate, "reason_code": approve_reason},
        "archive": {"available": item.get("status") != "archived", "reason_code": None if item.get("status") != "archived" else "invalid_belief_transition"},
        "restore": {"available": False, "reason_code": "belief_restore_command_unavailable"},
        "delete": {"available": False, "reason_code": "physical_delete_disabled"},
    }
    return item


def _deep_link_error(state: str):
    return jsonify(error_payload("not_found", "Resource not found")), 404


def _resolve_belief_detail(scope: RuntimeScope, *, locator: int | None = None):
    ref = request.args.get("ref")
    if not ref:
        return None, (jsonify(error_payload("object_ref_required", "Object reference is required")), 400)
    registry = _object_ref_registry()
    if registry is None:
        return None, _deep_link_error("not-found")
    binding, state = registry.resolve_with_state(
        ref,
        kind="belief",
        locator=locator,
        request_scope=scope,
    )
    if binding is None:
        return None, _deep_link_error(state)
    try:
        belief_id = int(binding.locator)
        row = _find_scoped_belief(_scoped_repo(get_container()), scope, belief_id)
    except (LookupError, TypeError, ValueError):
        return None, _deep_link_error("not-found")
    if _item_revision(row) != binding.revision:
        return None, _deep_link_error("version-stale")
    container = get_container()
    item = _formal_belief(
        row,
        scope,
        evidence_available=_memory_evidence_available(container, scope, row.get("source_memory_id")),
        observations=_belief_observations(_scoped_repo(container), scope, int(row.get("id") or 0)),
    )
    return item, None


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


@beliefs_bp.route("/", methods=["GET"])
@require_auth
async def list_beliefs():
    """正式 scoped beliefs 列表；使用统一 PageResponse。"""
    try:
        scope = _group_scope_from_query()
        limit, offset = _pagination_from_query()
        container = get_container()
        repo = _scoped_repo(container)
        rows = repo.list_scoped_beliefs(
            scope, status=request.args.get("status"), limit=10000,
        )
        belief_type = _normalize_belief_type(request.args.get("type"))
        search = (request.args.get("search") or "").strip()
        if belief_type:
            rows = [row for row in rows if _normalize_belief_type(row.get("belief_type")) == belief_type]
        if search:
            rows = [row for row in rows if search in str(row.get("content") or "")]
        observations_by_belief: dict[int, list[dict]] = {}
        for observation in _belief_observations(repo, scope):
            try:
                observations_by_belief.setdefault(int(observation.get("belief_id")), []).append(observation)
            except (TypeError, ValueError):
                continue
        items = [
            _formal_belief(
                row,
                scope,
                evidence_available=_memory_evidence_available(container, scope, row.get("source_memory_id")),
                observations=observations_by_belief.get(int(row.get("id") or 0), []),
            )
            for row in rows[offset:offset + limit]
        ]
        payload = page_response(items, total=len(rows), limit=limit, offset=offset)
        payload["scope"] = ScopeCodec.to_dict(scope)
        payload["capabilities"] = {
            "lifecycle": {"available": True, "actions": ["approve", "archive"], "reason_code": None},
            "batch_lifecycle": {"available": True, "actions": ["approve", "archive"], "reason_code": None},
            "evidence": {"available": True, "reason_code": None},
            "create": {"available": False, "reason_code": "anchored_belief_command_unavailable"},
            "edit": {"available": False, "reason_code": "belief_edit_command_unavailable"},
            "physical_delete": {"available": False, "reason_code": "physical_delete_disabled"},
            "archive_legacy": {"available": False, "reason_code": "legacy_mutation_disabled"},
            "select_all_matching": {"available": False, "reason_code": "object_ref_batch_required"},
        }
        return jsonify(payload)
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@beliefs_bp.route("/resolve", methods=["GET"])
@require_auth
async def resolve_belief():
    """仅凭 opaque ObjectRef 与显式当前 Scope 解析详情，不接受裸 ID。"""
    try:
        scope = _group_scope_from_query()
        item, failure = _resolve_belief_detail(scope)
        if failure is not None:
            return failure
        return jsonify({"item": item, "resolution": {"state": "ready"}})
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@beliefs_bp.route("/<int:belief_id>", methods=["GET"])
@require_auth
async def get_belief(belief_id: int):
    """使用 ObjectRef+当前 Scope+canonical revision 读取单条详情。"""
    try:
        scope = _group_scope_from_query()
        item, failure = _resolve_belief_detail(scope, locator=belief_id)
        if failure is not None:
            return failure
        return jsonify({"item": item, "resolution": {"state": "ready"}})
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@beliefs_bp.route("/<int:belief_id>/evidence", methods=["GET"])
@require_auth
async def get_scoped_belief_evidence(belief_id: int):
    """按 ObjectRef 与完整 RuntimeScope 还原正式信念的同 Scope 证据。"""
    try:
        scope = _group_scope_from_query()
        item, failure = _resolve_belief_detail(scope, locator=belief_id)
        if failure is not None:
            return failure
        before = _context_count(request.args.get("before"), 15)
        after = _context_count(request.args.get("after"), 15)
        return jsonify(_scoped_belief_evidence(get_container(), scope, item, before=before, after=after))
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@beliefs_bp.route("/", methods=["POST"])
@require_auth
async def create_belief():
    """没有 candidate→evidence→QualityGate 命令时禁止 WebUI 手工创建正式信念。"""
    return _scope_error("anchored_belief_command_unavailable", 503)


@beliefs_bp.route("/<int:belief_id>", methods=["PUT"])
@require_auth
async def edit_belief(belief_id: int):
    """自由编辑会绕过 confidence/evidence policy，因此正式 API 禁用。"""
    return _scope_error("belief_edit_command_unavailable", 503)


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


@beliefs_bp.route("/<int:belief_id>/approve", methods=["POST"])
@require_auth
async def approve_belief(belief_id: int):
    """审核通过 scoped belief；证据只接受同 Scope 的 source_memory_id。"""
    body = await request.get_json(silent=True) or {}
    try:
        scope = _scope_from_envelope(body)
        container = get_container()
        repo = _scoped_repo(container)
        current = _find_scoped_belief(repo, scope, belief_id)
        _require_object_ref(body, kind="belief", locator=belief_id, scope=scope, item=current)
        if not _memory_evidence_available(container, scope, current.get("source_memory_id")):
            return _scope_error("belief_anchor_unavailable", 422)
        result = BeliefLifecycleService(repo, getattr(container, "injection_trace_store", None)).transition(scope, belief_id, "approve", query_trace_id=body.get("query_trace_id"))
        return jsonify(mutation_response(
            operation_kind="belief.approve", status="succeeded", revision=None,
            item=result, include_item=True,
        ))
    except LookupError:
        return _scope_error("scoped_object_not_found", 404)
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@beliefs_bp.route("/<int:belief_id>/archive", methods=["POST"])
@require_auth
async def archive_belief(belief_id: int):
    """归档 scoped 信念。"""
    body = await request.get_json(silent=True) or {}
    try:
        scope = _scope_from_envelope(body)
        repo = _scoped_repo(get_container())
        current = _find_scoped_belief(repo, scope, belief_id)
        _require_object_ref(body, kind="belief", locator=belief_id, scope=scope, item=current)
        result = BeliefLifecycleService(repo).transition(scope, belief_id, "archive")
        return jsonify(mutation_response(
            operation_kind="belief.archive", status="succeeded", revision=None,
            item=result, include_item=True,
        ))
    except LookupError:
        return _scope_error("scoped_object_not_found", 404)
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@beliefs_bp.route("/commands/batch-lifecycle", methods=["POST"])
@require_auth
async def batch_transition_scoped_beliefs():
    """批量迁移当前页签发的信念；先全量校验，再执行领域命令。"""
    body = await request.get_json(silent=True) or {}
    try:
        scope = _scope_from_envelope(body)
        action = str(body.get("action") or "")
        entries = body.get("items")
        if action not in {"approve", "archive"}:
            raise ScopedKnowledgeScopeError("invalid_belief_transition")
        if not isinstance(entries, list) or not entries or len(entries) > 100:
            raise ScopedKnowledgeScopeError("invalid_batch_items")

        container = get_container()
        repo = _scoped_repo(container)
        validated: list[dict] = []
        seen: set[int] = set()
        for entry in entries:
            if not isinstance(entry, dict):
                raise ScopedKnowledgeScopeError("invalid_batch_item")
            belief_id = int(entry.get("id"))
            if belief_id in seen:
                continue
            seen.add(belief_id)
            current = _find_scoped_belief(repo, scope, belief_id)
            _require_object_ref(entry, kind="belief", locator=belief_id, scope=scope, item=current)
            if action == "approve":
                if current.get("status") != "pending":
                    raise ScopedKnowledgeScopeError("invalid_belief_transition")
                if not _memory_evidence_available(container, scope, current.get("source_memory_id")):
                    raise ScopedKnowledgeScopeError("belief_anchor_unavailable")
            elif current.get("status") == "archived":
                raise ScopedKnowledgeScopeError("invalid_belief_transition")
            validated.append(current)

        lifecycle = BeliefLifecycleService(repo, getattr(container, "injection_trace_store", None))
        results = [lifecycle.transition(scope, int(current["id"]), action, query_trace_id=body.get("query_trace_id")) for current in validated]
        return jsonify({
            "ok": True,
            "operation": {"kind": f"belief.batch.{action}", "status": "succeeded"},
            "transitioned_count": len(results),
            "items": results,
        })
    except LookupError:
        return _scope_error("scoped_object_not_found", 404)
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@beliefs_bp.route("/<int:belief_id>", methods=["DELETE"])
@require_auth
async def delete_belief(belief_id: int):
    """Belief 只允许生命周期归档，不允许物理删除。"""
    return _scope_error("physical_delete_disabled", 410)


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
