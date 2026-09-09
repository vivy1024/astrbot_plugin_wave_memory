"""KG legacy 收敛层：显式 RuntimeScope 下的规范只读投影。"""

from __future__ import annotations

import asyncio
import copy
import hmac
import json
import sqlite3
import time
from collections import OrderedDict, deque
from urllib.parse import unquote

from quart import Blueprint, jsonify, request

from ..container import get_container
from ..graph_projection import SUPPORTED_LAYERS, build_graph_projection, scoped_layer_counts
from ..middleware.auth import require_auth
from .explore import _canonical_memory_store, _table_columns

try:
    from ...domain.scope import RuntimeScope, ScopeCodec, ScopeValidationError, SessionRef
    from ...engine.db.scoped_knowledge_repo import ScopedKnowledgeScopeError
    from ...services.scoped_knowledge_mutations import (
        ScopedKnowledgeIdentityConflict,
        ScopedKnowledgeIdempotencyConflict,
        ScopedKnowledgeMutationGateway,
        ScopedKnowledgeMutationTarget,
        ScopedKnowledgeNotFound,
        ScopedKnowledgeRevisionConflict,
    )
except ImportError:  # pragma: no cover - plugin root may be imported directly
    from domain.scope import RuntimeScope, ScopeCodec, ScopeValidationError, SessionRef
    from engine.db.scoped_knowledge_repo import ScopedKnowledgeScopeError
    from services.scoped_knowledge_mutations import (
        ScopedKnowledgeIdentityConflict,
        ScopedKnowledgeIdempotencyConflict,
        ScopedKnowledgeMutationGateway,
        ScopedKnowledgeMutationTarget,
        ScopedKnowledgeNotFound,
        ScopedKnowledgeRevisionConflict,
    )


kg_bp = Blueprint("kg", __name__, url_prefix="/api/kg")
_overview_cache: OrderedDict[tuple, tuple[float, dict]] = OrderedDict()
_GRAPH_CACHE_TTL_SECONDS = 15.0
_GRAPH_CACHE_MAX_ENTRIES = 32
_STRONG_SECRET_MIN_LENGTH = 32
_WEAK_PAYMENT_SECRETS = frozenset({"", "wavemoney", "password", "secret", "changeme"})


def _scope_error(code: str, status: int):
    return jsonify({"error": {"code": code}}), status


def _scope_failure(exc: Exception):
    code = getattr(exc, "reason_code", None) or getattr(exc, "code", None) or "invalid_scope"
    status = 400 if code in {"scope_required", "pagination_required", "invalid_pagination"} else 422
    return _scope_error(str(code), status)


def _read_only_gone():
    return _scope_error("legacy_mutation_disabled", 410)


def _table_exists(conn, table: str) -> bool:
    if conn is None:
        return False
    try:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone() is not None
    except Exception:
        return False


def _json_loads_safe(value, default=None):
    fallback = {} if default is None else default
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value not in (None, "") else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _conn():
    return getattr(getattr(get_container(), "db", None), "conn", None)


def _scoped_repo(container):
    repo = getattr(getattr(container, "db", None), "scoped_knowledge", None)
    if repo is None:
        raise ScopedKnowledgeScopeError("scoped_repository_unavailable")
    return repo


def _object_ref_registry():
    try:
        from quart import current_app

        registry = current_app.extensions.get("wave_api_contract", {}).get("object_refs")
        if registry is not None:
            return registry
    except (ImportError, RuntimeError, AttributeError):
        pass
    return getattr(get_container(), "object_refs", None)


def _scoped_mutation_gateway(container):
    configured = getattr(container, "scoped_knowledge_mutations", None)
    if configured is not None:
        return configured
    write_gateway = getattr(container, "write_gateway", None)
    if write_gateway is None:
        return None
    try:
        return ScopedKnowledgeMutationGateway(write_gateway)
    except (TypeError, ValueError):
        return None


def _scope_query(scope: RuntimeScope) -> dict:
    query = {"bot_id": scope.bot_id, "visibility": scope.visibility}
    if scope.session is not None:
        query["session_id"] = scope.session.id
    return query


def _object_descriptor(kind: str, locator: int, revision: int, scope: RuntimeScope) -> dict | None:
    registry = _object_ref_registry()
    if registry is None:
        return None
    ref = registry.issue(kind=kind, locator=int(locator), scope=scope, revision=int(revision))
    return {
        "ref": ref,
        "kind": kind,
        "locator": int(locator),
        "scope_key": scope.session.id if scope.session else scope.bot_id,
        "scope_query": _scope_query(scope),
        "version": int(revision),
    }


def _mutation_capabilities(kind: str, *, available: bool) -> dict:
    resource = "facts" if kind == "fact" else "tag-relations"
    reason = None if available else "scoped_knowledge_mutation_gateway_unavailable"
    return {
        "update": {
            "available": available,
            "reason_code": reason,
            "command": f"/api/kg/commands/{resource}/update",
        },
        "delete": {
            "available": available,
            "reason_code": reason,
            "command": f"/api/kg/commands/{resource}/delete",
        },
    }


def _decorate_mutable_item(item: dict, *, kind: str, scope: RuntimeScope) -> dict:
    result = copy.deepcopy(item)
    id_field = "id"
    locator = result.get(id_field)
    revision = result.get("revision")
    if locator is None or isinstance(revision, bool) or not isinstance(revision, int):
        return result
    descriptor = _object_descriptor(kind, int(locator), int(revision), scope)
    available = descriptor is not None and _scoped_mutation_gateway(get_container()) is not None
    result["object_ref"] = descriptor
    result["capabilities"] = _mutation_capabilities(kind, available=available)
    result["editable"] = available
    result["read_only"] = not available
    return result


def _canonical_graph_revisions(
    scope: RuntimeScope, *, min_confidence: float
) -> dict[str, dict[int, int]]:
    conn = _conn()
    params = _scope_params(scope)
    result: dict[str, dict[int, int]] = {"fact": {}, "tag_relation": {}}
    fact_columns = _table_columns(conn, "scoped_facts")
    if {"id", "bot_id", "session_id", "visibility", "revision"} <= fact_columns:
        rows = conn.execute(
            """SELECT id, revision FROM scoped_facts
                 WHERE bot_id=? AND session_id=? AND visibility=? AND confidence>=?
                   AND status NOT IN ('deleted','superseded')
                 ORDER BY updated_at DESC, id DESC LIMIT 2000""",
            (*params, min_confidence),
        ).fetchall()
        result["fact"] = {int(row[0]): int(row[1]) for row in rows}
    relation_columns = _table_columns(conn, "scoped_tag_relations")
    if {"id", "bot_id", "session_id", "visibility", "revision"} <= relation_columns:
        rows = conn.execute(
            """SELECT id, revision FROM scoped_tag_relations
                 WHERE bot_id=? AND session_id=? AND visibility=? AND confidence>=?
                   AND status NOT IN ('deleted','superseded')
                 ORDER BY updated_at DESC, id DESC LIMIT 2000""",
            (*params, min_confidence),
        ).fetchall()
        result["tag_relation"] = {int(row[0]): int(row[1]) for row in rows}
    return result


def _graph_projection_matches(
    payload: dict, scope: RuntimeScope, *, min_confidence: float
) -> bool:
    actual = _canonical_graph_revisions(scope, min_confidence=min_confidence)
    projected = {"fact": {}, "tag_relation": {}}
    for edge in payload.get("edges", []):
        kind = edge.get("kind")
        if kind == "fact" and edge.get("fact_id") is not None:
            projected["fact"][int(edge["fact_id"])] = int(edge.get("revision") or 0)
        elif kind == "tag_relation" and edge.get("relation_id") is not None:
            projected["tag_relation"][int(edge["relation_id"])] = int(edge.get("revision") or 0)
    return projected == actual


def _decorate_graph_mutations(
    payload: dict, scope: RuntimeScope, *, min_confidence: float
) -> dict:
    result = copy.deepcopy(payload)
    actual = _canonical_graph_revisions(scope, min_confidence=min_confidence)
    gateway_available = _scoped_mutation_gateway(get_container()) is not None
    decorated_edges = []
    for edge in result.get("edges", []):
        kind = edge.get("kind")
        if kind == "fact":
            locator = edge.get("fact_id")
            ref_kind = "fact"
        elif kind == "tag_relation":
            locator = edge.get("relation_id")
            ref_kind = "tag_relation"
        else:
            decorated_edges.append(edge)
            continue
        revision = edge.get("revision")
        if locator is None or isinstance(revision, bool) or not isinstance(revision, int):
            continue
        if actual[ref_kind].get(int(locator)) != int(revision):
            continue
        descriptor = _object_descriptor(ref_kind, int(locator), int(revision), scope)
        edge["object_ref"] = descriptor
        available = descriptor is not None and gateway_available
        edge["capabilities"] = _mutation_capabilities(ref_kind, available=available)
        edge["editable"] = available
        edge["read_only"] = not available
        decorated_edges.append(edge)
    result["edges"] = decorated_edges
    result["total"] = len(decorated_edges)
    return result


def _resolve_command_target(body: dict, *, kind: str, scope: RuntimeScope):
    descriptor = body.get("object_ref") or body.get("ref")
    ref = descriptor.get("ref") if isinstance(descriptor, dict) else descriptor
    if not isinstance(ref, str) or not ref:
        raise ScopedKnowledgeScopeError("object_ref_required")
    try:
        revision = int(body.get("revision"))
    except (TypeError, ValueError) as exc:
        raise ScopedKnowledgeScopeError("object_ref_revision_required") from exc
    if revision <= 0:
        raise ScopedKnowledgeScopeError("object_ref_revision_required")
    registry = _object_ref_registry()
    if registry is None:
        return None, (_scope_error("not_found", 404))
    binding, _state = registry.resolve_with_state(
        ref,
        kind=kind,
        request_scope=scope,
    )
    if binding is None:
        return None, _scope_error("not_found", 404)
    if int(binding.revision) != revision:
        return None, _scope_error("stale_revision", 409)
    try:
        locator = int(binding.locator)
    except (TypeError, ValueError):
        return None, _scope_error("not_found", 404)
    return ScopedKnowledgeMutationTarget(kind, locator, revision), None


def _group_scope_from_query() -> RuntimeScope:
    required = ("bot_id", "session_id", "visibility")
    values = {field: request.args.get(field) for field in required}
    if any(value is None or str(value).strip() == "" for value in values.values()):
        raise ScopedKnowledgeScopeError("scope_required")
    if values["visibility"] != "group":
        raise ScopedKnowledgeScopeError("derived_scope_visibility_unsupported")
    try:
        platform_id, kind, conversation_id = str(values["session_id"]).split(":", 2)
    except ValueError as exc:
        raise ScopeValidationError("invalid_session_id", "session_id must be canonical") from exc
    return RuntimeScope(
        str(values["bot_id"]), "group",
        SessionRef(str(values["session_id"]), platform_id, kind, conversation_id),
    )


def _scope_params(scope: RuntimeScope) -> tuple[str, str, str]:
    assert scope.session is not None
    return scope.bot_id, scope.session.id, scope.visibility


def _page_from_query() -> tuple[int, int]:
    if request.args.get("page") is None or request.args.get("page_size") is None:
        raise ScopedKnowledgeScopeError("pagination_required")
    try:
        page = int(request.args["page"])
        page_size = int(request.args["page_size"])
    except (TypeError, ValueError) as exc:
        raise ScopedKnowledgeScopeError("invalid_pagination") from exc
    if page < 1 or page_size < 1 or page_size > 100:
        raise ScopedKnowledgeScopeError("invalid_pagination")
    return page, page_size


def _scoped_page(items: list[dict], *, page: int, page_size: int) -> dict:
    start = (page - 1) * page_size
    return {
        "items": items[start:start + page_size],
        "page": {
            "number": page, "page_size": page_size, "total": len(items),
            "total_status": "exact", "has_next": start + page_size < len(items),
        },
    }


def _list_scoped_relations_from_conn(conn, scope: RuntimeScope) -> list[dict]:
    required = {"bot_id", "session_id", "visibility", "source_tag_id", "target_tag_id"}
    if not required <= _table_columns(conn, "scoped_tag_relations"):
        return []
    if not {"id", "bot_id", "session_id", "visibility", "name"} <= _table_columns(conn, "scoped_tags"):
        return []
    columns = _table_columns(conn, "scoped_tag_relations")
    status_sql = "r.status" if "status" in columns else "'active'"
    valid_until_sql = "r.valid_until" if "valid_until" in columns else "NULL"
    revision_sql = "r.revision" if "revision" in columns else "1"
    status_filter = "AND r.status NOT IN ('deleted','superseded')" if "status" in columns else ""
    rows = conn.execute(
        f"""SELECT r.id, r.source_tag_id, r.target_tag_id, r.relation_type, r.weight, r.confidence,
                  r.metadata, r.created_at, r.updated_at, source.name, target.name,
                  source.tag_type, target.tag_type, {status_sql}, {valid_until_sql}, {revision_sql}
             FROM scoped_tag_relations r
             JOIN scoped_tags source ON source.id=r.source_tag_id
              AND source.bot_id=r.bot_id AND source.session_id=r.session_id AND source.visibility=r.visibility
             JOIN scoped_tags target ON target.id=r.target_tag_id
              AND target.bot_id=r.bot_id AND target.session_id=r.session_id AND target.visibility=r.visibility
            WHERE r.bot_id=? AND r.session_id=? AND r.visibility=? {status_filter}
            ORDER BY r.updated_at DESC, r.id DESC""",
        _scope_params(scope),
    ).fetchall()
    return [
        {
            "id": row[0], "source_tag_id": row[1], "target_tag_id": row[2],
            "relation_type": row[3], "weight": row[4], "confidence": row[5],
            "metadata": _json_loads_safe(row[6], {}), "created_at": row[7],
            "updated_at": row[8], "source": row[9], "target": row[10],
            "source_type": row[11] or "topic", "target_type": row[12] or "topic",
            "status": row[13], "valid_until": row[14], "revision": int(row[15]),
            "read_only": True,
        }
        for row in rows
    ]


def _list_scoped_relations(scope: RuntimeScope) -> list[dict]:
    conn = _conn()
    if conn is not None:
        return _list_scoped_relations_from_conn(conn, scope)
    repo = _scoped_repo(get_container())
    cm = getattr(repo, "cm", None)
    if cm is None:
        raise ScopedKnowledgeScopeError("scoped_repository_unavailable")
    return _list_scoped_relations_from_conn(cm, scope)


@kg_bp.route("/facts", methods=["GET"])
@require_auth
async def list_scoped_facts():
    try:
        scope = _group_scope_from_query()
        page, page_size = _page_from_query()
        rows = _scoped_repo(get_container()).list_scoped_facts(
            scope, subject=request.args.get("subject"), limit=10000,
        )
        payload = _scoped_page(
            [_decorate_mutable_item(dict(row, read_only=True), kind="fact", scope=scope) for row in rows],
            page=page,
            page_size=page_size,
        )
        payload.update({"scope": ScopeCodec.to_dict(scope), "read_only": True})
        return jsonify(payload)
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@kg_bp.route("/facts", methods=["POST"])
@require_auth
async def create_scoped_fact():
    """KG 页面仅投影事实；创建必须迁移到正式证据锚定命令。"""
    return _read_only_gone()


@kg_bp.route("/tag-relations", methods=["GET"])
@require_auth
async def list_scoped_relations():
    try:
        scope = _group_scope_from_query()
        page, page_size = _page_from_query()
        payload = _scoped_page(
            [
                _decorate_mutable_item(row, kind="tag_relation", scope=scope)
                for row in _list_scoped_relations(scope)
            ],
            page=page,
            page_size=page_size,
        )
        payload.update({"scope": ScopeCodec.to_dict(scope), "read_only": True})
        return jsonify(payload)
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@kg_bp.route("/tag-relations", methods=["POST"])
@require_auth
async def create_scoped_relation():
    return _read_only_gone()


def _command_fields(body: dict, allowed: tuple[str, ...]) -> dict:
    fields = body.get("patch")
    if fields is None:
        fields = body.get("fields")
    if fields is None:
        fields = {name: body[name] for name in allowed if name in body}
    if not isinstance(fields, dict):
        raise ValueError("patch must be an object")
    if set(fields) - set(allowed):
        raise ValueError("unsupported mutation fields")
    return fields


def _command_result_payload(result, scope: RuntimeScope) -> dict:
    descriptor = None
    if result.status not in {"deleted", "superseded"}:
        descriptor = _object_descriptor(
            result.kind, result.locator, result.revision, scope
        )
    return {
        "ok": True,
        "operation": {"id": result.operation_id, "status": "committed"},
        "revision": result.revision,
        "status": result.status,
        "object_ref": descriptor,
        "previous_object_replaced": result.previous_locator is not None,
    }


async def _execute_scoped_command(*, kind: str, action: str):
    try:
        body = await request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return _scope_error("invalid_request", 400)
        scope = _group_scope_from_query()
        target, error = _resolve_command_target(body, kind=kind, scope=scope)
        if error is not None:
            return error
        gateway = _scoped_mutation_gateway(get_container())
        if gateway is None:
            return _scope_error("scoped_knowledge_mutation_gateway_unavailable", 503)
        idempotency_key = body.get("idempotency_key")
        if idempotency_key is not None and (
            not isinstance(idempotency_key, str) or not idempotency_key.strip()
        ):
            return _scope_error("invalid_idempotency_key", 400)
        assert target is not None
        if kind == "fact" and action == "update":
            fields = _command_fields(body, (
                "subject", "predicate", "object", "confidence",
            ))
            result = await gateway.update_fact(
                scope=scope, target=target, fields=fields,
                idempotency_key=idempotency_key,
            )
        elif kind == "fact":
            result = await gateway.delete_fact(
                scope=scope, target=target, idempotency_key=idempotency_key,
            )
        elif kind == "tag" and action == "update":
            fields = _command_fields(body, (
                "name", "tag_type", "description", "aliases",
            ))
            result = await gateway.update_tag(
                scope=scope, target=target, fields=fields,
                idempotency_key=idempotency_key,
            )
        elif action == "update":
            fields = _command_fields(body, (
                "relation_type", "weight", "confidence",
            ))
            result = await gateway.update_tag_relation(
                scope=scope, target=target, fields=fields,
                idempotency_key=idempotency_key,
            )
        else:
            result = await gateway.delete_tag_relation(
                scope=scope, target=target, idempotency_key=idempotency_key,
            )
        clear_kg_cache()
        return jsonify(_command_result_payload(result, scope))
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        if isinstance(exc, (
            ScopedKnowledgeIdentityConflict,
            ScopedKnowledgeIdempotencyConflict,
            ScopedKnowledgeRevisionConflict,
        )):
            code = "stale_revision" if isinstance(exc, ScopedKnowledgeRevisionConflict) else "mutation_conflict"
            return _scope_error(code, 409)
        if isinstance(exc, ScopedKnowledgeNotFound):
            return _scope_error("not_found", 404)
        return _scope_error("invalid_request", 400)
    except (RuntimeError, sqlite3.Error):
        return _scope_error("scoped_knowledge_mutation_gateway_unavailable", 503)


@kg_bp.route("/commands/tags/update", methods=["POST"])
@require_auth
async def command_update_tag():
    return await _execute_scoped_command(kind="tag", action="update")


@kg_bp.route("/commands/facts/update", methods=["POST"])
@require_auth
async def command_update_fact():
    return await _execute_scoped_command(kind="fact", action="update")


@kg_bp.route("/commands/facts/delete", methods=["POST"])
@require_auth
async def command_delete_fact():
    return await _execute_scoped_command(kind="fact", action="delete")


@kg_bp.route("/commands/tag-relations/update", methods=["POST"])
@require_auth
async def command_update_tag_relation():
    return await _execute_scoped_command(kind="tag_relation", action="update")


@kg_bp.route("/commands/tag-relations/delete", methods=["POST"])
@require_auth
async def command_delete_tag_relation():
    return await _execute_scoped_command(kind="tag_relation", action="delete")


@kg_bp.route("/legacy/audit/facts", methods=["GET"])
@require_auth
async def legacy_audit_facts():
    return _read_only_gone()


@kg_bp.route("/legacy/audit/relations", methods=["GET"])
@require_auth
async def legacy_audit_relations():
    return _read_only_gone()


def _parse_layers(layers_raw) -> tuple[str, ...]:
    if isinstance(layers_raw, str):
        values = layers_raw.split(",")
    else:
        values = list(layers_raw or ())
    layers = tuple(dict.fromkeys(str(value).strip() for value in values if str(value).strip())) or ("facts",)
    unsupported = sorted(set(layers) - set(SUPPORTED_LAYERS))
    if unsupported:
        raise ValueError("unsupported_layers:" + ",".join(unsupported))
    return layers


def _bounded_int(value, *, default: int, minimum: int, maximum: int) -> int:
    parsed = default if value in (None, "") else int(value)
    if parsed < minimum or parsed > maximum:
        raise ValueError("graph_parameter_out_of_range")
    return parsed


def _bounded_float(value, *, default: float, minimum: float, maximum: float) -> float:
    parsed = default if value in (None, "") else float(value)
    if not minimum <= parsed <= maximum:
        raise ValueError("graph_parameter_out_of_range")
    return parsed


def _full_edges(scope: RuntimeScope, *, min_confidence: float = 0.0) -> list[dict]:
    """兼容旧调用方的 facts-only edge 投影。"""
    return build_full_graph_data(
        "facts", use_cache=False, min_confidence=min_confidence, scope=scope,
    )["edges"]


def _cache_get(key: tuple) -> dict | None:
    cached = _overview_cache.get(key)
    if cached is None:
        return None
    created_at, payload = cached
    if time.monotonic() - created_at > _GRAPH_CACHE_TTL_SECONDS:
        _overview_cache.pop(key, None)
        return None
    _overview_cache.move_to_end(key)
    return copy.deepcopy(payload)


def _cache_put(key: tuple, payload: dict) -> None:
    _overview_cache[key] = (time.monotonic(), copy.deepcopy(payload))
    _overview_cache.move_to_end(key)
    while len(_overview_cache) > _GRAPH_CACHE_MAX_ENTRIES:
        _overview_cache.popitem(last=False)


def build_full_graph_data(
    layers_raw: str = "facts", *, use_cache: bool = True,
    min_confidence: float = 0.0, scope: RuntimeScope | None = None,
    memory_limit: int = 150, similarity_k: int = 3,
    similarity_threshold: float = 0.65,
) -> dict:
    """构建 exact RuntimeScope 下的新版只读多图层投影。"""
    layers = _parse_layers(layers_raw)
    if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
        return {
            "nodes": [], "edges": [], "total": 0, "node_total": 0,
            "layers": list(layers), "read_only": True, "reason_code": "scope_required",
        }
    key = (
        scope.bot_id, scope.session.id, scope.visibility, tuple(sorted(layers)),
        float(min_confidence), int(memory_limit), int(similarity_k), float(similarity_threshold),
    )
    if use_cache:
        cached = _cache_get(key)
        if cached is not None:
            return dict(cached, cache={"hit": True, "ttl_seconds": _GRAPH_CACHE_TTL_SECONDS})
    container = get_container()
    data = build_graph_projection(
        conn=_conn(), scope=scope, layers=layers,
        memory_index=getattr(container, "memory_index", None),
        fewshot_repository=getattr(container, "fewshot_repository", None),
        book_lore_repository=getattr(container, "book_lore_repository", None),
        min_confidence=min_confidence, memory_limit=memory_limit,
        similarity_k=similarity_k, similarity_threshold=similarity_threshold,
    )
    data["cache"] = {"hit": False, "ttl_seconds": _GRAPH_CACHE_TTL_SECONDS}
    if use_cache:
        _cache_put(key, data)
    return data


def clear_kg_cache() -> None:
    _overview_cache.clear()


def warmup_kg_cache(layers: str = "facts") -> dict:
    """Scope-less 启动预热不得读取任何业务行。"""
    started = time.perf_counter()
    data = build_full_graph_data(layers, use_cache=False, scope=None)
    return {
        "ok": True, "layers": ",".join(_parse_layers(layers)), "edges": 0,
        "reason_code": data["reason_code"],
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    }


@kg_bp.route("/overview")
@require_auth
async def overview():
    try:
        scope = _group_scope_from_query()
        min_confidence = float(request.args.get("min_confidence", 0.0))
        max_nodes = max(1, min(1000, int(request.args.get("max_nodes", 150))))
        graph = build_full_graph_data(
            "facts", use_cache=True, min_confidence=min_confidence, scope=scope
        )
        selected = sorted(graph["nodes"], key=lambda node: node.get("degree", 0), reverse=True)[:max_nodes]
        selected_ids = {node["id"] for node in selected}
        graph_to_numeric = {node["id"]: index + 1 for index, node in enumerate(selected)}
        nodes = [dict(node, id=graph_to_numeric[node["id"]]) for node in selected]
        edges = [
            {"source": graph_to_numeric[edge["s"]], "target": graph_to_numeric[edge["t"]],
             "label": edge["l"], "weight": edge["weight"], "read_only": True}
            for edge in graph["edges"]
            if edge["s"] in selected_ids and edge["t"] in selected_ids
        ]
        return jsonify({"nodes": nodes, "edges": edges, "scope": ScopeCodec.to_dict(scope), "read_only": True})
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


def _scoped_memories_for_entity(conn, scope: RuntimeScope, name: str, limit: int) -> list[dict]:
    if not _canonical_memory_store(conn):
        return []
    params = _scope_params(scope)
    rows = conn.execute(
        """SELECT id, content, sender_name, timestamp, version
             FROM memories
            WHERE sender_name=? AND bot_id=? AND session_id=? AND visibility=?
              AND resolution_state='resolved' AND COALESCE(quarantine,0)=0
            ORDER BY timestamp DESC LIMIT ?""",
        (name, *params, limit),
    ).fetchall()
    return [
        {"id": row[0], "content": row[1] or "", "sender": row[2] or "", "ts": row[3], "revision": row[4]}
        for row in rows
    ]


@kg_bp.route("/entity/<entity_name>")
@require_auth
async def entity_detail(entity_name: str):
    try:
        scope = _group_scope_from_query()
        name = unquote(entity_name).strip()
        limit = max(1, min(100, int(request.args.get("limit", 15))))
        conn = _conn()
        facts = []
        fact_columns = _table_columns(conn, "scoped_facts")
        if {"bot_id", "session_id", "visibility"} <= fact_columns:
            revision_sql = "revision" if "revision" in fact_columns else "1"
            status_filter = "AND status NOT IN ('deleted','superseded')" if "status" in fact_columns else ""
            rows = conn.execute(
                f"""SELECT id, subject, predicate, object, confidence, status, source_memory_id,
                            updated_at, {revision_sql}
                     FROM scoped_facts
                    WHERE bot_id=? AND session_id=? AND visibility=? AND (subject=? OR object=?)
                      {status_filter}
                    ORDER BY updated_at DESC, id DESC LIMIT ?""",
                (*_scope_params(scope), name, name, limit),
            ).fetchall()
            facts = [
                _decorate_mutable_item(
                    {"id": row[0], "subject": row[1], "predicate": row[2], "object": row[3],
                     "confidence": row[4], "status": row[5], "source_memory_id": row[6],
                     "revision": int(row[8]), "read_only": True},
                    kind="fact",
                    scope=scope,
                )
                for row in rows
            ]
        relations = [
            _decorate_mutable_item({**row, "type": row["relation_type"]}, kind="tag_relation", scope=scope)
            for row in _list_scoped_relations_from_conn(conn, scope)
            if row["source"] == name or row["target"] == name
        ][:limit]
        memories = _scoped_memories_for_entity(conn, scope, name, limit)
        person = None
        if memories:
            person = {"name": name, "msg_count": len(memories), "read_only": True}
        return jsonify({
            "name": name, "person": person, "facts": facts, "relations": relations,
            "memories": memories, "scope": ScopeCodec.to_dict(scope), "read_only": True,
        })
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@kg_bp.route("/add-fact", methods=["POST"])
@require_auth
async def add_fact():
    return _read_only_gone()


@kg_bp.route("/payment", methods=["POST"])
@require_auth
async def payment_webhook():
    """只验证通知，不写事实、关系或好感；弱/缺失 secret 时关闭。"""
    try:
        scope = _group_scope_from_query()
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)
    config = getattr(get_container(), "plugin_config", None)
    expected = str(config.get("payment_secret") or "") if isinstance(config, dict) else ""
    if len(expected) < _STRONG_SECRET_MIN_LENGTH or expected.casefold() in _WEAK_PAYMENT_SECRETS:
        return _scope_error("payment_disabled", 503)
    body = await request.get_json(silent=True) or {}
    supplied = str(body.get("secret") or "")
    if not hmac.compare_digest(supplied, expected):
        return _scope_error("invalid_payment_secret", 403)
    try:
        amount = float(body.get("amount", 0))
    except (TypeError, ValueError):
        amount = 0
    if amount <= 0:
        return _scope_error("invalid_payment_amount", 400)
    return jsonify({
        "ok": True, "verified": True, "amount": amount,
        "note": str(body.get("note") or ""), "scope": ScopeCodec.to_dict(scope),
        "read_only": True, "knowledge_written": False,
    })


def _graph_layer_counts(scope: RuntimeScope) -> dict[str, int | None]:
    counts = scoped_layer_counts(_conn(), scope)
    container = get_container()
    for layer, attribute in (("few_shot", "fewshot_repository"), ("book_lore", "book_lore_repository")):
        repository = getattr(container, attribute, None)
        counter = getattr(repository, "count_approved", None)
        if callable(counter):
            try:
                counts[layer] = int(counter(scope=scope))
            except Exception:
                counts[layer] = None
    return counts


@kg_bp.route("/stats")
@require_auth
async def kg_stats():
    try:
        scope = _group_scope_from_query()
        conn = _conn()
        params = _scope_params(scope)
        counts = {"facts": 0, "tag_relations": 0, "persons": 0}
        fact_columns = _table_columns(conn, "scoped_facts")
        if {"bot_id", "session_id", "visibility"} <= fact_columns:
            fact_filter = " AND status NOT IN ('deleted','superseded')" if "status" in fact_columns else ""
            counts["facts"] = int(conn.execute(
                f"SELECT COUNT(*) FROM scoped_facts WHERE bot_id=? AND session_id=? AND visibility=?{fact_filter}", params
            ).fetchone()[0])
        relation_columns = _table_columns(conn, "scoped_tag_relations")
        if {"bot_id", "session_id", "visibility"} <= relation_columns:
            relation_filter = " AND status NOT IN ('deleted','superseded')" if "status" in relation_columns else ""
            counts["tag_relations"] = int(conn.execute(
                f"SELECT COUNT(*) FROM scoped_tag_relations WHERE bot_id=? AND session_id=? AND visibility=?{relation_filter}", params
            ).fetchone()[0])
        if _canonical_memory_store(conn):
            counts["persons"] = int(conn.execute(
                """SELECT COUNT(DISTINCT sender_id) FROM memories
                    WHERE bot_id=? AND session_id=? AND visibility=?
                      AND resolution_state='resolved' AND COALESCE(quarantine,0)=0""", params
            ).fetchone()[0])
        layer_counts = _graph_layer_counts(scope)
        return jsonify({
            **counts, "layer_counts": layer_counts, "supported_layers": list(SUPPORTED_LAYERS),
            "scope": ScopeCodec.to_dict(scope), "read_only": True,
        })
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@kg_bp.route("/config")
@require_auth
async def kg_config():
    try:
        scope = _group_scope_from_query()
        rel_types = sorted({row["relation_type"] for row in _list_scoped_relations_from_conn(_conn(), scope) if row["relation_type"]})
        counts = _graph_layer_counts(scope)
        layer_labels = {
            "facts": "事实与标签关系", "memories": "记忆与 HNSW 同域近邻",
            "beliefs": "信念", "jargon": "群内表达", "concerns": "关切",
            "mood": "情绪", "timeline": "Soul 时间线", "affinity": "人物关系",
            "few_shot": "Few-shot 示例", "book_lore": "BookLore 内化投影",
            "communities": "作用域标签簇",
        }
        return jsonify({
            "relation_types": [*rel_types, "fact", "HNSW 近邻", "标注", "证据", "来源"],
            "node_types": [
                "bot", "person", "topic", "event", "entity", "keyword", "memory", "belief",
                "jargon", "concern", "mood", "timeline", "relationship_event", "few_shot",
                "trait", "book_lore", "community",
            ],
            "supported_layers": [
                {"id": layer, "label": layer_labels[layer], "count": counts.get(layer),
                 "available": counts.get(layer) is not None}
                for layer in SUPPORTED_LAYERS
            ],
            "layer_counts": counts,
            "defaults": {
                "max_nodes": 300, "min_weight": 0.0, "min_confidence": 0.0, "days": 0,
                "layers": ["facts", "memories"], "memory_limit": 150,
                "similarity_k": 3, "similarity_threshold": 0.65,
            },
            "scope": ScopeCodec.to_dict(scope), "read_only": True,
        })
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@kg_bp.route("/full")
@require_auth
async def kg_full():
    try:
        scope = _group_scope_from_query()
        layers = _parse_layers(request.args.get("layers", "facts"))
        min_confidence = _bounded_float(request.args.get("min_confidence"), default=0.0, minimum=0.0, maximum=1.0)
        memory_limit = _bounded_int(request.args.get("memory_limit"), default=150, minimum=10, maximum=500)
        similarity_k = _bounded_int(request.args.get("similarity_k"), default=3, minimum=1, maximum=10)
        similarity_threshold = _bounded_float(request.args.get("similarity_threshold"), default=0.65, minimum=0.0, maximum=1.0)
        build_kwargs = {
            "use_cache": True, "min_confidence": min_confidence, "scope": scope,
            "memory_limit": memory_limit, "similarity_k": similarity_k,
            "similarity_threshold": similarity_threshold,
        }
        # 正式 ConnectionManager 使用 check_same_thread=False；轻量单测连接保留同步执行。
        container = get_container()
        threaded = getattr(getattr(container, "db", None), "_cm", None) is not None
        if threaded:
            payload = await asyncio.to_thread(build_full_graph_data, layers, **build_kwargs)
        else:
            payload = build_full_graph_data(layers, **build_kwargs)
        if "facts" in layers and not _graph_projection_matches(
            payload, scope, min_confidence=min_confidence
        ):
            clear_kg_cache()
            if threaded:
                payload = await asyncio.to_thread(build_full_graph_data, layers, **build_kwargs)
            else:
                payload = build_full_graph_data(layers, **build_kwargs)
        return jsonify(
            _decorate_graph_mutations(
                payload, scope, min_confidence=min_confidence
            )
        )
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@kg_bp.route("/entity/<entity_name>/timeline")
@require_auth
async def entity_timeline(entity_name: str):
    try:
        scope = _group_scope_from_query()
        name = unquote(entity_name).strip()
        limit = max(1, min(100, int(request.args.get("limit", 30))))
        conn = _conn()
        events = []
        fact_columns = _table_columns(conn, "scoped_facts")
        if {"bot_id", "session_id", "visibility"} <= fact_columns:
            status_filter = "AND status NOT IN ('deleted','superseded')" if "status" in fact_columns else ""
            rows = conn.execute(
                f"""SELECT subject, predicate, object, updated_at, source_memory_id
                     FROM scoped_facts
                    WHERE bot_id=? AND session_id=? AND visibility=? AND (subject=? OR object=?)
                      {status_filter}
                    ORDER BY updated_at DESC LIMIT ?""",
                (*_scope_params(scope), name, name, limit),
            ).fetchall()
            events.extend({
                "type": "fact", "ts": row[3], "subject": row[0], "predicate": row[1],
                "object": row[2], "source_id": row[4],
            } for row in rows)
        events.extend(
            {"type": "memory", **item}
            for item in _scoped_memories_for_entity(conn, scope, name, limit)
        )
        events.sort(key=lambda item: item.get("ts") or 0, reverse=True)
        return jsonify({"name": name, "events": events[:limit], "scope": ScopeCodec.to_dict(scope), "read_only": True})
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@kg_bp.route("/path", methods=["POST"])
@require_auth
async def kg_path():
    try:
        scope = _group_scope_from_query()
        body = await request.get_json(silent=True) or {}
        source, target = str(body.get("from") or "").strip(), str(body.get("to") or "").strip()
        if not source or not target:
            return jsonify({"path": [], "edges": [], "nodes": [], "scope": ScopeCodec.to_dict(scope), "read_only": True})
        adjacency: dict[str, list[tuple[str, str]]] = {}
        for edge in _full_edges(scope):
            adjacency.setdefault(edge["s"], []).append((edge["t"], edge["l"]))
            adjacency.setdefault(edge["t"], []).append((edge["s"], edge["l"]))
        visited: dict[str, tuple[str | None, str | None]] = {source: (None, None)}
        queue = deque([(source, 0)])
        max_depth = max(1, min(8, int(body.get("max_depth", 5))))
        while queue and target not in visited:
            current, depth = queue.popleft()
            if depth >= max_depth:
                continue
            for neighbor, label in adjacency.get(current, []):
                if neighbor not in visited:
                    visited[neighbor] = (current, label)
                    queue.append((neighbor, depth + 1))
        if target not in visited:
            return jsonify({"path": [], "edges": [], "nodes": [], "scope": ScopeCodec.to_dict(scope), "read_only": True})
        names, edges = [], []
        node = target
        while node is not None:
            names.append(node)
            parent, label = visited[node]
            if parent is not None:
                edges.append({"source": parent, "target": node, "label": label})
            node = parent
        names.reverse()
        edges.reverse()
        return jsonify({
            "path": names, "edges": edges,
            "nodes": [{"id": index + 1, "name": name, "type": "entity", "degree": 1} for index, name in enumerate(names)],
            "scope": ScopeCodec.to_dict(scope), "read_only": True,
        })
    except (ScopeValidationError, ScopedKnowledgeScopeError, TypeError, ValueError) as exc:
        return _scope_failure(exc)


@kg_bp.route("/facts/<int:fact_id>", methods=["DELETE"])
@require_auth
async def delete_fact(fact_id: int):
    return _read_only_gone()


@kg_bp.route("/facts/<int:fact_id>", methods=["PUT"])
@require_auth
async def update_fact(fact_id: int):
    return _read_only_gone()


@kg_bp.route("/tag-relations/<int:rel_id>", methods=["DELETE"])
@require_auth
async def delete_tag_relation(rel_id: int):
    return _read_only_gone()


@kg_bp.route("/tag-relations/<int:rel_id>", methods=["PUT"])
@require_auth
async def update_tag_relation(rel_id: int):
    return _read_only_gone()


@kg_bp.route("/tags/<int:tag_id>", methods=["PUT"])
@require_auth
async def update_tag(tag_id: int):
    return _read_only_gone()


@kg_bp.route("/entities/rename-preview", methods=["POST"])
@require_auth
async def rename_entity_preview():
    return _read_only_gone()


@kg_bp.route("/entities/rename", methods=["POST"])
@require_auth
async def rename_entity():
    return _read_only_gone()
