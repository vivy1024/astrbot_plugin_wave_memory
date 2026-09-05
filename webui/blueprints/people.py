"""规范 People API：按 ``(user_id, group_id, bot_id)`` 投影人物画像。"""

from __future__ import annotations

import json
import math
import time
from collections.abc import Mapping
from typing import Any

from quart import Blueprint, current_app, jsonify, request

from ..api_contract import current_runtime_scope, error_payload, mutation_response, page_response
from ..container import get_container
from ..middleware.auth import require_auth

try:
    from ...domain.scope import RuntimeScope, SessionRef
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope, SessionRef

try:
    from ...services.relationship_calibration import RelationshipCalibrationError, RelationshipCalibrationGateway
    from ...services.evidence_resolver import EvidenceResolutionError, resolve_relationship_evidence
except ImportError:  # pragma: no cover - focused tests import webui as top-level
    from domain.scope import RuntimeScope, ScopeValidationError
    from services.relationship_calibration import RelationshipCalibrationError, RelationshipCalibrationGateway
    from services.evidence_resolver import EvidenceResolutionError, resolve_relationship_evidence

people_bp = Blueprint("people", __name__, url_prefix="/api")


def _page_args() -> tuple[int, int]:
    limit = max(1, min(500, int(request.args.get("limit", request.args.get("size", 25)))))
    if request.args.get("offset") is not None:
        offset = max(0, int(request.args.get("offset", 0)))
    else:
        page = max(1, int(request.args.get("page", 1)))
        offset = (page - 1) * limit
    return limit, offset


def _connection():
    db = getattr(get_container(), "db", None)
    if db is None:
        return None
    if getattr(db, "closed", False):
        try:
            db.reopen()
        except Exception:
            return None
    return getattr(db, "conn", None)


def _table_rows(conn: Any, table: str, where: str = "", params: tuple[Any, ...] = ()) -> list[dict[str, Any]]:
    sql = f'SELECT * FROM "{table}"'
    if where:
        sql += f" WHERE {where}"
    cursor = conn.execute(sql, params)
    names = [str(column[0]) for column in cursor.description or ()]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _table_exists(conn: Any, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _json(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _registry_by_principal(conn: Any) -> dict[str, dict[str, Any]]:
    if not _table_exists(conn, "person_registry"):
        return {}
    result: dict[str, dict[str, Any]] = {}
    for row in _table_rows(conn, "person_registry"):
        principal = str(row.get("qq_id") or row.get("user_id") or "").strip()
        if not principal or principal in result:
            continue
        item = dict(row)
        item["aliases"] = _json(item.get("aliases"), [])
        item["groups"] = _json(item.get("groups"), [])
        item["tag_ids"] = _json(item.get("tag_ids"), [])
        item["metadata"] = _json(item.get("metadata"), {})
        result[principal] = item
    return result


def _request_scope():
    """只接受由统一请求 Scope provider 解析出的 RuntimeScope。"""
    try:
        provider = current_app.extensions.get("wave_api_contract", {}).get("request_scope_provider")
    except RuntimeError:
        provider = None
    return current_runtime_scope(provider)


def _alias_session_ids(conn: Any, scope) -> list[str]:
    """Same bot + group conversation, different platform names. Read-only aliases."""
    if scope is None or scope.session is None or conn is None:
        return []
    current_id = scope.session.id
    conversation_id = scope.session.conversation_id
    if not conversation_id:
        return [current_id]
    ids = [current_id]
    try:
        rows = conn.execute(
            """SELECT DISTINCT session_id
                 FROM scoped_soul_relationships
                WHERE bot_id=? AND visibility=? AND session_id LIKE ?""",
            (scope.bot_id, scope.visibility, f"%:group:{conversation_id}"),
        ).fetchall()
    except Exception:
        return ids
    suffix = f":group:{conversation_id}"
    for (session_id,) in rows:
        value = str(session_id or "").strip()
        if value and value.endswith(suffix) and value not in ids:
            ids.append(value)
    return ids


def _relationship_rows_for_scope(repository: Any, scope, alias_session_ids: list[str]) -> dict[str, Any]:
    rows: dict[str, Any] = {}
    for item in repository.list_relationships(scope):
        rows[str(item["subject_principal_id"])] = item
    if scope.session is None:
        return rows
    for session_id in alias_session_ids:
        if session_id == scope.session.id:
            continue
        try:
            platform_id, kind, conversation_id = session_id.split(":", 2)
            alias_scope = RuntimeScope(
                scope.bot_id,
                scope.visibility,
                SessionRef(session_id, platform_id, kind, conversation_id),
            )
        except Exception:
            continue
        for item in repository.list_relationships(alias_scope):
            subject = str(item.get("subject_principal_id") or "")
            current_subject = f"{scope.session.platform_id}:user:{subject.split(':user:')[-1]}" if ":user:" in subject else subject
            if current_subject in rows:
                continue
            alias_item = dict(item)
            alias_item["subject_principal_id"] = current_subject
            alias_item["calibration"] = {
                "available": False,
                "reason_code": "alias_session_readonly",
            }
            rows[current_subject] = alias_item
    return rows


def _profile_item(profile: dict[str, Any], registry: dict[str, dict[str, Any]], scope) -> dict[str, Any] | None:
    user_id = str(profile.get("user_id") or "").strip()
    group_id = str(profile.get("group_id") or "").strip()
    bot_id = str(profile.get("bot_id") or "").strip()
    if not user_id or not group_id or not bot_id:
        return None
    if scope is None or scope.session is None or scope.visibility != "group":
        return None
    if bot_id != scope.bot_id or group_id != scope.session.conversation_id:
        return None

    person = registry.get(user_id, {})
    item = dict(profile)
    item.pop("affection", None)
    item["scope"] = {"user_id": user_id, "group_id": group_id, "bot_id": bot_id}
    item["scope_key"] = f"{user_id}|{group_id}|{bot_id}"
    item["display_name"] = person.get("display_name") or item.get("nickname") or user_id
    item["aliases"] = person.get("aliases", [])
    item["registry_metadata"] = person.get("metadata", {})
    item["metadata"] = _json(item.get("metadata"), {})
    item["person_registry"] = {
        key: person.get(key)
        for key in ("qq_id", "display_name", "first_seen", "last_seen", "message_count", "groups", "tag_ids")
        if key in person
    }
    # 旧 affection 不是经复合 RuntimeScope 验证的 affinity projection，不得伪装为好感度。
    item["affinity"] = None
    item["affinity_status"] = "unavailable"
    item["affinity_reason_code"] = "scoped_affinity_projection_unavailable"
    return item


def _optional_float(raw: Any, name: str) -> float | None:
    text = str(raw or "").strip()
    if not text:
        return None
    value = float(text)
    if not math.isfinite(value):
        raise ValueError(f"invalid {name}")
    return value


def _optional_int(raw: Any, name: str) -> int | None:
    text = str(raw or "").strip()
    if not text:
        return None
    return int(text)


def _people_query_from_request() -> dict[str, Any]:
    return {
        "search": str(request.args.get("search") or "").strip().casefold(),
        "user_id": str(request.args.get("user_id") or "").strip(),
        "relationship_state": str(request.args.get("relationship_state") or "all").strip().lower() or "all",
        "alias_filter": str(request.args.get("alias_filter") or "all").strip().lower() or "all",
        "sort_by": str(request.args.get("sort_by") or "name").strip().lower() or "name",
        "sort_order": str(request.args.get("sort_order") or "asc").strip().lower() or "asc",
        "min_affinity": _optional_float(request.args.get("min_affinity"), "min_affinity"),
        "max_affinity": _optional_float(request.args.get("max_affinity"), "max_affinity"),
        "min_interactions": _optional_int(request.args.get("min_interactions"), "min_interactions"),
    }


def _person_from_relationship(item: Mapping[str, Any]) -> dict[str, Any]:
    person = item.get("person")
    return dict(person) if isinstance(person, Mapping) else {}


def _relationship_affinity(item: Mapping[str, Any]) -> float | None:
    value = item.get("affinity")
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(float(value)):
        return None
    return float(value)


def _interaction_count(person: Mapping[str, Any]) -> int | None:
    for candidate in (person.get("interaction_count"), (person.get("person_registry") or {}).get("message_count") if isinstance(person.get("person_registry"), Mapping) else None):
        if isinstance(candidate, bool) or not isinstance(candidate, (int, float)) or not math.isfinite(float(candidate)):
            continue
        return int(candidate)
    return None


def _alias_count(person: Mapping[str, Any]) -> int:
    aliases = person.get("aliases")
    if not isinstance(aliases, list):
        return 0
    return sum(1 for alias in aliases if str(alias or "").strip())


def _filter_people_rows(
    items: list[dict[str, Any]],
    *,
    query: Mapping[str, Any] | None = None,
    relationship_lookup: Mapping[str, Mapping[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    filters = dict(query or _people_query_from_request())
    search = str(filters.get("search") or "").strip().casefold()
    user_filter = str(filters.get("user_id") or "").strip()
    relationship_state = str(filters.get("relationship_state") or "all").strip().lower() or "all"
    alias_filter = str(filters.get("alias_filter") or "all").strip().lower() or "all"
    sort_by = str(filters.get("sort_by") or "name").strip().lower() or "name"
    sort_order = str(filters.get("sort_order") or "asc").strip().lower() or "asc"
    min_affinity = filters.get("min_affinity")
    max_affinity = filters.get("max_affinity")
    min_interactions = filters.get("min_interactions")
    if min_affinity is not None:
        min_affinity = float(min_affinity)
    if max_affinity is not None:
        max_affinity = float(max_affinity)
    if min_interactions is not None:
        min_interactions = int(min_interactions)

    filtered: list[dict[str, Any]] = []
    for item in items:
        nested_person = item.get("person") if isinstance(item.get("person"), Mapping) else None
        person = dict(nested_person) if nested_person is not None else item
        user_id = str(person.get("user_id") or "")
        if user_filter and user_id != user_filter:
            continue
        relationship = item if nested_person is not None else ((relationship_lookup or {}).get(user_id) or item)
        affinity = _relationship_affinity(relationship or {})
        if relationship_state == "known" and affinity is None:
            continue
        if relationship_state == "unknown" and affinity is not None:
            continue
        if min_affinity is not None and (affinity is None or affinity < min_affinity):
            continue
        if max_affinity is not None and (affinity is None or affinity > max_affinity):
            continue
        interactions = _interaction_count(person)
        if min_interactions is not None and (interactions is None or interactions < min_interactions):
            continue
        aliases = _alias_count(person)
        if alias_filter == "has" and aliases <= 0:
            continue
        if alias_filter == "none" and aliases > 0:
            continue
        if search:
            alias_text = " ".join(
                str(alias).strip()
                for alias in (person.get("aliases") or [])
                if str(alias or "").strip()
            ) if isinstance(person.get("aliases"), list) else str(person.get("aliases") or "")
            haystack = " ".join(
                str(part or "")
                for part in (
                    person.get("user_id"),
                    person.get("display_name"),
                    person.get("nickname"),
                    alias_text,
                    person.get("scope_key"),
                )
            ).casefold()
            if search not in haystack:
                continue
        filtered.append(item)

    reverse = sort_order == "desc"

    def sort_key(item: Mapping[str, Any]) -> tuple:
        nested_person = item.get("person") if isinstance(item.get("person"), Mapping) else None
        person = dict(nested_person) if nested_person is not None else item
        name = str(person.get("display_name") or "").casefold()
        if sort_by == "interactions":
            count = _interaction_count(person)
            return ((count is None, count if count is not None else 0), name)
        if sort_by == "affinity":
            affinity = _relationship_affinity(item if nested_person is not None else ((relationship_lookup or {}).get(str(person.get("user_id") or "")) or item))
            return ((affinity is None, affinity if affinity is not None else 0.0), name)
        return (name, str(person.get("user_id") or ""))

    filtered.sort(key=sort_key, reverse=reverse)
    return filtered


@people_bp.route("/people/legacy/audit", methods=["GET"])
@require_auth
async def list_legacy_people_audit():
    """按 legacy (bot_id, group_id) 查看人物；不声称具有 canonical SessionRef。"""
    try:
        limit, offset = _page_args()
        conn = _connection()
        if conn is None:
            return jsonify(error_payload("service_unavailable", "People store is unavailable", retryable=True)), 503
        if not _table_exists(conn, "user_profiles"):
            payload = page_response([], total=0, limit=limit, offset=offset)
            payload.update({"legacy": True, "readonly": True, "scope_status": "legacy_group_key"})
            return jsonify(payload)
        bot_id = str(request.args.get("bot_id") or "").strip()
        group_id = str(request.args.get("group_id") or "").strip()
        search = str(request.args.get("search") or "").strip().casefold()
        registry = _registry_by_principal(conn)
        items = []
        where_parts: list[str] = []
        params: list[Any] = []
        if bot_id:
            where_parts.append("bot_id=?")
            params.append(bot_id)
        if group_id:
            where_parts.append("group_id=?")
            params.append(group_id)
        for profile in _table_rows(conn, "user_profiles", " AND ".join(where_parts), tuple(params)):
            user_id = str(profile.get("user_id") or "").strip()
            if not user_id:
                continue
            person = registry.get(user_id, {})
            item = dict(profile)
            item.pop("affection", None)
            item.update({
                "display_name": person.get("display_name") or item.get("nickname") or user_id,
                "aliases": person.get("aliases", []),
                "metadata": _json(item.get("metadata"), {}),
                "legacy": True,
                "readonly": True,
                "scope": None,
                "scope_status": "legacy_group_key",
                "scope_reason": "canonical_platform_and_session_unavailable",
                "affinity": None,
                "affinity_status": "unavailable",
                "affinity_reason_code": "scoped_affinity_projection_unavailable",
                "object_ref": None,
                "actions": {},
            })
            if search and not any(
                search in str(item.get(field, "")).casefold()
                for field in ("user_id", "display_name", "nickname", "aliases", "bot_id", "group_id")
            ):
                continue
            items.append(item)
        items.sort(key=lambda item: (
            str(item.get("bot_id") or ""), str(item.get("group_id") or ""),
            str(item.get("display_name") or "").casefold(), str(item.get("user_id") or ""),
        ))
        total = len(items)
        payload = page_response(items[offset:offset + limit], total=total, limit=limit, offset=offset)
        payload.update({
            "legacy": True,
            "readonly": True,
            "scope": None,
            "scope_status": "legacy_group_key",
            "reason_code": "canonical_platform_and_session_unavailable",
        })
        return jsonify(payload)
    except (TypeError, ValueError):
        return jsonify(error_payload("invalid_pagination", "Invalid pagination parameters")), 400
    except Exception:
        return jsonify(error_payload("service_unavailable", "Legacy people audit is unavailable", retryable=True)), 503


@people_bp.route("/people/legacy/relationships", methods=["GET"])
@require_auth
async def list_legacy_relationships_audit():
    """只读 legacy relationship events；筛选键只用于审计，不构造 RuntimeScope。"""
    try:
        limit, offset = _page_args()
        conn = _connection()
        if conn is None:
            return jsonify(error_payload("service_unavailable", "Relationship store is unavailable", retryable=True)), 503
        if not _table_exists(conn, "relationship_events"):
            payload = page_response([], total=0, limit=limit, offset=offset)
            payload.update({"legacy": True, "readonly": True, "scope": None, "scope_status": "legacy_group_key"})
            return jsonify(payload)
        bot_id = str(request.args.get("bot_id") or "").strip()
        group_id = str(request.args.get("group_id") or request.args.get("session_id") or "").strip()
        user_id = str(request.args.get("user_id") or "").strip()
        search = str(request.args.get("search") or "").strip()
        where = ["1=1"]
        params: list[Any] = []
        for column, value in (("bot_id", bot_id), ("group_id", group_id), ("user_id", user_id)):
            if value:
                where.append(f"{column}=?")
                params.append(value)
        if search:
            where.append("(user_id LIKE ? OR event_type LIKE ? OR dimension LIKE ? OR reason LIKE ?)")
            params.extend([f"%{search}%"] * 4)
        where_sql = " AND ".join(where)
        total = int(conn.execute(
            f"SELECT COUNT(*) FROM relationship_events WHERE {where_sql}", params
        ).fetchone()[0])
        cursor = conn.execute(
            "SELECT id,bot_id,group_id,user_id,event_type,dimension,delta,reason,"
            "source_episode_id,source_memory_id,created_at FROM relationship_events "
            f"WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        )
        names = [str(column[0]) for column in cursor.description or ()]
        items = []
        for row in cursor.fetchall():
            item = dict(zip(names, row))
            item.update({
                "legacy": True,
                "readonly": True,
                "scope": None,
                "scope_status": "legacy_group_key",
                "scope_reason": "canonical_platform_and_session_unavailable",
                "object_ref": None,
                "actions": {},
            })
            items.append(item)
        payload = page_response(items, total=total, limit=limit, offset=offset)
        payload.update({
            "legacy": True,
            "readonly": True,
            "scope": None,
            "scope_status": "legacy_group_key",
            "reason_code": "canonical_platform_and_session_unavailable",
        })
        return jsonify(payload)
    except (TypeError, ValueError):
        return jsonify(error_payload("invalid_pagination", "Invalid pagination parameters")), 400
    except Exception:
        return jsonify(error_payload("service_unavailable", "Legacy relationship audit is unavailable", retryable=True)), 503


def _object_refs():
    try:
        return current_app.extensions.get("wave_api_contract", {}).get("object_refs")
    except RuntimeError:
        return None


def _relationship_gateway(container):
    configured = getattr(container, "relationship_calibration", None)
    if configured is not None:
        return configured
    write_gateway = getattr(container, "write_gateway", None)
    repository = getattr(container, "soul_repository", None) or getattr(getattr(container, "db", None), "soul_repository", None)
    if write_gateway is None or repository is None:
        return None
    try:
        from ...services.relationship_calibration import RelationshipCalibrationGateway
    except ImportError:  # pragma: no cover
        from services.relationship_calibration import RelationshipCalibrationGateway
    try:
        configured = RelationshipCalibrationGateway(write_gateway, repository)
    except (TypeError, ValueError):
        return None
    container.relationship_calibration = configured
    return configured


def _relationship_error(exc: Exception):
    code = getattr(exc, "reason_code", None) or getattr(exc, "code", None) or "relationship_calibration_failed"
    status = 409 if code in {"relationship_revision_conflict", "relationship_manual_layer_unavailable"} else 404 if code in {"object_ref_not_found", "relationship_unknown"} else 422
    return jsonify(error_payload(str(code), str(exc))), status


def _formal_evidence_summaries(relationship: Any) -> list[str]:
    """Extract historical_audit_summary texts from formal relationship.evidence."""
    if not isinstance(relationship, dict):
        return []
    try:
        from ...services.relationship_evidence_display import extract_historical_audit_summaries
    except ImportError:  # pragma: no cover
        from services.relationship_evidence_display import extract_historical_audit_summaries
    return extract_historical_audit_summaries(relationship.get("evidence"), max_items=3)


def _historical_audit_summary_for_subject(repository: Any, scope: RuntimeScope, subject: str) -> dict[str, Any]:
    """Read-only historical audit side-channel; never mutates formal affinity."""
    if repository is None or not hasattr(repository, "list_legacy_relationship_audit_summary"):
        return {
            "available": False,
            "total": 0,
            "by_type": [],
            "recent": [],
            "readonly": True,
            "affects_affinity": False,
        }
    try:
        subject_scope = RuntimeScope(
            bot_id=scope.bot_id,
            visibility=scope.visibility,
            session=scope.session,
            subject_principal_id=subject,
        )
        summary = repository.list_legacy_relationship_audit_summary(
            subject_scope,
            recent_limit=5,
        )
        if isinstance(summary, dict) and summary.get("available"):
            return {
                **summary,
                "readonly": True,
                "affects_affinity": False,
                "source_table": "scoped_soul_relationship_legacy_events",
            }
        if hasattr(repository, "list_relationship_history"):
            history = repository.list_relationship_history(
                subject_scope,
                subject_principal_id=subject,
                limit=25,
                offset=0,
            )
            items = list(history.get("items") or [])
            counts: dict[str, int] = {}
            recent = []
            for item in items[:5]:
                event_type = str(item.get("event_type") or item.get("kind") or "event")
                counts[event_type] = counts.get(event_type, 0) + 1
                recent.append({
                    "event_type": event_type,
                    "dimension": str(item.get("dimension") or ""),
                    "delta": item.get("delta"),
                    "reason": str(item.get("reason") or ""),
                    "occurred_at": item.get("timestamp"),
                    "legacy_event_id": str(item.get("id") or ""),
                })
            return {
                "available": bool(items),
                "total": int(history.get("total") or len(items)),
                "by_type": [{"event_type": key, "count": value} for key, value in counts.items()],
                "recent": recent,
                "readonly": True,
                "affects_affinity": False,
                "source_table": "scoped_soul_relationship_events",
            }
        return {
            "available": False,
            "total": 0,
            "by_type": [],
            "recent": [],
            "readonly": True,
            "affects_affinity": False,
            "source_table": "scoped_soul_relationship_legacy_events",
        }
    except Exception:
        return {
            "available": False,
            "total": 0,
            "by_type": [],
            "recent": [],
            "readonly": True,
            "affects_affinity": False,
            "reason_code": "historical_audit_query_failed",
        }


@people_bp.route("/people/relationships", methods=["GET"])
@require_auth
async def list_relationships():
    try:
        scope = _request_scope()
        if scope is None or scope.session is None or scope.visibility != "group":
            return jsonify(error_payload("scope_required", "A complete group RuntimeScope is required")), 400
        repository = getattr(get_container(), "soul_repository", None)
        if repository is None:
            return jsonify(error_payload("relationship_repository_unavailable", "Scoped relationship repository is unavailable", retryable=True)), 503
        limit, offset = _page_args()
        include_historical_audit = str(
            request.args.get("include_historical_audit") or ""
        ).strip().lower() in {"1", "true", "yes", "on"}
        profiles = []
        conn = _connection()
        if conn is not None and _table_exists(conn, "user_profiles"):
            registry = _registry_by_principal(conn)
            for profile in _table_rows(
                conn,
                "user_profiles",
                "bot_id=? AND group_id=?",
                (scope.bot_id, scope.session.conversation_id),
            ):
                item = _profile_item(profile, registry, scope)
                if item is not None:
                    profiles.append(item)
        relationship_rows = _relationship_rows_for_scope(
            repository,
            scope,
            _alias_session_ids(conn, scope),
        )
        refs = _object_refs()
        items = []
        for profile in profiles:
            subject = f"{scope.session.platform_id}:user:{profile['user_id']}"
            relationship = relationship_rows.get(subject)
            if relationship is None:
                item = {"subject_principal_id": subject, "person": profile, "affinity": None, "state": "unknown", "revision": None, "values": None, "evidence": [], "evidence_summaries": [], "object_ref": None, "calibration": {"available": False, "reason_code": "relationship_unknown"}}
            else:
                item = {"subject_principal_id": subject, "person": profile, **relationship}
                item["evidence_summaries"] = _formal_evidence_summaries(relationship)
                if refs is not None:
                    ref = refs.issue(kind="relationship", locator=subject, scope=scope, revision=int(relationship["revision"]))
                    item["object_ref"] = {"ref": ref, "kind": "relationship", "locator": subject, "scope_key": scope.session.id, "version": int(relationship["revision"])}
            if include_historical_audit:
                item["historical_audit"] = _historical_audit_summary_for_subject(
                    repository, scope, subject
                )
            items.append(item)
        items = _filter_people_rows(items)
        total = len(items)
        return jsonify({
            **page_response(items[offset:offset + limit], total=total, limit=limit, offset=offset),
            "scope": scope.to_dict(),
            "historical_audit_mode": "included" if include_historical_audit else "omitted",
        })
    except (TypeError, ValueError) as exc:
        return jsonify(error_payload("invalid_relationship_query", str(exc))), 400
    except Exception as exc:
        return jsonify(error_payload("relationship_query_unavailable", str(exc), retryable=True)), 503


@people_bp.route("/people/relationships/historical-audit", methods=["GET"])
@require_auth
async def get_relationship_historical_audit():
    """Scoped formal historical audit summary/list (readonly, never changes affinity)."""
    try:
        scope = _request_scope()
        if scope is None or scope.session is None or scope.visibility != "group":
            return jsonify(error_payload("scope_required", "A complete group RuntimeScope is required")), 400
        repository = getattr(get_container(), "soul_repository", None)
        if repository is None:
            return jsonify(error_payload(
                "relationship_repository_unavailable",
                "Scoped relationship repository is unavailable",
                retryable=True,
            )), 503

        subject = str(request.args.get("subject_principal_id") or "").strip()
        user_id = str(request.args.get("user_id") or "").strip()
        if not subject and user_id:
            subject = f"{scope.session.platform_id}:user:{user_id}"
        if not subject:
            return jsonify(error_payload(
                "subject_required",
                "subject_principal_id or user_id is required",
            )), 400

        summary = _historical_audit_summary_for_subject(repository, scope, subject)
        limit, offset = _page_args()
        items: list[dict[str, Any]] = []
        total = int(summary.get("total") or 0)
        conn = _connection()
        if (
            conn is not None
            and _table_exists(conn, "scoped_soul_relationship_legacy_events")
            and total > 0
        ):
            cursor = conn.execute(
                """SELECT id, legacy_event_id, bot_id, session_id, visibility, group_id,
                          subject_principal_id, event_type, dimension, delta, reason,
                          occurred_at, source_episode_id, source_memory_id, created_at
                     FROM scoped_soul_relationship_legacy_events
                    WHERE bot_id=? AND session_id=? AND visibility=? AND subject_principal_id=?
                    ORDER BY COALESCE(occurred_at, 0) DESC, id DESC
                    LIMIT ? OFFSET ?""",
                (
                    scope.bot_id,
                    scope.session.id,
                    scope.visibility,
                    subject,
                    limit,
                    offset,
                ),
            )
            names = [str(column[0]) for column in cursor.description or ()]
            for row in cursor.fetchall():
                item = dict(zip(names, row))
                item.update({
                    "readonly": True,
                    "affects_affinity": False,
                    "source": "scoped_soul_relationship_legacy_events",
                })
                items.append(item)

        payload = page_response(items, total=total, limit=limit, offset=offset)
        payload.update({
            "scope": scope.to_dict(),
            "subject_principal_id": subject,
            "summary": summary,
            "readonly": True,
            "affects_affinity": False,
            "legacy": False,
            "historical_audit": True,
        })
        return jsonify(payload)
    except (TypeError, ValueError) as exc:
        return jsonify(error_payload("invalid_historical_audit_query", str(exc))), 400
    except Exception as exc:
        return jsonify(error_payload(
            "historical_audit_unavailable",
            str(exc),
            retryable=True,
        )), 503


@people_bp.route("/people/relationships/commands/calibrate", methods=["POST"])
@require_auth
async def calibrate_relationship():
    scope = _request_scope()
    if scope is None or scope.session is None or scope.visibility != "group":
        return jsonify(error_payload("scope_required", "A complete group RuntimeScope is required")), 400
    container = get_container()
    gateway = _relationship_gateway(container)
    if gateway is None:
        return jsonify(error_payload("relationship_calibration_unavailable", "Relationship calibration is unavailable", retryable=True)), 503
    body = await request.get_json(silent=True) or {}
    refs = _object_refs()
    try:
        descriptor = body.get("object_ref") or body.get("ref")
        ref = descriptor.get("ref") if isinstance(descriptor, dict) else descriptor
        binding, state = refs.resolve_with_state(ref, kind="relationship", request_scope=scope) if refs is not None else (None, "not-found")
        if binding is None or state != "ready":
            raise RelationshipCalibrationError("object_ref_not_found")
        expected_revision = int(body.get("revision"))
        if int(binding.revision) != expected_revision:
            raise RelationshipCalibrationError("relationship_revision_conflict")
        subject = str(binding.locator)
        if not subject.startswith(f"{scope.session.platform_id}:user:"):
            raise RelationshipCalibrationError("scope_subject_mismatch")
        target_scope = RuntimeScope(scope.bot_id, scope.visibility, scope.session, subject)
        result = await gateway.calibrate(
            scope=target_scope,
            subject_principal_id=subject,
            expected_revision=expected_revision,
            action=body.get("action"),
            dimension=body.get("dimension"),
            delta=body.get("delta"),
            value=body.get("value"),
            reason=body.get("reason"),
            evidence=resolve_relationship_evidence(
                _connection(),
                scope=target_scope,
                values=body.get("evidence"),
            ),
            object_ref=str(ref),
        )
        return jsonify(mutation_response(operation_kind="relationship.calibrate", operation_id=result.operation_id, status=result.status, revision=result.revision, item={"calibration_id": result.calibration_id, "subject_principal_id": result.subject_principal_id, "dimension": result.dimension, "action": result.action, "before": result.before, "after": result.after, "affinity": result.affinity, "state": result.state, "evidence": result.evidence}, include_item=True))
    except RelationshipCalibrationError as exc:
        return _relationship_error(exc)
    except EvidenceResolutionError as exc:
        return jsonify(error_payload(exc.code, str(exc))), 422
    except (TypeError, ValueError) as exc:
        return jsonify(error_payload("relationship_request_invalid", str(exc))), 422


def _clear_impression_on_connection(conn: Any, *, scope: RuntimeScope, user_id: str, reason: str) -> dict[str, Any]:
    if scope.session is None or scope.visibility != "group":
        raise ValueError("scope_required")
    user_id = str(user_id or "").strip()
    reason = str(reason or "").strip()
    if not user_id:
        raise ValueError("user_id_required")
    if len(reason) < 4:
        raise ValueError("impression_clear_reason_required")
    if not _table_exists(conn, "user_profiles"):
        raise LookupError("person_not_found")
    row = conn.execute(
        "SELECT metadata FROM user_profiles WHERE user_id=? AND group_id=? AND bot_id=?",
        (user_id, scope.session.conversation_id, scope.bot_id),
    ).fetchone()
    if row is None:
        raise LookupError("person_not_found")
    metadata = _json(row[0], {})
    if not isinstance(metadata, dict):
        metadata = {}
    previous = str(metadata.get("impression") or "").strip()
    if not previous:
        raise LookupError("impression_not_found")
    now = time.time()
    try:
        from ...services.impression_timeline import clear_impression as _append_clear
    except ImportError:  # pragma: no cover
        from services.impression_timeline import clear_impression as _append_clear
    metadata = _append_clear(metadata, reason=reason, now=now, actor="webui")
    conn.execute(
        "UPDATE user_profiles SET metadata=? WHERE user_id=? AND group_id=? AND bot_id=?",
        (json.dumps(metadata, ensure_ascii=False), user_id, scope.session.conversation_id, scope.bot_id),
    )
    conn.commit()
    return {
        "user_id": user_id,
        "group_id": scope.session.conversation_id,
        "bot_id": scope.bot_id,
        "cleared": True,
        "previous_impression": previous,
        "reason": reason,
        "impression_updated_at": now,
    }


@people_bp.route("/people/commands/clear-impression", methods=["POST"])
@require_auth
async def clear_impression():
    """Clear the current Bot impression for one person in the current group, with audit."""
    scope = _request_scope()
    if scope is None or scope.session is None or scope.visibility != "group":
        return jsonify(error_payload("scope_required", "A complete group RuntimeScope is required")), 400
    conn = _connection()
    if conn is None:
        return jsonify(error_payload("service_unavailable", "People store is unavailable", retryable=True)), 503
    body = await request.get_json(silent=True) or {}
    try:
        item = _clear_impression_on_connection(
            conn,
            scope=scope,
            user_id=str(body.get("user_id") or request.args.get("user_id") or ""),
            reason=str(body.get("reason") or ""),
        )
        return jsonify(mutation_response(
            operation_kind="people.impression.clear",
            operation_id=f"{scope.bot_id}:{scope.session.conversation_id}:{item['user_id']}:{int(item['impression_updated_at'] * 1000)}",
            status="succeeded",
            revision=int(item["impression_updated_at"] * 1000),
            item=item,
            include_item=True,
        ))
    except LookupError as exc:
        code = str(exc)
        status = 404
        return jsonify(error_payload(code, str(exc))), status
    except ValueError as exc:
        return jsonify(error_payload(str(exc), str(exc))), 422


@people_bp.route("/people", methods=["GET"])
@require_auth
async def list_people():
    try:
        limit, offset = _page_args()
        conn = _connection()
        if conn is None:
            return jsonify(error_payload("service_unavailable", "People store is unavailable", retryable=True)), 503
        if not _table_exists(conn, "user_profiles"):
            return jsonify(page_response([], total=0, limit=limit, offset=offset))

        scope = _request_scope()
        if scope is None or scope.session is None or scope.visibility != "group":
            return jsonify(error_payload("scope_required", "A complete request Scope is required")), 400
        registry = _registry_by_principal(conn)
        items = [
            item
            for profile in _table_rows(
                conn,
                "user_profiles",
                "bot_id=? AND group_id=?",
                (scope.bot_id, scope.session.conversation_id),
            )
            if (item := _profile_item(profile, registry, scope)) is not None
        ]
        relationship_lookup: dict[str, Mapping[str, Any]] = {}
        repository = getattr(get_container(), "soul_repository", None)
        if repository is not None:
            for row in _relationship_rows_for_scope(repository, scope, _alias_session_ids(conn, scope)).values():
                subject = str(row.get("subject_principal_id") or "")
                user_id = subject.rsplit(":user:", 1)[-1] if ":user:" in subject else ""
                if user_id:
                    relationship_lookup[user_id] = row
            for item in items:
                rel = relationship_lookup.get(str(item.get("user_id") or ""))
                if rel is None:
                    continue
                affinity = _relationship_affinity(rel)
                item["affinity"] = affinity
                item["affinity_status"] = "available" if affinity is not None else item.get("affinity_status")
                if affinity is not None:
                    item["affinity_reason_code"] = "scoped_relationship"
        items = _filter_people_rows(items, relationship_lookup=relationship_lookup)
        total = len(items)
        return jsonify(page_response(items[offset:offset + limit], total=total, limit=limit, offset=offset))
    except (TypeError, ValueError):
        return jsonify(error_payload("invalid_pagination", "Invalid pagination parameters")), 400
    except Exception:
        return jsonify(error_payload("service_unavailable", "People store is unavailable", retryable=True)), 503


__all__ = ["people_bp"]
