"""Memories Blueprint — 记忆查询、导入、统计"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
from urllib.parse import urlencode

try:
    from quart import Blueprint, current_app, jsonify, request
except ImportError:  # pragma: no cover - 本地单测可能注入不完整 fake Quart
    from types import SimpleNamespace
    from quart import Blueprint, jsonify, request

    try:
        from quart import current_app
    except ImportError:
        current_app = SimpleNamespace(extensions={})

from ..api_contract import (
    current_runtime_scope,
    error_payload,
    mutation_response,
    not_found_payload,
    page_response,
    scope_matches_memory_row,
)
from ..container import get_container
from ..middleware.auth import require_auth
from ..tag_execution import normalize_tag_execution_options

try:
    from ...engine.db.memory_repo import MemoryRevisionConflict
    from ...engine.query_engine import QueryDebugCollector, QueryOptions
    from ...services.memory_mutations import (
        MemoryMutationGateway,
        MemoryMutationTarget,
        read_memory_tag_state,
    )
except ImportError:  # pragma: no cover - focused tests import top-level packages
    from engine.db.memory_repo import MemoryRevisionConflict
    from engine.query_engine import QueryDebugCollector, QueryOptions
    from services.memory_mutations import (
        MemoryMutationGateway,
        MemoryMutationTarget,
        read_memory_tag_state,
    )

memories_bp = Blueprint("memories", __name__, url_prefix="/api")

_import_preflights: dict[str, dict] = {}


def _safe_int(val, default):
    """安全 int 转换。"""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val, default):
    """安全 float 转换。"""
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _api_composition():
    return current_app.extensions.get("wave_api_contract", {})


def _memory_columns(conn) -> set[str]:
    return {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}


def _memory_row(conn, memory_id: int) -> dict | None:
    columns = _memory_columns(conn)
    selected = [
        name for name in (
            "id", "content", "sender_id", "sender_name", "group_id", "source",
            "timestamp", "importance", "access_count", "bot_id", "session_id",
            "visibility", "resolution_state", "version",
        ) if name in columns
    ]
    if "id" not in selected:
        return None
    cursor = conn.execute(
        f"SELECT {', '.join(selected)} FROM memories WHERE id=? LIMIT 1", (memory_id,)
    )
    row = cursor.fetchone()
    return dict(zip(selected, row)) if row else None


def _request_scope():
    return current_runtime_scope(_api_composition().get("request_scope_provider"))


def _memory_ref_item(item: dict, *, existing_ref: str | None = None) -> dict:
    result = dict(item)
    scope = _request_scope()
    refs = _api_composition().get("object_refs")
    if (
        refs is None
        or scope is None
        or not scope_matches_memory_row(scope, result)
        or not isinstance(result.get("version"), int)
    ):
        return result
    ref = existing_ref or refs.issue(
        kind="memory",
        locator=result["id"],
        scope=scope,
        revision=result["version"],
    )
    scope_query = {
        "bot_id": scope.bot_id,
        "visibility": scope.visibility,
    }
    if scope.session is not None:
        scope_query["session_id"] = scope.session.id
    if scope.subject_principal_id:
        scope_query["subject_principal_id"] = scope.subject_principal_id
    result["ref"] = ref
    result["object_ref"] = {
        "ref": ref,
        "kind": "memory",
        "locator": result["id"],
        "scope_key": scope.session.id if scope.session else scope.bot_id,
        "scope_query": dict(scope_query),
        "version": result["version"],
    }
    detail_query = {"ref": ref, **scope_query}
    result["detail_url"] = f"/api/memories/{result['id']}?{urlencode(detail_query)}"
    result["mutation_url"] = result["detail_url"]
    return result


def _resolve_memory_ref_with_state(
    conn,
    *,
    ref: str | None = None,
    memory_id: int | None = None,
):
    ref = ref or request.args.get("ref")
    refs = _api_composition().get("object_refs")
    scope = _request_scope()
    if refs is None:
        return None, None, "not-found"
    binding, state = refs.resolve_with_state(
        ref,
        kind="memory",
        locator=memory_id,
        request_scope=scope,
    )
    if binding is None:
        return None, None, state
    try:
        resolved_id = int(binding.locator)
    except (TypeError, ValueError):
        return None, None, "not-found"
    row = _memory_row(conn, resolved_id)
    if row is None:
        return None, None, "not-found"
    if row.get("version") != binding.revision:
        return None, None, "version-stale"
    if scope is None or not scope_matches_memory_row(scope, row):
        return None, None, "scope-mismatch"
    return binding, row, "ready"


def _resolve_memory_ref(conn, memory_id: int, *, ref: str | None = None):
    binding, row, state = _resolve_memory_ref_with_state(
        conn,
        ref=ref,
        memory_id=memory_id,
    )
    return (binding, row) if state == "ready" else (None, None)


def _resolve_batch_refs(
    conn, values
) -> tuple[tuple[object, list[MemoryMutationTarget]] | None, tuple[dict, int] | None]:
    """Resolve selected refs to one Scope plus immutable ID/revision job targets."""
    if not isinstance(values, list) or not values:
        return None, (error_payload("object_refs_required", "Object references are required"), 400)
    scope = None
    resolved: list[MemoryMutationTarget] = []
    seen: set[int] = set()
    for value in values:
        if not isinstance(value, dict):
            return None, (error_payload("object_refs_required", "Object references are required"), 400)
        memory_id = _safe_int(value.get("id"), 0)
        ref = value.get("ref")
        if memory_id <= 0 or not isinstance(ref, str):
            return None, (error_payload("object_refs_required", "Object references are required"), 400)
        binding, row = _resolve_memory_ref(conn, memory_id, ref=ref)
        if binding is None or row is None:
            return None, (not_found_payload(), 404)
        if scope is None:
            scope = binding.scope
        elif binding.scope != scope:
            return None, (error_payload("mixed_runtime_scopes", "Batch targets must share one RuntimeScope"), 400)
        if memory_id not in seen:
            resolved.append(
                MemoryMutationTarget(memory_id=memory_id, revision=int(binding.revision))
            )
            seen.add(memory_id)
    return (scope, resolved), None


def _legacy_batch_error(body: dict) -> tuple[dict, int] | None:
    if body.get("ids") is not None and body.get("refs") is None:
        return (
            error_payload(
                "memory_object_ref_migration_required",
                "Legacy bare memory IDs are no longer accepted; reload and submit ObjectRefs",
            ),
            410,
        )
    return None


def _memory_mutation_gateway(container):
    write_gateway = getattr(container, "write_gateway", None)
    if write_gateway is None:
        return None
    try:
        return MemoryMutationGateway(write_gateway)
    except (TypeError, ValueError):
        return None


async def _enqueue_memory_job(
    container,
    *,
    kind: str,
    scope,
    targets: list[MemoryMutationTarget],
    options: dict | None = None,
    schedule_slot: str | None = None,
):
    jobs = getattr(container, "durable_jobs", None)
    enqueue = getattr(jobs, "enqueue", None)
    if not callable(enqueue):
        return None
    payload = {
        "scope": scope.to_dict(),
        "targets": [target.to_dict() for target in targets],
        **dict(options or {}),
        "requested_by": "webui",
    }
    digest = hashlib.sha256(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest()
    slot = str(schedule_slot or f"webui:{kind}:{digest}")
    return await enqueue(
        idempotency_key=f"{kind}:{digest}",
        kind=kind,
        scope=scope.to_dict(),
        payload=payload,
        schedule_slot=slot,
        cursor={"phase": "queued", "processed": 0, "total": len(targets)},
    )


@memories_bp.route("/memories/legacy/audit", methods=["GET"])
@require_auth
async def list_legacy_memories_audit():
    """分页读取无法证明完整 RuntimeScope 的旧记忆；永不签发 ObjectRef 或写能力。"""
    c = get_container()
    conn = c.db.conn
    columns = _memory_columns(conn)
    limit = max(1, min(200, _safe_int(request.args.get("limit", 25), 25)))
    offset = max(0, _safe_int(request.args.get("offset", 0), 0))
    required = {"id", "content"}
    if not required.issubset(columns):
        payload = page_response([], total=0, limit=limit, offset=offset)
        payload.update({"legacy": True, "readonly": True, "scope_status": "unresolved_legacy"})
        return jsonify(payload)

    unresolved = []
    if "resolution_state" in columns:
        unresolved.append("COALESCE(resolution_state, '') != 'resolved'")
    if "bot_id" in columns:
        unresolved.append("COALESCE(bot_id, '') = ''")
    if "session_id" in columns:
        unresolved.append("COALESCE(session_id, '') = ''")
    if "visibility" in columns:
        unresolved.append("COALESCE(visibility, '') != 'group'")
    where = ["(" + " OR ".join(unresolved) + ")"] if unresolved else ["1=1"]
    params: list = []
    search = str(request.args.get("search") or "").strip()
    if search:
        where.append("content LIKE ?")
        params.append(f"%{search}%")
    source = str(request.args.get("source") or "").strip()
    if source and "source" in columns:
        where.append("source=?")
        params.append(source)
    where_sql = " AND ".join(where)
    total = int(conn.execute(f"SELECT COUNT(*) FROM memories WHERE {where_sql}", params).fetchone()[0])
    selected = [name for name in (
        "id", "content", "sender_id", "sender_name", "group_id", "source", "timestamp",
        "importance", "bot_id", "session_id", "visibility", "resolution_state", "quarantine",
    ) if name in columns]
    cursor = conn.execute(
        f"SELECT {', '.join(selected)} FROM memories WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        (*params, limit, offset),
    )
    names = [str(column[0]) for column in cursor.description or ()]
    items = []
    for row in cursor.fetchall():
        item = dict(zip(names, row))
        item.update({
            "legacy": True,
            "readonly": True,
            "scope_status": "unresolved_legacy",
            "scope_reason": "complete_runtime_scope_unavailable",
            "object_ref": None,
            "actions": {},
        })
        items.append(item)
    payload = page_response(items, total=total, limit=limit, offset=offset)
    payload.update({
        "legacy": True,
        "readonly": True,
        "scope": None,
        "scope_status": "unresolved_legacy",
        "reason_code": "complete_runtime_scope_unavailable",
    })
    return jsonify(payload)


@memories_bp.route("/memories", methods=["GET"])
@require_auth
async def list_memories():
    """分页查看真实记忆；page/size 仅在 HTTP 边界换算为 limit/offset。"""
    c = get_container()
    page = request.args.get("page")
    size = request.args.get("size")
    if page is not None or size is not None:
        limit = max(1, min(200, _safe_int(size or 30, 30)))
        offset = (max(1, _safe_int(page or 1, 1)) - 1) * limit
    else:
        limit = max(1, min(200, _safe_int(request.args.get("limit", 50), 50)))
        offset = max(0, _safe_int(request.args.get("offset", 0), 0))

    conn = c.db.conn
    columns = _memory_columns(conn)
    scope = _request_scope()
    if scope is None or scope.session is None:
        return jsonify(page_response(
            [],
            total=None,
            limit=limit,
            offset=offset,
            unavailable_reason="scope_required",
        ))
    required_scope_columns = {"bot_id", "session_id", "visibility", "group_id", "resolution_state"}
    if not required_scope_columns.issubset(columns):
        return jsonify(page_response(
            [],
            total=None,
            limit=limit,
            offset=offset,
            unavailable_reason="scope_schema_unavailable",
        ))
    # List path is group-open: historical sessions use 羽书:group:… while runtime
    # emits qq:group:…. Exact bot/session filters would empty the management UI.
    where = ["COALESCE(group_id, '') = ?"]
    params: list = [scope.session.conversation_id]
    if "quarantine" in columns:
        where.append("COALESCE(quarantine, 0) = 0")
    if "memory_type" in columns:
        where.append(
            "COALESCE(memory_type, 'message') NOT IN "
            "('archived', 'evicted', 'deleted', 'noise')"
        )
    if "source" in columns:
        where.append("COALESCE(source, '') != 'noise'")
    exact_id = _safe_int(request.args.get("id"), 0)
    if exact_id > 0:
        # 观测台等页面按记忆编号深链：仍受当前群过滤，且只有作用域完全吻合的行才会签发 ref。
        where.append("id = ?")
        params.append(exact_id)
    before_id = request.args.get("before_id")
    if before_id:
        where.append("id < ?")
        params.append(_safe_int(before_id, 0))
        offset = 0
    for name in ("source", "sender_id", "group_id"):
        value = request.args.get(name)
        if value and name in columns:
            where.append(f"{name} = ?")
            params.append(value)
    sender = request.args.get("sender")
    if sender and "sender_name" in columns:
        where.append("sender_name = ?")
        params.append(sender)
    bot_id = request.args.get("bot_id")
    if bot_id and "bot_id" in columns:
        where.append("bot_id = ?")
        params.append(bot_id)
    search = (request.args.get("search") or "").strip()
    if search:
        where.append("content LIKE ?")
        params.append(f"%{search}%")
    has_vector = request.args.get("has_vector")
    if has_vector == "true":
        where.append("vector IS NOT NULL")
    elif has_vector == "false":
        where.append("vector IS NULL")
    has_tags = request.args.get("has_tags")
    if has_tags == "true":
        where.append("EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id = memories.id)")
    elif has_tags == "false":
        where.append("NOT EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id = memories.id)")

    where_sql = " AND ".join(where)
    total = int(conn.execute(
        f"SELECT COUNT(*) FROM memories WHERE {where_sql}", params
    ).fetchone()[0])
    selected = [
        name for name in (
            "id", "content", "sender_id", "sender_name", "group_id", "source",
            "timestamp", "importance", "access_count", "bot_id", "session_id",
            "visibility", "resolution_state", "version",
        ) if name in columns
    ]
    select_sql = ", ".join(selected + ["vector IS NOT NULL AS has_vector"])
    cursor = conn.execute(
        f"SELECT {select_sql} FROM memories WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
        params + [limit, offset],
    )
    names = [str(column[0]) for column in cursor.description or ()]
    items = []
    for row in cursor.fetchall():
        item = dict(zip(names, row))
        item["has_vector"] = bool(item.get("has_vector"))
        items.append(_memory_ref_item(item))
    return jsonify(page_response(items, total=total, limit=limit, offset=offset))


@memories_bp.route("/memories/senders", methods=["GET"])
@require_auth
async def list_senders():
    """发送者列表（按记忆数排序，供筛选下拉）。"""
    c = get_container()
    limit = max(1, min(500, _safe_int(request.args.get("limit", 100), 100)))
    scope = _request_scope()
    if scope is None or scope.session is None:
        return jsonify({"senders": [], "source": {"status": "unknown", "reason_code": "scope_required"}})
    rows = c.db.conn.execute(
        """SELECT sender_name, COUNT(*) AS cnt FROM memories
           WHERE sender_name IS NOT NULL AND sender_name != ''
             AND bot_id=? AND session_id=? AND visibility=? AND group_id=?
             AND resolution_state='resolved'
           GROUP BY sender_name ORDER BY cnt DESC LIMIT ?""",
        (
            scope.bot_id,
            scope.session.id,
            scope.visibility,
            scope.session.conversation_id,
            limit,
        ),
    ).fetchall()
    return jsonify({
        "senders": [{"name": r[0], "count": r[1]} for r in rows],
        "source": {"status": "healthy", "reason_code": None},
    })


def _memory_deep_link_error(state: str):
    # Detail reads intentionally collapse missing, scope mismatch and stale
    # revisions into one non-leaking response. Mutations retain explicit codes.
    return jsonify(not_found_payload()), 404


@memories_bp.route("/memories/resolve", methods=["GET"])
@require_auth
async def resolve_memory():
    """仅凭 opaque ObjectRef 与显式当前 Scope 解析记忆详情。"""
    if not request.args.get("ref"):
        return jsonify(error_payload("object_ref_required", "Object reference is required")), 400
    _, item, state = _resolve_memory_ref_with_state(get_container().db.conn)
    if item is None:
        return _memory_deep_link_error(state)
    return jsonify({
        "item": _memory_ref_item(item, existing_ref=request.args.get("ref")),
        "resolution": {"state": "ready"},
    })


@memories_bp.route("/memories/<int:memory_id>", methods=["GET"])
@require_auth
async def get_memory(memory_id: int):
    """仅通过服务端签发且与当前 Scope/revision 相符的 ObjectRef 读取详情。"""
    conn = get_container().db.conn
    if not request.args.get("ref"):
        return jsonify(error_payload("object_ref_required", "Object reference is required")), 400
    _, item, state = _resolve_memory_ref_with_state(conn, memory_id=memory_id)
    if item is None:
        return _memory_deep_link_error(state)
    return jsonify({
        "item": _memory_ref_item(item, existing_ref=request.args.get("ref")),
        "resolution": {"state": "ready"},
    })


@memories_bp.route("/memories/<int:memory_id>", methods=["PUT"])
@require_auth
async def update_memory(memory_id: int):
    """使用 scoped ObjectRef 乐观更新记忆并推进 revision。"""
    c = get_container()
    conn = c.db.conn
    if not request.args.get("ref"):
        return jsonify(error_payload("object_ref_required", "Object reference is required")), 400
    binding, item = _resolve_memory_ref(conn, memory_id)
    if binding is None or item is None:
        return jsonify(not_found_payload()), 404
    body = await request.get_json(silent=True) or {}
    fields = {
        name: body[name]
        for name in ("content", "importance")
        if name in body
    }
    if not fields:
        return jsonify(error_payload("invalid_request", "No mutable fields were supplied")), 400
    gateway = _memory_mutation_gateway(c)
    if gateway is None:
        return jsonify(error_payload(
            "memory_mutation_gateway_unavailable",
            "The coordinated memory mutation gateway is unavailable",
            retryable=True,
        )), 503
    try:
        result = await gateway.update_memory(
            scope=binding.scope,
            target=MemoryMutationTarget(memory_id=memory_id, revision=binding.revision),
            **fields,
        )
    except MemoryRevisionConflict:
        return jsonify(not_found_payload()), 404
    except ValueError as exc:
        return jsonify(error_payload("invalid_request", str(exc))), 400
    refreshed = _memory_row(conn, memory_id)
    if refreshed is None:
        return jsonify(not_found_payload()), 404
    return jsonify(
        mutation_response(
            operation_kind="memory.update",
            operation_id=result.operation_id,
            status="succeeded",
            revision=result.revision,
            item=_memory_ref_item(refreshed),
            include_item=True,
        )
    )


@memories_bp.route("/memories/<int:memory_id>", methods=["DELETE"])
@require_auth
async def delete_memory(memory_id: int):
    """使用 scoped ObjectRef 删除单条记忆。"""
    c = get_container()
    if not request.args.get("ref"):
        return jsonify(error_payload("object_ref_required", "Object reference is required")), 400
    binding, item = _resolve_memory_ref(c.db.conn, memory_id)
    if binding is None or item is None:
        return jsonify(not_found_payload()), 404
    gateway = _memory_mutation_gateway(c)
    if gateway is None:
        return jsonify(error_payload(
            "memory_mutation_gateway_unavailable",
            "The coordinated memory mutation gateway is unavailable",
            retryable=True,
        )), 503
    try:
        result = await gateway.delete_memories(
            scope=binding.scope,
            targets=[MemoryMutationTarget(memory_id=memory_id, revision=binding.revision)],
        )
    except MemoryRevisionConflict:
        return jsonify(not_found_payload()), 404
    return jsonify(
        mutation_response(
            operation_kind="memory.delete",
            operation_id=result.operation_id,
            status="succeeded",
            revision=result.revision,
        )
    )


@memories_bp.route("/memories/<int:memory_id>/re-embed", methods=["POST"])
@require_auth
async def re_embed_memory(memory_id: int):
    """重新向量化单条记忆。"""
    c = get_container()
    if not request.args.get("ref"):
        return jsonify(error_payload("object_ref_required", "Object reference is required")), 400
    binding, detail = _resolve_memory_ref(c.db.conn, memory_id)
    if binding is None or detail is None:
        return jsonify(not_found_payload()), 404
    envelope = await _enqueue_memory_job(
        c,
        kind="memory.reembed.v1",
        scope=binding.scope,
        targets=[MemoryMutationTarget(memory_id=memory_id, revision=binding.revision)],
        schedule_slot=request.args.get("schedule_slot"),
    )
    if envelope is None:
        return jsonify(error_payload(
            "durable_jobs_unavailable",
            "Durable memory jobs are unavailable",
            retryable=True,
        )), 503
    return jsonify(envelope.to_dict()), 202


@memories_bp.route("/memories/batch/delete", methods=["POST"])
@require_auth
async def batch_delete_memories():
    """批量删除记忆。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    legacy_error = _legacy_batch_error(body)
    if legacy_error is not None:
        payload, status = legacy_error
        return jsonify(payload), status
    resolved, batch_error = _resolve_batch_refs(c.db.conn, body.get("refs"))
    if batch_error is not None:
        payload, status = batch_error
        return jsonify(payload), status
    assert resolved is not None
    scope, targets = resolved
    gateway = _memory_mutation_gateway(c)
    if gateway is None:
        return jsonify(error_payload(
            "memory_mutation_gateway_unavailable",
            "The coordinated memory mutation gateway is unavailable",
            retryable=True,
        )), 503
    try:
        result = await gateway.delete_memories(scope=scope, targets=targets)
    except MemoryRevisionConflict:
        return jsonify(not_found_payload()), 404
    return jsonify(mutation_response(
        operation_kind="memory.batch.delete",
        operation_id=result.operation_id,
        status="succeeded",
        revision=None,
        item={"deleted": len(result.targets)},
        include_item=True,
    ))


@memories_bp.route("/memories/batch/re-embed", methods=["POST"])
@require_auth
async def batch_re_embed():
    """批量重新向量化（SSE 流）。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    legacy_error = _legacy_batch_error(body)
    if legacy_error is not None:
        payload, status = legacy_error
        return jsonify(payload), status
    resolved, batch_error = _resolve_batch_refs(c.db.conn, body.get("refs"))
    if batch_error is not None:
        payload, status = batch_error
        return jsonify(payload), status
    assert resolved is not None
    scope, targets = resolved
    envelope = await _enqueue_memory_job(
        c,
        kind="memory.batch.reembed.v1",
        scope=scope,
        targets=targets,
        schedule_slot=body.get("schedule_slot"),
    )
    if envelope is None:
        return jsonify(error_payload(
            "durable_jobs_unavailable",
            "Durable memory jobs are unavailable",
            retryable=True,
        )), 503
    return jsonify(envelope.to_dict()), 202


@memories_bp.route("/memories/batch/extract-tags", methods=["POST"])
@require_auth
async def batch_extract_tags_for_ids():
    """对选中记忆批量提取 Tag（SSE 流）。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    legacy_error = _legacy_batch_error(body)
    if legacy_error is not None:
        payload, status = legacy_error
        return jsonify(payload), status
    try:
        tag_options = normalize_tag_execution_options(body)
    except ValueError as exc:
        return jsonify(error_payload("invalid_tag_options", str(exc))), 400
    resolved, batch_error = _resolve_batch_refs(c.db.conn, body.get("refs"))
    if batch_error is not None:
        payload, status = batch_error
        return jsonify(payload), status
    assert resolved is not None
    scope, targets = resolved
    envelope = await _enqueue_memory_job(
        c,
        kind="memory.batch.extract_tags.v1",
        scope=scope,
        targets=targets,
        options={
            "tag_batch_size": tag_options["tag_batch_size"],
            "tag_write_policy": tag_options["tag_write_policy"],
            "skip_short_min_length": tag_options["skip_short_min_length"],
        },
        schedule_slot=body.get("schedule_slot"),
    )
    if envelope is None:
        return jsonify(error_payload(
            "durable_jobs_unavailable",
            "Durable memory jobs are unavailable",
            retryable=True,
        )), 503
    return jsonify(envelope.to_dict()), 202



@memories_bp.route("/memories/stats", methods=["GET"])
@require_auth
async def memory_stats():
    """各 source 记忆统计。"""
    c = get_container()
    rows = c.db.conn.execute(
        "SELECT source, COUNT(*) FROM memories GROUP BY source ORDER BY COUNT(*) DESC"
    ).fetchall()
    total = sum(r[1] for r in rows)
    by_source = {r[0] or "unknown": r[1] for r in rows}
    return jsonify({"total": total, "by_source": by_source})


@memories_bp.route("/memories/<int:memory_id>", methods=["PATCH"])
@require_auth
async def patch_memory(memory_id: int):
    """旧裸 PATCH 永久停用；正式记忆变更只允许带 ObjectRef 的 PUT。"""
    return jsonify(error_payload("legacy_mutation_disabled", "Bare PATCH memory mutation is no longer supported")), 410


@memories_bp.route("/query", methods=["POST"])
@require_auth
async def query_test():
    """通过正式 QueryEngine 执行显式只读的高级检索调试。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    if not text:
        return jsonify(error_payload("query_text_required", "Query text is required")), 400

    scope = _request_scope()
    if scope is None or scope.visibility != "group" or scope.session is None:
        return jsonify(error_payload(
            "canonical_group_scope_required",
            "A canonical group RuntimeScope is required",
        )), 400

    query_engine = getattr(c, "query_engine", None)
    if query_engine is None:
        return jsonify(error_payload(
            "query_engine_unavailable",
            "The formal query engine is unavailable",
            retryable=True,
        )), 503

    top_k = max(1, min(_safe_int(body.get("top_k", 5), 5), 50))
    source_filter = str(body.get("source_filter") or "").strip() or None
    exclude_sources = body.get("exclude_sources") if isinstance(body.get("exclude_sources"), list) else []
    exclude_sources = [str(item).strip() for item in exclude_sources if str(item).strip()]
    raw_stages = body.get("stages") if isinstance(body.get("stages"), dict) else {}
    raw_params = body.get("params") if isinstance(body.get("params"), dict) else {}

    def _bool_flag(value, default=False):
        if value is None:
            return default
        if isinstance(value, bool):
            return value
        if isinstance(value, str):
            return value.strip().lower() in {"1", "true", "yes", "on"}
        return bool(value)

    stages = {}
    for stage_name, legacy_key, default in (
        ("epa", "enable_epa", True),
        ("pyramid", "enable_pyramid", True),
        ("spike", "enable_spike", True),
        ("geodesic", "enable_geodesic", True),
    ):
        stages[stage_name] = _bool_flag(
            raw_stages.get(stage_name) if stage_name in raw_stages else body.get(legacy_key),
            default,
        )

    options = QueryOptions(touch=False, stages=stages, params=raw_params)
    collector = QueryDebugCollector(
        max_items_per_partition=50,
        max_total_bytes=48_000,
    )
    debug_requested = _bool_flag(body.get("debug"), True)
    started = time.perf_counter()
    try:
        results = await query_engine.query(
            text,
            group_id=scope.session.conversation_id,
            top_k=top_k,
            exclude_sources=exclude_sources,
            source_filter=source_filter,
            scope=scope,
            options=options,
            collector=collector,
        )
    except Exception as exc:
        # 调试器失败不扩散到普通 WebUI 回复；返回稳定、可重试的调试 envelope。
        try:
            collector.warn("query", "query_debug_execution_failed", str(exc))
            collector.record("final", {
                "result_count": 0,
                "reason_code": "query_debug_execution_failed",
                "readonly": True,
                "touch": False,
            })
        except Exception:
            pass
        results = []

    elapsed_ms = round((time.perf_counter() - started) * 1000, 1)
    debug = collector.snapshot() if debug_requested else {}
    timing = dict(debug.get("timing") or {}) if isinstance(debug, dict) else {}
    timing["total_ms"] = elapsed_ms
    payload = {
        "results": results,
        "timing": timing,
        "debug": debug,
        "readonly": True,
        "touch": False,
    }
    return jsonify(payload)


def _memory_context_rows(conn, row: dict, *, before: int, after: int) -> list[dict]:
    """同 Bot、同群按 id 取前后若干条消息（id 即写入顺序），排除隔离与噪声。"""
    columns = _memory_columns(conn)
    if not {"id", "group_id", "content"}.issubset(columns):
        return []
    selected = [name for name in ("id", "sender_id", "sender_name", "content", "timestamp", "source") if name in columns]
    where = ["group_id = ?"]
    params: list = [row.get("group_id")]
    if "bot_id" in columns:
        where.append("bot_id IS ?")
        params.append(row.get("bot_id"))
    if "quarantine" in columns:
        where.append("COALESCE(quarantine, 0) = 0")
    if "memory_type" in columns:
        where.append("COALESCE(memory_type, 'message') NOT IN ('archived', 'evicted', 'deleted', 'noise')")
    if "source" in columns:
        where.append("COALESCE(source, '') != 'noise'")
    where_sql = " AND ".join(where)
    select_sql = ", ".join(selected)
    memory_id = int(row["id"])

    def fetch(op: str, order: str, limit: int) -> list[dict]:
        if limit <= 0:
            return []
        cursor = conn.execute(
            f"SELECT {select_sql} FROM memories WHERE {where_sql} AND id {op} ? ORDER BY id {order} LIMIT ?",
            params + [memory_id, limit],
        )
        return [dict(zip(selected, values)) for values in cursor.fetchall()]

    anchor = {name: row.get(name) for name in selected}
    messages = [{**item, "role": "before"} for item in reversed(fetch("<", "DESC", before))]
    messages.append({**anchor, "role": "anchor"})
    messages.extend({**item, "role": "after"} for item in fetch(">", "ASC", after))
    return messages


@memories_bp.route("/memories/<int:memory_id>/context", methods=["GET"])
@require_auth
async def get_memory_context(memory_id: int):
    """按服务端签发的 ref 读取这条记忆在同群里的前后消息（只读）。"""
    conn = get_container().db.conn
    if not request.args.get("ref"):
        return jsonify(error_payload("object_ref_required", "Object reference is required")), 400
    binding, row = _resolve_memory_ref(conn, memory_id)
    if binding is None or row is None:
        return jsonify(not_found_payload()), 404
    before = max(0, min(20, _safe_int(request.args.get("before"), 5)))
    after = max(0, min(20, _safe_int(request.args.get("after"), 5)))
    return jsonify({
        "memory_id": int(row["id"]),
        "group_id": row.get("group_id"),
        "messages": _memory_context_rows(conn, row, before=before, after=after),
        "before": before,
        "after": after,
    })


@memories_bp.route("/memories/<int:memory_id>/similar", methods=["GET"])
@require_auth
async def get_similar_memories(memory_id: int):
    """基于内存 C-HNSW 检索单条记忆的 Top 3 相似推荐（≤15ms 高性能，零 SQLite 向量扫描）。"""
    c = get_container()
    if not request.args.get("ref"):
        return jsonify(error_payload("object_ref_required", "Object reference is required")), 400
    binding, detail = _resolve_memory_ref(c.db.conn, memory_id)
    if binding is None or detail is None:
        return jsonify(not_found_payload()), 404
    row = c.db.conn.execute("SELECT vector FROM memories WHERE id=?", (memory_id,)).fetchone()
    if not row or not row[0]:
        return jsonify({"items": [], "reason": "no_vector"})

    vec_blob = row[0]
    import struct
    try:
        num_floats = len(vec_blob) // 4
        vector = list(struct.unpack(f"{num_floats}f", vec_blob))
    except Exception as exc:
        return jsonify({"items": [], "reason": f"unpack_failed: {exc}"})

    if not getattr(c, "memory_index", None):
        return jsonify({"items": [], "reason": "hnsw_index_unavailable"})

    # 1. knn 快速查询
    try:
        candidates = c.memory_index.search(vector, k=5)  # 多取几条，过滤掉自身
    except Exception as exc:
        return jsonify({"items": [], "reason": f"hnsw_query_failed: {exc}"})

    similar_ids = []
    distances = {}
    for mid, dist in candidates:
        if mid != memory_id:
            similar_ids.append(mid)
            distances[mid] = dist
        if len(similar_ids) >= 3:
            break

    if not similar_ids:
        return jsonify({"items": []})

    # 2. SQLite 主键点查
    placeholders = ",".join("?" for _ in similar_ids)
    scope = binding.scope
    assert scope.session is not None
    rows = c.db.conn.execute(
        f"SELECT id, content, source FROM memories WHERE id IN ({placeholders}) "
        "AND bot_id=? AND session_id=? AND visibility=? AND group_id=? "
        "AND resolution_state='resolved'",
        similar_ids + [
            scope.bot_id,
            scope.session.id,
            scope.visibility,
            scope.session.conversation_id,
        ],
    ).fetchall()

    items = []
    for r in rows:
        mid = r[0]
        dist = distances.get(mid, 0.5)
        # 相似度 = 1.0 - 距离 (100% 格式展示)
        similarity = round((1.0 - float(dist)) * 100, 1)
        items.append({
            "id": mid,
            "content": r[1] or "",
            "source": r[2] or "",
            "similarity": similarity
        })

    # 按相似度降序排序
    items.sort(key=lambda x: x["similarity"], reverse=True)
    return jsonify({"items": items})


def _memory_tag_state_payload(conn, *, scope, memory_id: int) -> dict:
    state = read_memory_tag_state(conn, scope=scope, memory_id=memory_id)
    manual = state.get("manual")
    refs = _api_composition().get("object_refs")
    if isinstance(manual, dict) and refs is not None:
        correction_ref = refs.issue(
            kind="memory_tag_correction",
            locator=manual["correction_id"],
            scope=scope,
            revision=int(manual["revision"]),
        )
        manual["ref"] = correction_ref
        manual["object_ref"] = {
            "ref": correction_ref,
            "kind": "memory_tag_correction",
            "locator": manual["correction_id"],
            "scope_key": scope.session.id if scope.session else scope.bot_id,
            "version": int(manual["revision"]),
        }
    return state


@memories_bp.route("/memories/<int:memory_id>/tags", methods=["GET"])
@require_auth
async def get_memory_tag_state(memory_id: int):
    c = get_container()
    if not request.args.get("ref"):
        return jsonify(error_payload("object_ref_required", "Object reference is required")), 400
    binding, item = _resolve_memory_ref(c.db.conn, memory_id)
    if binding is None or item is None:
        return jsonify(not_found_payload()), 404
    return jsonify({"item": _memory_tag_state_payload(c.db.conn, scope=binding.scope, memory_id=memory_id)})


@memories_bp.route("/memories/<int:memory_id>/tags/correction", methods=["POST"])
@require_auth
async def correct_memory_tags(memory_id: int):
    c = get_container()
    if not request.args.get("ref"):
        return jsonify(error_payload("object_ref_required", "Object reference is required")), 400
    binding, item = _resolve_memory_ref(c.db.conn, memory_id)
    if binding is None or item is None:
        return jsonify(not_found_payload()), 404
    body = await request.get_json(silent=True) or {}
    try:
        result = await _memory_mutation_gateway(c).correct_memory_tags(
            scope=binding.scope,
            target=MemoryMutationTarget(memory_id=memory_id, revision=int(binding.revision)),
            operation=body.get("operation"),
            tags=body.get("tags") if isinstance(body.get("tags"), list) else (),
            reason=body.get("reason"),
        )
    except ValueError as exc:
        return jsonify(error_payload("invalid_memory_tag_correction", str(exc))), 400
    except MemoryRevisionConflict:
        return jsonify(not_found_payload()), 404
    refreshed = _memory_row(c.db.conn, memory_id)
    if refreshed is None:
        return jsonify(not_found_payload()), 404
    state = _memory_tag_state_payload(c.db.conn, scope=binding.scope, memory_id=memory_id)
    return jsonify(mutation_response(
        operation_kind="memory.tags.correct",
        operation_id=result.operation_id,
        status="succeeded",
        revision=result.revision,
        item={"memory": _memory_ref_item(refreshed), "tags": state},
        include_item=True,
    ))


@memories_bp.route("/memories/<int:memory_id>/tags/correction/undo", methods=["POST"])
@require_auth
async def undo_memory_tag_correction(memory_id: int):
    c = get_container()
    memory_ref = request.args.get("ref")
    if not memory_ref:
        return jsonify(error_payload("object_ref_required", "Memory object reference is required")), 400
    binding, item = _resolve_memory_ref(c.db.conn, memory_id, ref=memory_ref)
    if binding is None or item is None:
        return jsonify(not_found_payload()), 404
    body = await request.get_json(silent=True) or {}
    correction_ref = body.get("correction_ref")
    refs = _api_composition().get("object_refs")
    correction_binding, _correction_state = refs.resolve_with_state(
        correction_ref,
        kind="memory_tag_correction",
        request_scope=binding.scope,
    ) if refs is not None else (None, "not-found")
    if correction_binding is None:
        return jsonify(not_found_payload()), 404
    try:
        result = await _memory_mutation_gateway(c).undo_memory_tag_correction(
            scope=binding.scope,
            target=MemoryMutationTarget(memory_id=memory_id, revision=int(binding.revision)),
            correction_id=str(correction_binding.locator),
            correction_revision=int(correction_binding.revision),
            reason=body.get("reason"),
        )
    except ValueError as exc:
        return jsonify(error_payload("invalid_memory_tag_correction_undo", str(exc))), 400
    except MemoryRevisionConflict:
        return jsonify(not_found_payload()), 404
    refreshed = _memory_row(c.db.conn, memory_id)
    if refreshed is None:
        return jsonify(not_found_payload()), 404
    state = _memory_tag_state_payload(c.db.conn, scope=binding.scope, memory_id=memory_id)
    return jsonify(mutation_response(
        operation_kind="memory.tags.undo",
        operation_id=result.operation_id,
        status="succeeded",
        revision=result.revision,
        item={"memory": _memory_ref_item(refreshed), "tags": state},
        include_item=True,
    ))


@memories_bp.route("/memories/<int:memory_id>/tags", methods=["POST"])
@memories_bp.route("/memories/<int:memory_id>/tags/<tag_name>", methods=["DELETE"])
@require_auth
async def legacy_memory_tag_mutation(memory_id: int, tag_name: str | None = None):
    del memory_id, tag_name
    if not request.args.get("ref"):
        return jsonify(error_payload("object_ref_required", "Object reference is required")), 400
    return jsonify(error_payload(
        "memory_tag_correction_required",
        "Legacy direct Tag mutation was removed; use the scoped correction endpoint with reason and ObjectRefs",
    )), 410


@memories_bp.route("/memories/import/sources", methods=["GET"])
@memories_bp.route("/import/sources", methods=["GET"])
@require_auth
async def discover_sources():
    """数据源发现。"""
    c = get_container()
    from ..source_discovery import SourceDiscovery
    refresh = request.args.get("refresh", "").lower() == "true"

    discovery = SourceDiscovery()
    sources = discovery.discover_all()
    result = []
    for s in sources:
        progress = discovery.estimate_imported(s, c.db)
        result.append({
            "id": s["id"], "name": s["name"], "description": s["description"],
            "count": s["count"], "type": s["type"],
            "db_path": s.get("db_path", ""),
            "has_adapter": s["type"] == "known",
            "imported_pct": progress["estimated_pct"],
            "remaining": progress["estimated_remaining"],
        })
    return jsonify({"sources": result})


@memories_bp.route("/import/preview", methods=["POST"])
@require_auth
async def import_preview():
    """导入预览。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    source = body.get("source", "")
    from ..importer import WaveMemoryImporter
    importer = WaveMemoryImporter(c.db, c.embedding_service, c.tag_extractor)
    result = await importer.preview(source)
    return jsonify(result)


@memories_bp.route("/import/preflight", methods=["POST"])
@require_auth
async def import_preflight():
    """Resolve a discovered source and issue a short-lived token for the exact import request."""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    source_id = str(body.get("source_id") or "").strip()
    if not source_id:
        return jsonify(error_payload("import_source_required", "Import source is required")), 400
    from ..source_discovery import SourceDiscovery

    discovery = SourceDiscovery()
    source = next((item for item in discovery.discover_all() if item.get("id") == source_id), None)
    if source is None:
        return jsonify(error_payload("import_source_not_found", "Import source was not found")), 404
    limit = max(1, min(_safe_int(body.get("limit", 2000), 2000), 50_000))
    progress = discovery.estimate_imported(source, c.db)
    target = str((source.get("adapter") or {}).get("target", "memories"))
    token = secrets.token_urlsafe(32)
    checked_at = time.time()
    request_payload = {
        "source_id": source_id,
        "limit": limit,
        "extract_tags": bool(body.get("extract_tags", True)),
        "tag_batch_size": max(1, min(_safe_int(body.get("tag_batch_size", 20), 20), 100)),
        "tag_write_policy": str(body.get("tag_write_policy") or "missing_only"),
    }
    _import_preflights[token] = {
        "expires_at": checked_at + 600.0,
        "request": request_payload,
    }
    return jsonify({
        "source": {
            "id": source_id,
            "name": source.get("name"),
            "description": source.get("description"),
            "count": int(source.get("count") or 0),
            "type": source.get("type"),
            "target": target,
        },
        "preview": {
            "total_count": int(source.get("count") or 0),
            "estimated_imported": int(progress.get("estimated_imported") or 0),
            "estimated_remaining": int(progress.get("estimated_remaining") or 0),
            "limit": limit,
            "re_embed": target == "memories",
            "extract_tags": request_payload["extract_tags"],
        },
        "preflight_token": token,
        "checked_at": checked_at,
        "source_status": "available",
    })


@memories_bp.route("/import/start", methods=["POST"])
@require_auth
async def import_start():
    """开始导入（SSE 流）。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    source = str(body.get("source") or "")
    source_id = str(body.get("source_id") or "")
    re_embed = body.get("re_embed", True)
    extract_tags = body.get("extract_tags", True)
    batch_size = _safe_int(body.get("batch_size", 20), 20)
    mode = "legacy"
    request_payload = {
        "mode": "legacy",
        "source": source,
        "re_embed": bool(re_embed),
        "extract_tags": bool(extract_tags),
        "batch_size": batch_size,
        "requested_by": "webui",
    }
    idempotency_source = f"legacy:{source}"
    if source_id:
        token = str(body.get("preflight_token") or "")
        preflight = _import_preflights.get(token)
        expected = preflight.get("request") if isinstance(preflight, dict) else None
        if (
            not isinstance(preflight, dict)
            or float(preflight.get("expires_at") or 0) < time.time()
            or not isinstance(expected, dict)
            or expected.get("source_id") != source_id
        ):
            return jsonify(error_payload("import_preflight_required", "A valid import preflight is required")), 409
        mode = "discovered_source"
        request_payload = {"mode": mode, **expected, "requested_by": "webui"}
        idempotency_source = f"source:{source_id}:{token}"

    jobs = getattr(c, "durable_jobs", None)
    if jobs is None:
        return jsonify({"ok": False, "error": "durable_jobs_unavailable"}), 503
    schedule_slot = str(body.get("schedule_slot") or f"manual-{int(time.time() // 60)}")
    request_record = await jobs.create_request(
        idempotency_key=f"import:{idempotency_source}:{schedule_slot}",
        kind="maintenance.import.run",
        scope={"kind": "system_maintenance"},
        payload=request_payload,
    )
    run = await jobs.schedule_run(
        request_id=request_record.request_id,
        schedule_slot=schedule_slot,
        cursor_generation=0,
        cursor={"phase": "queued"},
    )
    return jsonify({
        "ok": False,
        "accepted": True,
        "request_id": request_record.request_id,
        "job_id": run.run_id,
        "status": run.status,
        "operation": {
            "id": run.run_id,
            "kind": "maintenance.import.run",
            "status": "queued" if run.status == "pending" else run.status,
        },
        "revision": None,
    }), 202


@memories_bp.route("/memories/import/run", methods=["POST"])
@memories_bp.route("/import/from-source", methods=["POST"])
@require_auth
async def import_from_source():
    """从指定数据源导入（SSE 流）。"""
    c = get_container()
    source_id = request.args.get("source_id", "")
    limit = _safe_int(request.args.get("limit", 5000), 5000)
    try:
        tag_options = normalize_tag_execution_options(request.args, defaults={"extract_tags": False})
    except ValueError as exc:
        return jsonify({"error": str(exc)}), 400
    extract_tags = tag_options["extract_tags"]
    tag_batch_size = tag_options["tag_batch_size"]
    tag_write_policy = tag_options["tag_write_policy"]

    from ..source_discovery import SourceDiscovery
    discovery = SourceDiscovery()
    all_sources = discovery.discover_all()
    source = next((s for s in all_sources if s["id"] == source_id), None)

    if not source:
        return jsonify({"error": f"Source not found: {source_id}"}), 404

    jobs = getattr(c, "durable_jobs", None)
    if jobs is None:
        return jsonify({"ok": False, "error": "durable_jobs_unavailable"}), 503
    schedule_slot = str(
        request.args.get("schedule_slot") or f"manual-{int(time.time() // 60)}"
    )
    request_record = await jobs.create_request(
        idempotency_key=f"import:source:{source_id}:{schedule_slot}",
        kind="maintenance.import.run",
        scope={"kind": "system_maintenance"},
        payload={
            "mode": "discovered_source",
            "source_id": source_id,
            "limit": limit,
            "extract_tags": extract_tags,
            "tag_batch_size": tag_batch_size,
            "tag_write_policy": tag_write_policy,
            "requested_by": "webui",
        },
    )
    run = await jobs.schedule_run(
        request_id=request_record.request_id,
        schedule_slot=schedule_slot,
        cursor_generation=0,
        cursor={"phase": "queued"},
    )
    return jsonify({
        "ok": True,
        "accepted": True,
        "request_id": request_record.request_id,
        "job_id": run.run_id,
        "status": run.status,
    }), 202


@memories_bp.route("/memories/import/llm-extract", methods=["POST"])
@require_auth
async def legacy_import_llm_extract():
    """旧前端兼容：转发到当前批量 Tag 提取 SSE。"""
    from .tags import batch_extract_tags
    return await batch_extract_tags()


@memories_bp.route("/memories/import/llm-extract/stop", methods=["POST"])
@require_auth
async def legacy_import_llm_extract_stop():
    """旧前端兼容：当前批量 Tag 提取不支持安全中止，避免旧包触发 404。"""
    return jsonify({"ok": False, "message": "当前批量标签提取任务不支持安全中止"})
