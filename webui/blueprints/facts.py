"""Facts Blueprint — 事实的读取与人工审核（与 /api/beliefs、/api/jargon 对称）。

写入只经 ScopedKnowledgeMutationGateway.review_fact：它在一个 coordinator 事务里同时
完成状态变更与审核审计，保证不会出现「状态改了却查不到是谁改的」。
"""

from __future__ import annotations

from quart import Blueprint, current_app, jsonify, request

from ..api_contract import error_payload, mutation_response, page_response
from ..container import get_container
from ..facts_evidence import (
    columns,
    fact_evidence,
    fact_object_ref,
    facts_page_response,
    review_hint,
    row_dicts,
)
from ..middleware.auth import require_auth

try:
    from ...domain.scope import RuntimeScope, ScopeCodec, ScopeValidationError, SessionRef
    from ...engine.db.scoped_knowledge_repo import ScopedKnowledgeScopeError
    from ...services.scoped_knowledge_mutations import (
        ScopedKnowledgeMutationGateway,
        ScopedKnowledgeMutationTarget,
        ScopedKnowledgeIdempotencyConflict,
        ScopedKnowledgeNotFound,
        ScopedKnowledgeRevisionConflict,
    )
except ImportError:  # pragma: no cover - 插件根目录直接导入
    from domain.scope import RuntimeScope, ScopeCodec, ScopeValidationError, SessionRef
    from engine.db.scoped_knowledge_repo import ScopedKnowledgeScopeError
    from services.scoped_knowledge_mutations import (
        ScopedKnowledgeMutationGateway,
        ScopedKnowledgeMutationTarget,
        ScopedKnowledgeIdempotencyConflict,
        ScopedKnowledgeNotFound,
        ScopedKnowledgeRevisionConflict,
    )

facts_bp = Blueprint("facts", __name__, url_prefix="/api/facts")

_REVIEWABLE_STATUSES = frozenset({"pending", "quarantined", "conflict"})


def _scope_error(code: str, status: int):
    return jsonify({"error": {"code": code}}), status


def _scope_failure(exc: Exception):
    code = getattr(exc, "reason_code", None) or getattr(exc, "code", None) or "invalid_scope"
    if isinstance(exc, ScopedKnowledgeIdempotencyConflict):
        return _scope_error(str(code), 409)
    if isinstance(exc, (ScopedKnowledgeRevisionConflict,)):
        return _scope_error("stale_revision", 409)
    if isinstance(exc, ScopedKnowledgeNotFound):
        return _scope_error("scoped_object_not_found", 404)
    return _scope_error(str(code), 400 if str(code) in {"scope_required", "object_ref_revision_required"} else 422)


def _scoped_repo(container):
    repo = getattr(getattr(container, "db", None), "scoped_knowledge", None)
    if repo is None:
        raise ScopedKnowledgeScopeError("scoped_repository_unavailable")
    return repo


def _connection():
    container = get_container()
    repo = getattr(getattr(container, "db", None), "scoped_knowledge", None)
    return getattr(repo, "cm", None)


def _mutation_gateway(container):
    configured = getattr(container, "scoped_knowledge_mutations", None)
    if configured is not None:
        return configured
    write_gateway = getattr(container, "write_gateway", None)
    if write_gateway is None:
        return None
    try:
        return ScopedKnowledgeMutationGateway(write_gateway)
    except Exception:
        return None


def _object_ref_registry():
    try:
        return current_app.extensions.get("wave_api_contract", {}).get("object_refs")
    except RuntimeError:
        return None


def _group_scope_from_query(*, optional: bool = False) -> RuntimeScope | None:
    required = ("bot_id", "session_id", "visibility")
    if any(request.args.get(field) is None for field in required):
        if optional:
            return None
        raise ScopedKnowledgeScopeError("scope_required")
    bot_id, session_id, visibility = (request.args.get(field) for field in required)
    if visibility != "group":
        raise ScopedKnowledgeScopeError("derived_scope_visibility_unsupported")
    try:
        platform_id, kind, conversation_id = str(session_id).split(":", 2)
    except ValueError as exc:
        raise ScopeValidationError("invalid_session_id", "session_id must be canonical") from exc
    return RuntimeScope(
        str(bot_id), visibility, SessionRef(str(session_id), platform_id, kind, conversation_id)
    )


def _scope_from_envelope(body: dict) -> RuntimeScope:
    if "scope" not in body:
        raise ScopedKnowledgeScopeError("scope_required")
    scope = ScopeCodec.from_dict(body["scope"])
    if not isinstance(scope, RuntimeScope) or scope.visibility != "group" or scope.session is None:
        raise ScopedKnowledgeScopeError("derived_scope_visibility_unsupported")
    return scope


def _pagination_from_query() -> tuple[int, int]:
    try:
        limit = int(request.args.get("limit", 25))
        offset = int(request.args.get("offset", 0))
    except (TypeError, ValueError) as exc:
        raise ScopedKnowledgeScopeError("invalid_pagination") from exc
    if limit < 1 or limit > 500 or offset < 0:
        raise ScopedKnowledgeScopeError("invalid_pagination")
    return limit, offset


def _fact_scope_params(scope: RuntimeScope) -> tuple[str, str, str]:
    return scope.bot_id, scope.session.id, scope.visibility


def _require_object_ref(body: dict, *, locator: int, scope: RuntimeScope, item: dict) -> None:
    """审核必须携带列表签发的 opaque ref 与同一 revision，避免过期点击。"""
    descriptor = body.get("object_ref") or body.get("ref")
    ref = descriptor.get("ref") if isinstance(descriptor, dict) else descriptor
    try:
        revision = int(body.get("revision"))
    except (TypeError, ValueError) as exc:
        raise ScopedKnowledgeScopeError("object_ref_revision_required") from exc
    registry = _object_ref_registry()
    binding = registry.resolve(ref, kind="fact", locator=locator, request_scope=scope) if registry else None
    if binding is None or binding.revision != revision:
        raise ScopedKnowledgeScopeError("object_ref_stale")


def _find_scoped_fact(conn, scope: RuntimeScope, fact_id: int) -> dict:
    """按 Scope 三列等值读取单条事实；跨 Bot/跨群的 fact_id 一律视为不存在。"""
    cursor = conn.execute_read(
        """SELECT * FROM scoped_facts
            WHERE id=? AND bot_id=? AND session_id=? AND visibility=?""",
        (int(fact_id), *_fact_scope_params(scope)),
    )
    if not cursor.description:
        raise ScopedKnowledgeNotFound()
    names = [description[0] for description in cursor.description]
    row = cursor.fetchone()
    if row is None:
        raise ScopedKnowledgeNotFound()
    return dict(zip(names, row))


def _capabilities(kind: str, *, available: bool, reason: str) -> dict:
    return {
        "review": {
            "available": available,
            "reason_code": None if available else reason,
            "command": f"/api/facts/commands/{kind}",
        },
        "batch_review": {
            "available": available,
            "reason_code": None if available else reason,
            "command": "/api/facts/commands/batch-review",
        },
    }


@facts_bp.route("", methods=["GET"])
@facts_bp.route("/", methods=["GET"])
@require_auth
async def list_facts():
    """列出当前 Scope 的正式事实，附证据、审核建议与可用的审核动作。"""
    try:
        limit, offset = _pagination_from_query()
        scope = _group_scope_from_query(optional=True)
        if scope is None:
            # 无 Scope 时返回空集合；前端通过 botId/sessionId 提示「请选择」，
            # 契约测试 R13 亦验证无参查询能安全返回合法分页结构。
            payload = facts_page_response([], total=0, limit=limit, offset=offset)
            payload["scope"] = None
            payload["capabilities"] = {
                **_capabilities("review", available=False, reason="scope_required"),
                "review_hint": {"available": False, "reason_code": "scope_required"},
            }
            return jsonify(payload)
        container = get_container()
        cm = _connection()
        if cm is None:
            return jsonify(error_payload("service_unavailable", "Knowledge store is unavailable", retryable=True)), 503
        table_columns = columns(cm, "scoped_facts")
        if not {"bot_id", "session_id", "visibility"} <= table_columns:
            return jsonify(page_response([], total=0, limit=limit, offset=offset))

        where = ["bot_id=?", "session_id=?", "visibility=?"]
        params: list[object] = list(_fact_scope_params(scope))
        search = str(request.args.get("search") or "").strip()
        searchable = [name for name in ("subject", "predicate", "object") if name in table_columns]
        if search and searchable:
            where.append("(" + " OR ".join(f'"{name}" LIKE ?' for name in searchable) + ")")
            params.extend([f"%{search}%"] * len(searchable))
        status = str(request.args.get("status") or "").strip()
        if status and "status" in table_columns:
            where.append("status=?")
            params.append(status)
        elif "status" in table_columns:
            where.append("status NOT IN ('deleted','superseded')")
        where_sql = " WHERE " + " AND ".join(where)

        rows = cm.execute_read(
            f"SELECT * FROM scoped_facts{where_sql} ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            [*params, limit, offset],
        )
        items = row_dicts(rows)
        total = int(cm.execute_read(
            f"SELECT COUNT(*) FROM scoped_facts{where_sql}", params
        ).fetchone()[0])
        fact_evidence(cm, items, _fact_scope_params(scope))

        gateway = _mutation_gateway(container)
        registry = _object_ref_registry()
        available = gateway is not None and registry is not None
        reason = "scoped_knowledge_mutation_gateway_unavailable" if gateway is None else "object_ref_registry_unavailable"
        for item in items:
            item["review_hint"] = review_hint(item)
            item["object_ref"] = fact_object_ref(item, scope, registry)
            item["capabilities"] = _capabilities("review", available=available, reason=reason)
            item["editable"] = available and str(item.get("status") or "") in _REVIEWABLE_STATUSES

        payload = facts_page_response(items, total=total, limit=limit, offset=offset)
        payload["scope"] = ScopeCodec.to_dict(scope)
        payload["capabilities"] = {
            **_capabilities("review", available=available, reason=reason),
            "review_hint": {"available": True, "reason_code": None},
        }
        return jsonify(payload)
    except (ScopedKnowledgeScopeError, ScopeValidationError) as exc:
        return _scope_failure(exc)
    except (TypeError, ValueError):
        return _scope_error("invalid_pagination", 400)
    except Exception:
        return jsonify(error_payload("service_unavailable", "Knowledge store is unavailable", retryable=True)), 503


@facts_bp.route("/<int:fact_id>/<action>", methods=["POST"])
@require_auth
async def review_fact(fact_id: int, action: str):
    """人工审核单条事实：approve → active/conflict，reject → rejected。"""
    if action not in {"approve", "reject"}:
        return _scope_error("unsupported_fact_review_action", 405)
    try:
        body = await request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return _scope_error("invalid_request", 400)
        scope = _scope_from_envelope(body)
        container = get_container()
        gateway = _mutation_gateway(container)
        if gateway is None:
            return _scope_error("scoped_knowledge_mutation_gateway_unavailable", 503)
        cm = _connection()
        if cm is None:
            return _scope_error("scoped_repository_unavailable", 503)
        item = _find_scoped_fact(cm, scope, fact_id)
        _require_object_ref(body, locator=fact_id, scope=scope, item=item)
        result = await gateway.review_fact(
            scope=scope,
            target=ScopedKnowledgeMutationTarget("fact", fact_id, int(body.get("revision"))),
            action=action,
            reason=body.get("reason"),
            idempotency_key=body.get("idempotency_key"),
        )
        return jsonify(mutation_response(
            operation_kind=f"fact.{action}",
            status="succeeded",
            revision=result.revision,
            item={"id": fact_id, "status": result.status},
            include_item=True,
        ))
    except ScopedKnowledgeNotFound:
        return _scope_error("scoped_object_not_found", 404)
    except (ScopedKnowledgeScopeError, ScopeValidationError) as exc:
        return _scope_failure(exc)
    except (ScopedKnowledgeRevisionConflict, ScopedKnowledgeIdempotencyConflict) as exc:
        return _scope_failure(exc)
    except (TypeError, ValueError):
        return _scope_error("invalid_request", 400)
    except Exception:
        return _scope_error("scoped_knowledge_mutation_gateway_unavailable", 503)


@facts_bp.route("/commands/batch-review", methods=["POST"])
@require_auth
async def batch_review_facts():
    """批量审核：逐条独立提交，返回每条的成败，失败项可安全重试。"""
    try:
        body = await request.get_json(silent=True) or {}
        if not isinstance(body, dict):
            return _scope_error("invalid_request", 400)
        scope = _scope_from_envelope(body)
        action = str(body.get("action") or "").strip()
        if action not in {"approve", "reject"}:
            return _scope_error("unsupported_fact_review_action", 405)
        entries = body.get("items")
        if not isinstance(entries, list) or not entries:
            return _scope_error("invalid_request", 400)
        container = get_container()
        gateway = _mutation_gateway(container)
        if gateway is None:
            return _scope_error("scoped_knowledge_mutation_gateway_unavailable", 503)
        cm = _connection()
        if cm is None:
            return _scope_error("scoped_repository_unavailable", 503)

        succeeded: list[dict] = []
        failed: list[dict] = []
        for entry in entries:
            if not isinstance(entry, dict):
                failed.append({"id": None, "code": "invalid_request"})
                continue
            try:
                fact_id = int(entry.get("id"))
            except (TypeError, ValueError):
                failed.append({"id": None, "code": "invalid_request"})
                continue
            try:
                item = _find_scoped_fact(cm, scope, fact_id)
                _require_object_ref(entry, locator=fact_id, scope=scope, item=item)
                result = await gateway.review_fact(
                    scope=scope,
                    target=ScopedKnowledgeMutationTarget("fact", fact_id, int(entry.get("revision"))),
                    action=action,
                    reason=entry.get("reason"),
                    idempotency_key=entry.get("idempotency_key"),
                )
                succeeded.append({"id": fact_id, "status": result.status, "revision": result.revision})
            except Exception as exc:
                code = getattr(exc, "reason_code", None) or getattr(exc, "code", None) or "review_failed"
                failed.append({"id": fact_id, "code": str(code)})
        return jsonify({
            "operation_kind": f"fact.batch_{action}",
            "status": "succeeded" if not failed else ("partial" if succeeded else "failed"),
            "succeeded": succeeded,
            "failed": failed,
        })
    except (ScopedKnowledgeScopeError, ScopeValidationError) as exc:
        return _scope_failure(exc)
    except (TypeError, ValueError):
        return _scope_error("invalid_request", 400)
    except Exception:
        return _scope_error("scoped_knowledge_mutation_gateway_unavailable", 503)


def _scoped_fact_evidence(cm: Any, scope: RuntimeScope, fact: dict, *, before: int = 15, after: int = 15) -> dict:
    """提取事实的前后聊天上下文与原话证据。"""
    import json
    source_memory_id = fact.get("source_memory_id")
    prov = fact.get("provenance")
    if isinstance(prov, str):
        try:
            prov = json.loads(prov)
        except Exception:
            prov = {}
    elif not isinstance(prov, dict):
        prov = {}
    source_quote = str(fact.get("source_quote") or prov.get("source_quote") or "").strip()

    base_payload = {
        "ok": True,
        "fact": {
            "id": fact.get("id"),
            "subject": fact.get("subject"),
            "predicate": fact.get("predicate"),
            "object": fact.get("object"),
            "source_quote": source_quote,
            "revision": fact.get("revision", 1),
        },
        "scope": ScopeCodec.to_dict(scope),
        "anchor": None,
        "messages": [],
        "source_quote": source_quote,
        "used_fallback": True,
    }
    if not source_memory_id or cm is None or scope.session is None:
        return base_payload

    try:
        conn = getattr(cm, "conn", cm)
        columns_set = {str(row[1]) for row in conn.execute("PRAGMA table_info(memories)").fetchall()}
        required = {"id", "content", "timestamp", "bot_id", "session_id", "visibility"}
        if not required <= columns_set:
            return base_payload

        group_col = "group_id" if "group_id" in columns_set else "session_id"
        sender_col = "sender_name" if "sender_name" in columns_set else ("sender_id" if "sender_id" in columns_set else "NULL")
        select_cols = f"id, {group_col}, sender_id, {sender_col}, content, timestamp"
        scope_where = "bot_id=? AND session_id=? AND visibility=?"
        scope_params = (scope.bot_id, scope.session.id, scope.visibility)

        anchor = conn.execute(
            f"SELECT {select_cols} FROM memories WHERE id=? AND {scope_where} LIMIT 1",
            (int(source_memory_id), *scope_params),
        ).fetchone()
        if not anchor:
            return base_payload

        anchor_ts = float(anchor[5])
        message_filter = " AND memory_type='message'" if "memory_type" in columns_set else ""
        before_rows = conn.execute(
            f"SELECT {select_cols} FROM memories WHERE {scope_where} AND timestamp < ?{message_filter} "
            "ORDER BY timestamp DESC LIMIT ?",
            (*scope_params, anchor_ts, before),
        ).fetchall()
        after_rows = conn.execute(
            f"SELECT {select_cols} FROM memories WHERE {scope_where} AND timestamp > ?{message_filter} "
            "ORDER BY timestamp ASC LIMIT ?",
            (*scope_params, anchor_ts, after),
        ).fetchall()

        def row_to_msg(r, role: str) -> dict:
            return {
                "id": r[0],
                "group_id": r[1],
                "sender_id": r[2],
                "sender_name": r[3] or r[2] or "未知发送者",
                "content": r[4] or "",
                "timestamp": r[5],
                "role": role,
            }

        anchor_msg = row_to_msg(anchor, "anchor")
        return {
            **base_payload,
            "anchor": anchor_msg,
            "messages": [
                *[row_to_msg(r, "before") for r in reversed(before_rows)],
                anchor_msg,
                *[row_to_msg(r, "after") for r in after_rows],
            ],
            "used_fallback": False,
        }
    except Exception:
        return base_payload


@facts_bp.route("/<int:fact_id>/evidence", methods=["GET"])
@require_auth
async def get_scoped_fact_evidence(fact_id: int):
    """按当前 RuntimeScope 还原该事实关联记忆的聊天上下文气泡。"""
    try:
        scope = _group_scope_from_query()
        cm = _connection()
        if cm is None:
            return jsonify(error_payload("service_unavailable", "Knowledge store is unavailable", retryable=True)), 503
        item = _find_scoped_fact(cm, scope, fact_id)
        before = max(0, min(50, int(request.args.get("before", 15))))
        after = max(0, min(50, int(request.args.get("after", 15))))
        return jsonify(_scoped_fact_evidence(cm, scope, item, before=before, after=after))
    except ScopedKnowledgeNotFound:
        return _scope_error("scoped_object_not_found", 404)
    except (ScopedKnowledgeScopeError, ScopeValidationError) as exc:
        return _scope_failure(exc)
    except (TypeError, ValueError):
        return _scope_error("invalid_pagination", 400)
    except Exception as exc:
        return jsonify(error_payload("service_unavailable", f"Evidence extraction failed: {exc}", retryable=True)), 503


@facts_bp.route("/reviews", methods=["GET"])
@require_auth
async def list_reviews():
    """人工审核流水（只读）。"""
    try:
        scope = _group_scope_from_query()
        repo = _scoped_repo(get_container())
        fact_id = request.args.get("fact_id")
        rows = repo.list_scoped_fact_reviews(
            scope,
            fact_id=int(fact_id) if str(fact_id or "").strip().isdigit() else None,
            limit=min(200, max(1, int(request.args.get("limit", 50)))),
        )
        return jsonify({"items": rows, "scope": ScopeCodec.to_dict(scope)})
    except (ScopedKnowledgeScopeError, ScopeValidationError) as exc:
        return _scope_failure(exc)
    except (TypeError, ValueError):
        return _scope_error("invalid_pagination", 400)
    except Exception:
        return jsonify(error_payload("service_unavailable", "Knowledge store is unavailable", retryable=True)), 503


__all__ = ["facts_bp"]
