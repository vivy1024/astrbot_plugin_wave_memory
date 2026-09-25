"""Tags Blueprint — Tag CRUD、审计、批量操作"""

from __future__ import annotations

import asyncio
import json
import math
import time
from functools import wraps

from quart import Blueprint, current_app, jsonify, request, Response

from ..container import get_container
from ..middleware.auth import require_auth
from ..api_contract import current_runtime_scope, error_payload, mutation_response, page_response
from ..tag_execution import normalize_tag_execution_options, tag_memory_batch

try:
    from ...domain.scope import scope_to_dict
    from ...services.tag_governance import TagGovernanceError, TagGovernanceGateway
except ImportError:  # pragma: no cover - focused tests import webui as top-level
    from domain.scope import scope_to_dict
    from services.tag_governance import TagGovernanceError, TagGovernanceGateway

tags_bp = Blueprint("tags", __name__, url_prefix="/api/tags")


def _scope_error(code: str, status: int):
    return jsonify({"error": {"code": code}}), status


def _request_scope():
    try:
        provider = current_app.extensions.get("wave_api_contract", {}).get("request_scope_provider")
    except RuntimeError:
        provider = None
    return current_runtime_scope(provider)


def _object_refs():
    try:
        return current_app.extensions.get("wave_api_contract", {}).get("object_refs")
    except RuntimeError:
        return None


def _governance_gateway(container):
    configured = getattr(container, "tag_governance", None)
    if configured is not None:
        return configured
    write_gateway = getattr(container, "write_gateway", None)
    if write_gateway is None:
        return None
    try:
        gateway = TagGovernanceGateway(write_gateway)
    except (TypeError, ValueError):
        return None
    container.tag_governance = gateway
    return gateway


def _require_governance_scope():
    scope = _request_scope()
    if scope is None or scope.visibility != "group" or scope.session is None:
        return None, _scope_error("scope_required", 400)
    return scope, None


def _legacy_mutation_disabled(handler):
    """Legacy Tag rows remain auditable but cannot be mutated by WebUI routes."""
    @wraps(handler)
    async def reject(*args, **kwargs):
        return _scope_error("legacy_mutation_disabled", 410)
    return reject


def build_tag_list_payload(
    conn,
    *,
    limit: int = 50,
    offset: int = 0,
    tag_type: str = "",
    search: str = "",
    sort: str = "frequency",
) -> dict:
    """Build a filtered read projection whose counts only include live memories."""
    limit = max(1, min(100, int(limit)))
    offset = max(0, int(offset))
    tag_type = str(tag_type or "").strip()
    search = str(search or "").strip()
    sort = sort if sort in {"frequency", "recent"} else "frequency"

    where = []
    filters: list[object] = []
    if tag_type:
        where.append("t.tag_type = ?")
        filters.append(tag_type)
    if search:
        where.append("t.name LIKE ?")
        filters.append(f"%{search}%")
    where_sql = f" WHERE {' AND '.join(where)}" if where else ""

    total = int(conn.execute(
        f"SELECT COUNT(*) FROM tags t{where_sql}", filters
    ).fetchone()[0])
    order_sql = "frequency DESC, t.id DESC" if sort == "frequency" else "t.id DESC"
    rows = conn.execute(
        f"""SELECT t.id, t.name, t.tag_type,
                   COUNT(DISTINCT m.id) AS frequency,
                   t.confidence
            FROM tags t
            LEFT JOIN memory_tags mt ON mt.tag_id = t.id
            LEFT JOIN memories m ON m.id = mt.memory_id
            {where_sql}
            GROUP BY t.id, t.name, t.tag_type, t.confidence
            ORDER BY {order_sql}
            LIMIT ? OFFSET ?""",
        [*filters, limit, offset],
    ).fetchall()
    available_types = [
        str(row[0]) for row in conn.execute(
            """SELECT DISTINCT tag_type FROM tags
               WHERE tag_type IS NOT NULL AND TRIM(tag_type) <> ''
               ORDER BY tag_type"""
        ).fetchall()
    ]
    return {
        "items": [
            {
                "id": row[0],
                "name": row[1],
                "type": row[2],
                "frequency": int(row[3] or 0),
                "confidence": row[4],
            }
            for row in rows
        ],
        "total": total,
        "available_types": available_types,
        "legacy": True,
        "readonly": True,
        "capabilities": {
            "mutation": {
                "available": False,
                "reason_code": "legacy_mutation_disabled",
            }
        },
    }


@tags_bp.route("/", methods=["GET"])
@require_auth
async def list_tags():
    """分页查看 Tag 列表。"""
    c = get_container()
    try:
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError):
        return _scope_error("invalid_pagination", 400)
    payload = build_tag_list_payload(
        c.db.conn,
        limit=limit,
        offset=offset,
        tag_type=request.args.get("type", ""),
        search=request.args.get("search", ""),
        sort=request.args.get("sort", "frequency"),
    )
    return jsonify(payload)


def _resolve_ref(refs, value, *, kind: str, scope):
    if refs is None or not isinstance(value, str) or not value:
        raise TagGovernanceError("object_ref_required", "a signed ObjectRef is required")
    binding, state = refs.resolve_with_state(value, kind=kind, request_scope=scope)
    if binding is None or state != "ready":
        raise TagGovernanceError("object_ref_not_found", "the signed ObjectRef is not valid for this Scope")
    return binding


def _governance_error(exc: TagGovernanceError):
    status = 409 if exc.code in {
        "suggestion_revision_conflict",
        "suggestion_already_processed",
        "suggestion_expired",
        "preflight_token_invalid",
        "batch_validation_failed",
        "compensation_conflict",
        "suggestion_not_compensable",
        "suggestion_already_compensated",
        "governance_snapshot_missing",
    } else 400
    return jsonify(error_payload(exc.code, str(exc))), status


@tags_bp.route("/governance/catalog", methods=["GET"])
@require_auth
async def list_scoped_governance_tags():
    scope, failure = _require_governance_scope()
    if failure:
        return failure
    c = get_container()
    gateway = _governance_gateway(c)
    if gateway is None:
        return jsonify(error_payload("tag_governance_unavailable", "scoped Tag governance is unavailable")), 503
    try:
        items, total = gateway.list_scoped_tags(c.db.conn, scope=scope, search=request.args.get("search", ""), limit=int(request.args.get("limit", 100)), offset=int(request.args.get("offset", 0)))
        refs = _object_refs()
        for item in items:
            if refs is not None:
                ref = refs.issue(kind="tag", locator=item["id"], scope=scope, revision=item["revision"])
                item["ref"] = ref
                item["object_ref"] = {"ref": ref, "kind": "tag", "locator": item["id"], "scope_key": scope.session.id, "version": item["revision"]}
        return jsonify({**page_response(items, total=total, limit=int(request.args.get("limit", 100)), offset=int(request.args.get("offset", 0))), "scope": scope_to_dict(scope)})
    except (TagGovernanceError, ValueError, TypeError) as exc:
        return _governance_error(exc if isinstance(exc, TagGovernanceError) else TagGovernanceError("invalid_catalog_request", str(exc)))


@tags_bp.route("/governance/suggestions", methods=["GET"])
@require_auth
async def list_scoped_governance_suggestions():
    scope, failure = _require_governance_scope()
    if failure:
        return failure
    c = get_container()
    gateway = _governance_gateway(c)
    if gateway is None:
        return jsonify(error_payload("tag_governance_unavailable", "scoped Tag governance is unavailable")), 503
    try:
        limit = int(request.args.get("limit", 50))
        offset = int(request.args.get("offset", 0))
        items, total = gateway.list_suggestions(c.db.conn, scope=scope, status=request.args.get("status", "pending"), action=request.args.get("action", ""), limit=limit, offset=offset)
        refs = _object_refs()
        if refs is not None:
            for item in items:
                item["ref"] = refs.issue(kind="tag_audit_suggestion", locator=item["suggestion_id"], scope=scope, revision=item["revision"])
                item["object_ref"] = {"ref": item["ref"], "kind": "tag_audit_suggestion", "locator": item["suggestion_id"], "scope_key": scope.session.id, "version": item["revision"]}
                item["tag_refs"] = [refs.issue(kind="tag", locator=int(tag_id), scope=scope, revision=int((item.get("tag_details") or {}).get(int(tag_id), {}).get("revision", 1))) for tag_id in item["tag_ids"]]
        return jsonify({**page_response(items, total=total, limit=limit, offset=offset), "scope": scope_to_dict(scope)})
    except (TagGovernanceError, ValueError, TypeError) as exc:
        return _governance_error(exc if isinstance(exc, TagGovernanceError) else TagGovernanceError("invalid_suggestion_query", str(exc)))


@tags_bp.route("/governance/suggestions", methods=["POST"])
@require_auth
async def create_scoped_governance_suggestion():
    scope, failure = _require_governance_scope()
    if failure:
        return failure
    c = get_container()
    gateway = _governance_gateway(c)
    if gateway is None:
        return jsonify(error_payload("tag_governance_unavailable", "scoped Tag governance is unavailable")), 503
    body = await request.get_json(silent=True) or {}
    refs = _object_refs()
    try:
        tag_bindings = [_resolve_ref(refs, value, kind="tag", scope=scope) for value in body.get("tag_refs", ())]
        target_binding = _resolve_ref(refs, body.get("target_tag_ref"), kind="tag", scope=scope) if body.get("target_tag_ref") else None
        result = await gateway.create_suggestion(scope=scope, action=body.get("action"), tag_ids=[int(binding.locator) for binding in tag_bindings], target_tag_id=None if target_binding is None else int(target_binding.locator), target_name=body.get("target_name"), target_type=body.get("target_type"), aliases=body.get("aliases") if isinstance(body.get("aliases"), list) else (), reason=body.get("reason"), evidence=body.get("evidence") if isinstance(body.get("evidence"), dict) else {})
        suggestion_ref = refs.issue(kind="tag_audit_suggestion", locator=result.suggestion_id, scope=scope, revision=int(result.revision or 1)) if refs is not None else None
        item = {"suggestion_id": result.suggestion_id, "ref": suggestion_ref, "revision": result.revision, "status": result.status, "impact": result.impact}
        return jsonify(mutation_response(operation_kind="tags.governance.suggestion.create", operation_id=result.operation_id, status="succeeded", revision=result.revision, item=item, include_item=True)), 201
    except TagGovernanceError as exc:
        return _governance_error(exc)


@tags_bp.route("/governance/preview", methods=["POST"])
@require_auth
async def preview_scoped_governance_suggestion():
    scope, failure = _require_governance_scope()
    if failure:
        return failure
    c = get_container()
    gateway = _governance_gateway(c)
    body = await request.get_json(silent=True) or {}
    try:
        binding = _resolve_ref(_object_refs(), body.get("suggestion_ref"), kind="tag_audit_suggestion", scope=scope)
        result = gateway.preview(c.db.conn, scope=scope, suggestion_id=str(binding.locator), expected_revision=int(body.get("revision", binding.revision)))
        return jsonify({"ok": True, **result})
    except TagGovernanceError as exc:
        return _governance_error(exc)


@tags_bp.route("/governance/suggestions/resolve", methods=["POST"])
@require_auth
async def resolve_scoped_governance_suggestion():
    scope, failure = _require_governance_scope()
    if failure:
        return failure
    c = get_container()
    gateway = _governance_gateway(c)
    body = await request.get_json(silent=True) or {}
    try:
        binding = _resolve_ref(_object_refs(), body.get("suggestion_ref"), kind="tag_audit_suggestion", scope=scope)
        result = await gateway.resolve(scope=scope, suggestion_id=str(binding.locator), expected_revision=int(body.get("revision", binding.revision)), decision=body.get("decision"), preview_token=body.get("preflight_token"), reason=body.get("reason"))
        return jsonify(mutation_response(operation_kind="tags.governance.resolve", operation_id=result.operation_id, status="succeeded", revision=result.revision, item={"suggestion_id": result.suggestion_id, "status": result.status, "impact": result.impact}, include_item=True))
    except TagGovernanceError as exc:
        return _governance_error(exc)


@tags_bp.route("/governance/suggestions/compensate", methods=["POST"])
@require_auth
async def compensate_scoped_governance_suggestion():
    scope, failure = _require_governance_scope()
    if failure:
        return failure
    c = get_container()
    gateway = _governance_gateway(c)
    if gateway is None:
        return jsonify(error_payload("tag_governance_unavailable", "scoped Tag governance is unavailable")), 503
    body = await request.get_json(silent=True) or {}
    try:
        binding = _resolve_ref(_object_refs(), body.get("suggestion_ref"), kind="tag_audit_suggestion", scope=scope)
        result = await gateway.compensate(
            scope=scope,
            suggestion_id=str(binding.locator),
            expected_revision=int(body.get("revision", binding.revision)),
            reason=body.get("reason"),
        )
        status = "succeeded" if result.status == "compensated" else result.status
        return jsonify(mutation_response(
            operation_kind="tags.governance.compensate",
            operation_id=result.operation_id,
            status=status,
            revision=result.revision,
            item={"suggestion_id": result.suggestion_id, "status": result.status, "impact": result.impact},
            include_item=True,
        ))
    except TagGovernanceError as exc:
        return _governance_error(exc)


@tags_bp.route("/governance/suggestions/resolve-batch", methods=["POST"])
@require_auth
async def resolve_scoped_governance_batch():
    scope, failure = _require_governance_scope()
    if failure:
        return failure
    c = get_container()
    gateway = _governance_gateway(c)
    body = await request.get_json(silent=True) or {}
    try:
        refs = _object_refs()
        items = []
        for item in body.get("items", ()):
            binding = _resolve_ref(refs, item.get("suggestion_ref"), kind="tag_audit_suggestion", scope=scope)
            items.append({"suggestion_id": str(binding.locator), "revision": int(item.get("revision", binding.revision)), "preflight_token": item.get("preflight_token")})
        result = await gateway.resolve_batch(scope=scope, items=items, decision=body.get("decision"), reason=body.get("reason"))
        return jsonify(mutation_response(operation_kind="tags.governance.resolve_batch", operation_id=result.operation_id, status="succeeded", revision=None, item={"status": result.status, "impact": result.impact}, include_item=True))
    except TagGovernanceError as exc:
        return _governance_error(exc)


@tags_bp.route("/retype", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def retype_tag():
    """旧 Tag 裸 ID 类型编辑没有 scoped ObjectRef 命令，永久禁用。"""
    return _scope_error("legacy_mutation_disabled", 410)


@tags_bp.route("/rename", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def rename_tag():
    """旧 Tag 裸 ID 自由重命名没有 scoped ObjectRef 命令，永久禁用。"""
    return _scope_error("legacy_mutation_disabled", 410)


@tags_bp.route("/batch-delete", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def batch_delete_tags():
    """旧 Tag 裸 ID 批量物理删除永久禁用。"""
    return _scope_error("legacy_mutation_disabled", 410)


def build_tag_runtime_payload(container) -> dict:
    """Describe live Tag extraction/index capabilities without exposing paths or IDs."""
    extractor = getattr(container, "tag_extractor", None)
    index = getattr(container, "tag_index", None)
    provider_configured = bool(
        str((getattr(container, "plugin_config", None) or {}).get("tag_llm_provider_id", "")).strip()
    )
    index_available = index is not None
    try:
        index_count = max(0, int(getattr(index, "count", 0))) if index_available else 0
    except Exception:
        index_count = 0
    manifest = getattr(index, "current_manifest", None) if index_available else None
    manifest_invalid = bool(getattr(index, "manifest_error", None)) if index_available else False

    extractor_embedding = getattr(extractor, "embedding_service", None) if extractor else None
    extractor_index = getattr(extractor, "tag_index", None) if extractor else None
    if extractor is None:
        rag_mode = "unavailable"
        fallback_reason = "tag_extractor_unavailable"
    elif extractor_embedding is None:
        rag_mode = "static"
        fallback_reason = "embedding_unavailable"
    elif extractor_index is None:
        rag_mode = "static"
        fallback_reason = "tag_index_unavailable"
    elif index_count <= 0:
        rag_mode = "static"
        fallback_reason = "tag_index_empty"
    else:
        rag_mode = "semantic"
        fallback_reason = None

    if not index_available:
        index_health = "unavailable"
        index_reason = "tag_index_unavailable"
    elif manifest_invalid:
        index_health = "invalid"
        index_reason = "manifest_invalid"
    elif manifest is None:
        index_health = "legacy"
        index_reason = "manifest_unavailable"
    else:
        index_health = "ready"
        index_reason = None

    return {
        "capabilities": {
            "extract": {
                "available": extractor is not None,
                "reason_code": None if extractor is not None else (
                    "provider_not_configured" if not provider_configured else "tag_extractor_unavailable"
                ),
            },
            "mutation": {
                "available": False,
                "reason_code": "legacy_mutation_disabled",
            },
        },
        "index": {
            "available": index_available,
            "health": index_health,
            "reason_code": index_reason,
            "count": index_count,
            "generation": getattr(manifest, "generation", None),
            "db_watermark": getattr(manifest, "db_watermark", None),
        },
        "rag": {
            "mode": rag_mode,
            "semantic_available": rag_mode == "semantic",
            "fallback_reason": fallback_reason,
            "provider_configured": provider_configured,
            "reference_refresh_interval": getattr(extractor, "_reference_refresh_interval", None),
        },
    }


def build_tag_quality_payload(conn) -> dict:
    """构造 Tag 质量概览，所有 memory 口径都必须以真实 memories 表为准。

    历史清理/删除可能留下 memory_tags 孤儿行；这些行不能算作“已标记记忆”，
    否则前端待处理数量会被严重压低。
    """
    total_tags = conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
    total_mem = conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
    tagged_mem = conn.execute(
        """SELECT COUNT(DISTINCT m.id)
           FROM memories m
           JOIN memory_tags mt ON mt.memory_id = m.id"""
    ).fetchone()[0]
    untagged_mem = conn.execute(
        """SELECT COUNT(*) FROM memories m
           WHERE NOT EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id = m.id)"""
    ).fetchone()[0]
    extractable_untagged = conn.execute(
        """SELECT COUNT(*) FROM memories m
           WHERE NOT EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id = m.id)
           AND LENGTH(COALESCE(m.content, '')) >= 10"""
    ).fetchone()[0]
    skipped_short = conn.execute(
        """SELECT COUNT(*) FROM memories m
           WHERE NOT EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id = m.id)
           AND LENGTH(COALESCE(m.content, '')) < 10"""
    ).fetchone()[0]
    orphan_refs = conn.execute(
        """SELECT COUNT(DISTINCT mt.memory_id)
           FROM memory_tags mt
           LEFT JOIN memories m ON m.id = mt.memory_id
           WHERE m.id IS NULL"""
    ).fetchone()[0]
    coverage = (tagged_mem / total_mem) if total_mem else 0.0
    return {
        "total_tags": total_tags,
        "total_memories": total_mem,
        "tagged_memories": tagged_mem,
        "untagged_memories": untagged_mem,
        "extractable_untagged_memories": extractable_untagged,
        "skipped_short_untagged_memories": skipped_short,
        "orphan_memory_tag_refs": orphan_refs,
        "coverage": round(coverage, 4),
    }


@tags_bp.route("/quality", methods=["GET"])
@require_auth
async def tag_quality():
    """Tag 质量概览：覆盖率、索引代次、提取能力与 RAG 降级状态。"""
    c = get_container()
    payload = build_tag_quality_payload(c.db.conn)
    # 覆盖率口径以统一的覆盖率报告为准（正式 + 旧标签、只算活跃记忆、区分按规则不提取）
    try:
        report = await cached_tag_coverage(c)
        counts = report["counts"]
        payload.update({
            "total_memories": report["active_memories"],
            "tagged_memories": counts["tagged"],
            "untagged_memories": report["active_memories"] - counts["tagged"],
            "extractable_untagged_memories": counts["pending"] + counts["lost"] + counts["failed"],
            "skipped_short_untagged_memories": counts["too_short"],
            "coverage": report["coverage"],
            "effective_coverage": report["effective_coverage"],
        })
    except Exception as exc:  # 覆盖率报告失败时保留旧口径
        payload["coverage_report_error"] = str(exc)
    payload["runtime"] = build_tag_runtime_payload(c)
    return jsonify(payload)


# ---------------------------------------------------------------- 标签覆盖面板

_COVERAGE_CACHE_SECONDS = 60.0
_coverage_lock = asyncio.Lock()


def _tag_worker(c):
    getter = getattr(c, "tag_worker_getter", None)
    return getter() if callable(getter) else None


def _coverage_report_sync(db_path: str, worker_status: dict | None) -> dict:
    import sqlite3

    from ...services.tag_coverage import build_tag_coverage

    # 只读的独立连接：临时表不落在共享主连接上，也不挡写入
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=30)
    try:
        return build_tag_coverage(conn, worker_status=worker_status)
    finally:
        conn.close()


async def cached_tag_coverage(c, *, refresh: bool = False) -> dict:
    """统一的标签覆盖率报告（线上约 1 秒，缓存 60 秒）。首页、维护页、标签页共用。"""
    cached = getattr(c, "_tag_coverage_cache", None)
    now = time.time()
    if not refresh and cached and now - cached[0] < _COVERAGE_CACHE_SECONDS:
        return cached[1]
    async with _coverage_lock:
        cached = getattr(c, "_tag_coverage_cache", None)
        if not refresh and cached and time.time() - cached[0] < _COVERAGE_CACHE_SECONDS:
            return cached[1]
        worker = _tag_worker(c)
        status = worker.status() if worker is not None and hasattr(worker, "status") else {"running": False}
        report = await asyncio.to_thread(_coverage_report_sync, c.db.db_path, status)
        c._tag_coverage_cache = (time.time(), report)
        return report


@tags_bp.route("/coverage", methods=["GET"])
@require_auth
async def tag_coverage():
    """标签覆盖率：每类数量、按 Bot 拆分、样例、提取速度与积压预计耗时。"""
    c = get_container()
    try:
        report = await cached_tag_coverage(c, refresh=request.args.get("refresh") in {"1", "true"})
    except Exception as exc:
        return jsonify(error_payload("coverage_failed", f"覆盖率统计失败: {exc}", retryable=True)), 500
    return jsonify(report)


@tags_bp.route("/coverage/requeue", methods=["POST"])
@require_auth
async def requeue_tag_category():
    """把某一类（标签丢失 / 模型判空 / 失败）重新放回提取队列，并叫醒 TagWorker。"""
    from ...services.tag_coverage import REQUEUEABLE, requeue_ids

    c = get_container()
    worker = _tag_worker(c)
    if worker is None or not hasattr(worker, "requeue"):
        return jsonify(error_payload("tag_worker_unavailable", "TagWorker 没有运行（检查标签提取开关与 LLM Provider）")), 409
    body = await request.get_json(silent=True) or {}
    category = str(body.get("category") or "")
    if category not in REQUEUEABLE:
        return jsonify(error_payload("invalid_category", f"category 只能是 {', '.join(REQUEUEABLE)}")), 400
    limit = max(1, min(int(body.get("limit") or 5000), 20000))

    def _collect():
        import sqlite3

        conn = sqlite3.connect(f"file:{c.db.db_path}?mode=ro", uri=True, timeout=30)
        try:
            return requeue_ids(conn, category, limit=limit)
        finally:
            conn.close()

    ids = await asyncio.to_thread(_collect)
    requeued = await asyncio.to_thread(worker.requeue, ids) if ids else 0
    worker.wake()
    c._tag_coverage_cache = None
    return jsonify({"ok": True, "category": category, "selected": len(ids), "requeued": requeued})


@tags_bp.route("/worker/wake", methods=["POST"])
@require_auth
async def wake_tag_worker():
    c = get_container()
    worker = _tag_worker(c)
    if worker is None or not hasattr(worker, "wake"):
        return jsonify(error_payload("tag_worker_unavailable", "TagWorker 没有运行")), 409
    worker.wake()
    return jsonify({"ok": True, "worker": worker.status()})


@tags_bp.route("/audit/trigger", methods=["GET", "POST"])
@require_auth
async def trigger_audit():
    """Queue Tag audit as a durable job and return its pollable job id."""
    c = get_container()
    strategy = request.args.get("strategy", "mixed")
    if strategy not in {"mixed", "low_quality", "high_freq"}:
        return jsonify({"ok": False, "error": "invalid_audit_strategy"}), 400
    total_count = max(10, min(2000, int(request.args.get("total_count", 500))))
    provider_id = str((c.plugin_config or {}).get("tag_llm_provider_id", ""))
    if not provider_id:
        return jsonify({"ok": False, "error": "tag_audit_provider_not_configured"}), 409
    jobs = getattr(c, "durable_jobs", None)
    if jobs is None:
        return jsonify({"ok": False, "error": "durable_jobs_unavailable"}), 503

    schedule_slot = str(
        request.args.get("schedule_slot") or f"manual-{int(time.time() // 60)}"
    )
    job_request = await jobs.create_request(
        idempotency_key=f"tag-audit:{strategy}:{total_count}:{schedule_slot}",
        kind="maintenance.tag_audit.run",
        scope={"kind": "system_maintenance"},
        payload={
            "strategy": strategy,
            "total_count": total_count,
            "provider_id": provider_id,
            "requested_by": "webui",
        },
    )
    run = await jobs.schedule_run(
        request_id=job_request.request_id,
        schedule_slot=schedule_slot,
        cursor_generation=0,
        cursor={"phase": "queued", "processed": 0},
    )
    return jsonify({
        "ok": True,
        "accepted": True,
        "request_id": job_request.request_id,
        "job_id": run.run_id,
        "status": run.status,
    }), 202


@tags_bp.route("/audit/suggestions", methods=["GET"])
@require_auth
async def get_audit_suggestions():
    """获取审计建议列表。"""
    c = get_container()
    from ...services.tag_auditor import TagAuditor
    status = request.args.get("status", "pending")
    limit = int(request.args.get("limit", 50))
    offset = int(request.args.get("offset", 0))
    action = request.args.get("action")

    auditor = TagAuditor(db=c.db)
    suggestions = auditor.get_suggestions(status=status, limit=limit, offset=offset, action=action)
    counts = auditor.get_suggestion_counts()
    return jsonify({"suggestions": suggestions, "counts": counts})


async def _resolve_audit_suggestion(c, suggestion_id: int, decision: str) -> dict:
    """兼容旧调用方的 fail-closed helper；legacy audit 只读，不接受裸 ID resolve。"""
    return {"error": "legacy_mutation_disabled"}


@tags_bp.route("/audit/resolve", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def resolve_audit_suggestion():
    """Legacy Tag audit 只读；裸 suggestion ID resolve 永久禁用。"""
    return _scope_error("legacy_mutation_disabled", 410)


@tags_bp.route("/audit/resolve-batch", methods=["POST"])
@require_auth
@_legacy_mutation_disabled
async def resolve_audit_batch():
    """Legacy Tag audit 只读；批量裸 ID resolve 永久禁用。"""
    return _scope_error("legacy_mutation_disabled", 410)


def _clamp_batch_size(value: object, *, default: int = 20, maximum: int = 50) -> int:
    try:
        size = int(value) if value is not None else default
    except (TypeError, ValueError):
        size = default
    return max(1, min(maximum, size))


def _untagged_memory_count(conn, min_length: int = 10, *, strict_scoped: bool = False) -> int:
    if not strict_scoped:
        return int(conn.execute(
            """SELECT COUNT(*) FROM memories m
               WHERE NOT EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id=m.id)
                 AND LENGTH(COALESCE(m.content, '')) >= ?""",
            (min_length,),
        ).fetchone()[0])
    return int(conn.execute(
        """SELECT COUNT(*) FROM memories m
           WHERE NOT EXISTS (
               SELECT 1 FROM scoped_memory_tags smt WHERE smt.memory_id = m.id
           )
           AND EXISTS (
               SELECT 1 FROM domain_outbox o
               WHERE o.aggregate_kind='memory'
                 AND o.aggregate_id=CAST(m.id AS TEXT)
                 AND o.event_type='memory.created'
           )
           AND m.resolution_state='resolved'
           AND COALESCE(m.quarantine, 0)=0
           AND LENGTH(COALESCE(m.content, '')) >= ?""",
        (min_length,),
    ).fetchone()[0])


def _load_untagged_memory_batch(
    conn, limit: int, min_length: int = 10, *, strict_scoped: bool = False
) -> list:
    if not strict_scoped:
        return [
            (*row, None)
            for row in conn.execute(
                """SELECT m.id, m.content, m.sender_name FROM memories m
                   WHERE NOT EXISTS (
                       SELECT 1 FROM memory_tags mt WHERE mt.memory_id=m.id
                   )
                     AND LENGTH(COALESCE(m.content, '')) >= ?
                   ORDER BY m.id DESC LIMIT ?""",
                (min_length, limit),
            ).fetchall()
        ]
    return conn.execute(
        """SELECT m.id, m.content, m.sender_name, o.payload_json
           FROM memories m
           JOIN domain_outbox o
             ON o.aggregate_kind='memory'
            AND o.aggregate_id=CAST(m.id AS TEXT)
            AND o.event_type='memory.created'
           WHERE NOT EXISTS (
               SELECT 1 FROM scoped_memory_tags smt WHERE smt.memory_id = m.id
           )
           AND LENGTH(COALESCE(m.content, '')) >= ?
           AND m.resolution_state='resolved'
           AND COALESCE(m.quarantine, 0)=0
           ORDER BY m.id DESC LIMIT ?""",
        (min_length, limit),
    ).fetchall()


async def iter_batch_extract_events(
    c,
    batch_size: int,
    *,
    tag_write_policy: str = "missing_only",
    skip_short_min_length: int = 10,
    runtime_budget_seconds: float = 45.0,
    batch_timeout_seconds: float = 40.0,
):
    """生成单轮 Tag 提取进度事件。

    注意：这是 WebUI 的一次 HTTP/SSE 请求，不是后台长任务。运行时存在约 60s 的
    chunked 连接上限，所以这里每次只处理一批，并在安全时间窗内正常结束；前端可
    根据 remaining/partial 继续发起下一轮，避免 ERR_INCOMPLETE_CHUNKED_ENCODING。
    """
    if not getattr(c, "tag_extractor", None):
        yield {"error": "Tag extractor not configured", "done": True}
        return
    if tag_write_policy != "missing_only":
        yield {"error": "全库补提取只允许 tag_write_policy=missing_only；append/replace 必须在选中范围内执行。", "done": True}
        return

    conn = c.db.conn
    strict_scoped = getattr(c, "write_gateway", None) is not None
    total_remaining_before = _untagged_memory_count(
        conn, skip_short_min_length, strict_scoped=strict_scoped
    )
    if total_remaining_before == 0:
        yield {
            "progress": 1.0,
            "processed": 0,
            "total": 0,
            "tagged": 0,
            "errors": 0,
            "remaining": 0,
            "done": True,
            "message": "所有可提取记忆均已有标签",
        }
        return

    rows = _load_untagged_memory_batch(
        conn, batch_size, skip_short_min_length, strict_scoped=strict_scoped
    )
    selected = len(rows)
    yield {
        "progress": 0,
        "processed": 0,
        "total": total_remaining_before,
        "selected": selected,
        "tagged": 0,
        "errors": 0,
        "remaining": total_remaining_before,
        "message": f"本轮将处理 {selected} 条，当前剩余 {total_remaining_before} 条未标注记忆",
    }

    processed = tagged = errors = 0
    started = time.monotonic()
    messages = []
    for mem_id, content, sender_name, payload_json in rows:
        try:
            scope = json.loads(payload_json).get("scope")
        except (TypeError, ValueError, json.JSONDecodeError):
            scope = None
        if strict_scoped and not isinstance(scope, dict):
            errors += 1
            continue
        message = {
            "id": mem_id,
            "content": (content or "")[:800],
            "sender": sender_name or "",
        }
        if isinstance(scope, dict):
            message["scope"] = scope
        messages.append(message)

    if messages:
        try:
            remaining_budget = max(1.0, runtime_budget_seconds - (time.monotonic() - started))
            timeout = max(1.0, min(batch_timeout_seconds, remaining_budget))
            result = await asyncio.wait_for(
                tag_memory_batch(
                    c.db,
                    getattr(c, "embedding_service", None),
                    c.tag_extractor,
                    messages,
                    tag_batch_size=batch_size,
                    tag_write_policy=tag_write_policy,
                    skip_short_min_length=skip_short_min_length,
                    write_gateway=getattr(c, "write_gateway", None),
                ),
                timeout=timeout,
            )
            processed = int(result.get("processed", 0))
            tagged = int(result.get("tagged", 0))
            errors = int(result.get("errors", 0))
        except asyncio.TimeoutError:
            yield {
                "error": f"本轮 Tag LLM 提取超过 {int(batch_timeout_seconds)} 秒，已安全停止。请调小 tag_batch_size 后重试。",
                "progress": round(processed / total_remaining_before, 3),
                "processed": processed,
                "total": total_remaining_before,
                "tagged": tagged,
                "errors": selected,
                "remaining": total_remaining_before,
                "done": True,
            }
            return
        except Exception as exc:
            yield {
                "error": f"Tag LLM 批量提取失败: {type(exc).__name__}: {exc}",
                "progress": round(processed / total_remaining_before, 3),
                "processed": processed,
                "total": total_remaining_before,
                "tagged": tagged,
                "errors": selected,
                "remaining": total_remaining_before,
                "done": True,
            }
            return

    remaining_after = _untagged_memory_count(
        conn, skip_short_min_length, strict_scoped=strict_scoped
    )
    partial = remaining_after > 0
    yield {
        "progress": round((total_remaining_before - remaining_after) / total_remaining_before, 3) if total_remaining_before else 1.0,
        "processed": processed,
        "total": total_remaining_before,
        "selected": selected,
        "tagged": tagged,
        "errors": errors,
        "remaining": remaining_after,
        "partial": partial,
        "done": True,
        "message": (
            f"本轮完成：处理 {processed} 条，写入标签 {tagged} 条，剩余 {remaining_after} 条。"
            if partial else
            f"本轮完成：处理 {processed} 条，写入标签 {tagged} 条，未标注队列已清空。"
        ),
    }


@tags_bp.route("/batch-extract", methods=["POST"])
@require_auth
async def batch_extract_tags():
    """Queue one bounded Tag backfill batch as a durable job."""
    c = get_container()
    try:
        tag_options = normalize_tag_execution_options(request.args)
    except ValueError as exc:
        return jsonify({"ok": False, "error": str(exc)}), 400
    if tag_options["tag_write_policy"] != "missing_only":
        return jsonify({
            "ok": False,
            "error": "durable tag backfill only supports missing_only",
        }), 400
    jobs = getattr(c, "durable_jobs", None)
    if jobs is None:
        return jsonify({"ok": False, "error": "durable_jobs_unavailable"}), 503
    schedule_slot = str(
        request.args.get("schedule_slot") or f"manual-{int(time.time() // 60)}"
    )
    request_record = await jobs.create_request(
        idempotency_key=f"tag-backfill:{schedule_slot}:{tag_options['tag_batch_size']}",
        kind="maintenance.tag_backfill.run",
        scope={"kind": "system_maintenance"},
        payload={
            "batch_size": tag_options["tag_batch_size"],
            "skip_short_min_length": tag_options["skip_short_min_length"],
            "requested_by": "webui",
        },
    )
    run = await jobs.schedule_run(
        request_id=request_record.request_id,
        schedule_slot=schedule_slot,
        cursor_generation=0,
        cursor={"phase": "queued", "after_id": 0},
    )
    return jsonify({
        "ok": True,
        "accepted": True,
        "request_id": request_record.request_id,
        "job_id": run.run_id,
        "status": run.status,
    }), 202


_import_lock = asyncio.Lock()
