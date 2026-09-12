"""规范 Knowledge API：BookLore、scoped Facts 与正式 FewShot 只读投影。"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from quart import Blueprint, current_app, jsonify, request

try:
    from domain.scope import CatalogScope
    from engine.external_book_lore import ExternalBookLoreError, ExternalBookLoreStore
    from services.identity_safety import is_identity_contamination
except ImportError:  # pragma: no cover - AstrBot package import path
    from ...domain.scope import CatalogScope
    from ...engine.external_book_lore import ExternalBookLoreError, ExternalBookLoreStore
    from ...services.identity_safety import is_identity_contamination

from ..api_contract import current_runtime_scope, error_payload, not_found_payload, page_response
from ..container import get_container
from ..facts_evidence import fact_evidence
from ..middleware.auth import require_auth

knowledge_bp = Blueprint("knowledge", __name__, url_prefix="/api")
_BOOK_LORE_RESOURCES = frozenset({"entities", "communities", "relations", "notes"})
_UNSAFE_STYLE_RE = re.compile(
    r"(怼回去|狠狠怼|别客气|骂回去|反击|嘴臭|阴阳怪气|傻逼|脑残|滚|nmsl|你妈|操你|fuck\s*you)",
    re.IGNORECASE,
)


def _page_args(*, max_limit: int = 200) -> tuple[int, int]:
    limit = max(1, min(max_limit, int(request.args.get("limit", request.args.get("size", 25)))))
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


def _table_exists(conn: Any, table: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone() is not None


def _columns(conn: Any, table: str) -> set[str]:
    if not _table_exists(conn, table):
        return set()
    return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}


def _row_dicts(cursor: Any) -> list[dict[str, Any]]:
    names = [str(column[0]) for column in cursor.description or ()]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def _json_value(value: Any, default: Any) -> Any:
    if not isinstance(value, str):
        return value if value is not None else default
    try:
        return json.loads(value)
    except (TypeError, ValueError, json.JSONDecodeError):
        return default


def _config_section(container: Any, name: str) -> Mapping[str, Any]:
    config = getattr(container, "plugin_config", None)
    if not isinstance(config, Mapping):
        return {}
    section = config.get(name)
    return section if isinstance(section, Mapping) else {}


def _book_lore_path(container: Any) -> Path | None:
    for name in ("book_lore_db_path", "lore_db_path"):
        value = getattr(container, name, None)
        if value is not None and str(value).strip():
            return Path(str(value)).expanduser().resolve()

    config = getattr(container, "plugin_config", None)
    if isinstance(config, Mapping):
        values: list[Any] = [config.get("book_lore_db_path"), config.get("lore_db_path")]
        section = config.get("BookLore_Settings")
        if isinstance(section, Mapping):
            values.extend((section.get("book_lore_db_path"), section.get("lore_db_path")))
        for value in values:
            if value is not None and str(value).strip():
                return Path(str(value)).expanduser().resolve()

    db_path = getattr(getattr(container, "db", None), "db_path", None)
    if db_path is None:
        db_path = getattr(getattr(getattr(container, "db", None), "cm", None), "db_path", None)
    if db_path is not None and str(db_path).strip():
        sibling = Path(str(db_path)).expanduser().resolve().with_name("book_lore.db")
        if sibling.is_file():
            return sibling
    return None


def _catalog_scope(container: Any) -> CatalogScope:
    catalog_id = str(request.args.get("catalog_id") or "book-lore").strip()
    corpus_id = str(request.args.get("corpus_id") or "default").strip()
    version = str(request.args.get("version") or "current").strip()
    return CatalogScope(catalog_id=catalog_id, corpus_id=corpus_id, version=version)


def _book_lore_store() -> tuple[ExternalBookLoreStore, CatalogScope]:
    container = get_container()
    path = _book_lore_path(container)
    if path is None:
        raise ExternalBookLoreError("book_lore_database_unavailable")
    return ExternalBookLoreStore(path), _catalog_scope(container)


def _book_lore_error(exc: Exception):
    code = str(exc) or "book_lore_database_unavailable"
    status = 400 if "scope" in code or "required" in code and "path" not in code else 503
    return jsonify(error_payload(code, "BookLore read-only source is unavailable", retryable=status == 503)), status


def _content_projection(item: Mapping[str, Any]) -> dict[str, Any]:
    """保留 raw 列，同时稳定暴露原文、本地化、翻译与治理状态。"""
    result = dict(item)

    def first(*names: str) -> Any:
        for name in names:
            if name in item:
                return item.get(name)
        return None

    result.setdefault("original", first("original_text", "raw_text", "source_text", "content", "summary", "description"))
    result.setdefault("localized", first("localized_text", "localized_content", "localized_summary", "localized_title"))
    result.setdefault("translation", first("translation_text", "translated_text", "translation"))
    result.setdefault("resolution", first("resolution", "resolution_state"))
    result.setdefault("quarantine", first("quarantine", "quarantined"))
    return result


@knowledge_bp.route("/knowledge/book-lore/summary", methods=["GET"])
@require_auth
async def get_book_lore_summary():
    try:
        store, scope = _book_lore_store()
        return jsonify({
            "scope": scope.to_dict(),
            "counts": store.counts(scope=scope),
            "schema": store.schema_info(scope=scope),
            "read_only": True,
        })
    except (ExternalBookLoreError, TypeError, ValueError) as exc:
        return _book_lore_error(exc)


@knowledge_bp.route("/knowledge/book-lore/<resource>", methods=["GET"])
@require_auth
async def list_book_lore(resource: str):
    if resource not in _BOOK_LORE_RESOURCES:
        return jsonify(not_found_payload()), 404
    try:
        limit, offset = _page_args()
        store, scope = _book_lore_store()
        method = getattr(store, f"list_{resource}")
        payload = method(
            scope=scope,
            limit=limit,
            offset=offset,
            search=str(request.args.get("search") or "").strip(),
            sort=str(request.args.get("sort") or "id"),
            filter=str(request.args.get("filter") or "").strip(),
        )
        items = [_content_projection(item) for item in payload["items"]]
        response = page_response(items, total=int(payload["total"]), limit=limit, offset=offset)
        response["scope"] = scope.to_dict()
        response["resource"] = resource
        response["read_only"] = True
        return jsonify(response)
    except (TypeError, ValueError):
        return jsonify(error_payload("invalid_pagination", "Invalid pagination parameters")), 400
    except ExternalBookLoreError as exc:
        return _book_lore_error(exc)


def _requested_runtime_scope():
    """只返回请求显式携带且通过统一 provider 校验的 group RuntimeScope。"""
    try:
        provider = current_app.extensions.get("wave_api_contract", {}).get("request_scope_provider")
    except RuntimeError:
        provider = None
    scope = current_runtime_scope(provider)
    if scope is None or scope.session is None or scope.visibility != "group":
        return None
    return scope


def _requested_fact_scope() -> tuple[str, str, str] | None:
    """Facts 只使用统一 request Scope，不能从裸 ID 推断授权范围。"""
    scope = _requested_runtime_scope()
    if scope is None:
        return None
    return scope.bot_id, scope.session.id, scope.visibility


def _fact_evidence(conn: Any, items: list[dict[str, Any]], scope: tuple[str, str, str]) -> None:
    """兼容入口：实现已收敛到 webui/facts_evidence.py，避免两份证据判定逻辑漂移。"""
    fact_evidence(conn, items, scope)


async def _list_scoped_facts(*, compatibility_empty: bool = False):
    try:
        limit, offset = _page_args(max_limit=500)
        scope = _requested_fact_scope()
        if scope is None:
            if compatibility_empty:
                return jsonify(page_response([], total=0, limit=limit, offset=offset))
            return jsonify(error_payload("scope_required", "bot_id and canonical session_id are required")), 400
        conn = _connection()
        if conn is None:
            return jsonify(error_payload("service_unavailable", "Knowledge store is unavailable", retryable=True)), 503
        columns = _columns(conn, "scoped_facts")
        required = {"bot_id", "session_id", "visibility"}
        if not required <= columns:
            return jsonify(page_response([], total=0, limit=limit, offset=offset))

        where = ["bot_id=?", "session_id=?", "visibility=?"]
        params: list[Any] = list(scope)
        search = str(request.args.get("search") or "").strip()
        searchable = [name for name in ("subject", "predicate", "object") if name in columns]
        if search and searchable:
            where.append("(" + " OR ".join(f'"{name}" LIKE ?' for name in searchable) + ")")
            params.extend([f"%{search}%"] * len(searchable))
        status = str(request.args.get("status") or "").strip()
        if status and "status" in columns:
            where.append("status=?")
            params.append(status)
        elif "status" in columns:
            where.append("status NOT IN ('deleted','superseded')")
        where_sql = " WHERE " + " AND ".join(where)
        total = int(conn.execute("SELECT COUNT(*) FROM scoped_facts" + where_sql, params).fetchone()[0])
        order = "updated_at DESC, id DESC" if {"updated_at", "id"} <= columns else "id DESC"
        items = _row_dicts(conn.execute(
            f"SELECT * FROM scoped_facts{where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ))
        _fact_evidence(conn, items, scope)
        response = page_response(items, total=total, limit=limit, offset=offset)
        response["scope"] = {"bot_id": scope[0], "session_id": scope[1], "visibility": scope[2]}
        return jsonify(response)
    except (TypeError, ValueError):
        return jsonify(error_payload("invalid_pagination", "Invalid pagination parameters")), 400
    except Exception:
        return jsonify(error_payload("service_unavailable", "Knowledge store is unavailable", retryable=True)), 503


@knowledge_bp.route("/knowledge/facts", methods=["GET"])
@require_auth
async def list_facts():
    return await _list_scoped_facts()


@knowledge_bp.route("/knowledge/facts/legacy/audit", methods=["GET"])
@require_auth
async def list_legacy_facts_audit():
    """只读 legacy facts；缺少稳定 Bot/Session 时不伪装为正式 scoped Fact。"""
    try:
        limit, offset = _page_args(max_limit=500)
        conn = _connection()
        if conn is None:
            return jsonify(error_payload("service_unavailable", "Knowledge store is unavailable", retryable=True)), 503
        columns = _columns(conn, "facts")
        if not {"id", "subject", "predicate", "object"} <= columns:
            payload = page_response([], total=0, limit=limit, offset=offset)
            payload.update({"legacy": True, "readonly": True, "scope": None,
                            "scope_status": "unresolved_legacy"})
            return jsonify(payload)
        where = ["1=1"]
        params: list[Any] = []
        search = str(request.args.get("search") or "").strip()
        if search:
            where.append("(subject LIKE ? OR predicate LIKE ? OR object LIKE ?)")
            params.extend([f"%{search}%"] * 3)
        group_id = str(request.args.get("group_id") or "").strip()
        if group_id and "group_id" in columns:
            where.append("group_id=?")
            params.append(group_id)
        where_sql = " AND ".join(where)
        total = int(conn.execute(f"SELECT COUNT(*) FROM facts WHERE {where_sql}", params).fetchone()[0])
        selected = [name for name in (
            "id", "subject", "predicate", "object", "group_id", "source_memory_id",
            "confidence", "valid_from", "valid_until", "created_at", "last_reinforced", "fact_type",
        ) if name in columns]
        rows = _row_dicts(conn.execute(
            f"SELECT {', '.join(selected)} FROM facts WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ))
        for item in rows:
            item.update({
                "legacy": True,
                "readonly": True,
                "scope": None,
                "scope_status": "unresolved_legacy",
                "scope_reason": "bot_and_canonical_session_unavailable",
                "evidence_status": "unavailable",
                "object_ref": None,
                "actions": {},
            })
        payload = page_response(rows, total=total, limit=limit, offset=offset)
        payload.update({
            "legacy": True,
            "readonly": True,
            "scope": None,
            "scope_status": "unresolved_legacy",
            "reason_code": "bot_and_canonical_session_unavailable",
        })
        return jsonify(payload)
    except (TypeError, ValueError):
        return jsonify(error_payload("invalid_pagination", "Invalid pagination parameters")), 400
    except Exception:
        return jsonify(error_payload("service_unavailable", "Legacy facts audit is unavailable", retryable=True)), 503


def _healthy_few_shot(content: Any) -> bool:
    text = str(content or "").strip()
    if not text:
        return False
    return not is_identity_contamination(text) and _UNSAFE_STYLE_RE.search(text) is None


def _serialize_domain(value: Any) -> Any:
    if hasattr(value, "to_dict") and callable(value.to_dict):
        return value.to_dict()
    if isinstance(value, tuple):
        return [_serialize_domain(item) for item in value]
    if isinstance(value, list):
        return [_serialize_domain(item) for item in value]
    if isinstance(value, Mapping):
        return {str(key): _serialize_domain(item) for key, item in value.items()}
    return value


def _few_shot_repository():
    return getattr(get_container(), "fewshot_repository", None)


def _serialize_few_shot(item: Mapping[str, Any]) -> dict[str, Any]:
    scope = item.get("scope")
    session = getattr(scope, "session", None)
    return {
        "id": int(item["id"]),
        "content": str(item.get("content") or ""),
        "score": float(item.get("score") or 0.0),
        "traits": list(item.get("traits") or ()),
        "status": "approved",
        "health": "healthy",
        "revision": int(item.get("revision") or 1),
        "bot_id": str(getattr(scope, "bot_id", "") or ""),
        "session_id": str(getattr(session, "id", "") or ""),
        "visibility": str(getattr(scope, "visibility", "") or ""),
        "scope": _serialize_domain(scope),
        "candidate": _serialize_domain(item.get("candidate") or {}),
        "evidence": _serialize_domain(item.get("evidence_refs") or ()),
        "source_candidate_id": item.get("source_candidate_id"),
        "created_at": item.get("created_at"),
        "updated_at": item.get("updated_at"),
        "approved_at": item.get("approved_at"),
    }


def _reject_nonformal_few_shot_filter():
    status = str(request.args.get("status") or "approved").strip().lower()
    health = str(request.args.get("health") or "healthy").strip().lower()
    if status != "approved" or health != "healthy":
        return jsonify(not_found_payload()), 404
    return None


@knowledge_bp.route("/knowledge/few-shot", methods=["GET"])
@require_auth
async def list_few_shot():
    rejected = _reject_nonformal_few_shot_filter()
    if rejected is not None:
        return rejected
    try:
        limit, offset = _page_args(max_limit=500)
        scope = _requested_runtime_scope()
        if scope is None:
            return jsonify(error_payload("scope_required", "bot_id and canonical session_id are required")), 400
        if _connection() is None:
            return jsonify(error_payload("service_unavailable", "FewShot store is unavailable", retryable=True)), 503
        repository = _few_shot_repository()
        if repository is None:
            return jsonify(error_payload("service_unavailable", "Scoped FewShot repository is unavailable", retryable=True)), 503
        search = str(request.args.get("search") or "").strip()
        rows = repository.list_approved(scope=scope, limit=limit, offset=offset, search=search)
        total = repository.count_approved(scope=scope, search=search)
        payload = page_response([_serialize_few_shot(item) for item in rows if _healthy_few_shot(item.get("content"))], total=total, limit=limit, offset=offset)
        payload["scope"] = scope.to_dict()
        payload["source"] = "scoped_few_shot_examples"
        return jsonify(payload)
    except (TypeError, ValueError):
        return jsonify(error_payload("invalid_request", "Invalid scoped FewShot query")), 400
    except Exception:
        return jsonify(error_payload("service_unavailable", "FewShot store is unavailable", retryable=True)), 503


@knowledge_bp.route("/knowledge/few-shot/<int:item_id>", methods=["GET"])
@require_auth
async def get_few_shot(item_id: int):
    rejected = _reject_nonformal_few_shot_filter()
    if rejected is not None:
        return rejected
    scope = _requested_runtime_scope()
    if scope is None:
        return jsonify(error_payload("scope_required", "bot_id and canonical session_id are required")), 400
    if _connection() is None:
        return jsonify(error_payload("service_unavailable", "FewShot store is unavailable", retryable=True)), 503
    repository = _few_shot_repository()
    if repository is None:
        return jsonify(error_payload("service_unavailable", "Scoped FewShot repository is unavailable", retryable=True)), 503
    try:
        item = repository.get(item_id)
    except Exception:
        item = None
    if item is None or item.get("scope") != scope or item.get("status") != "approved" or not _healthy_few_shot(item.get("content")):
        return jsonify(not_found_payload()), 404
    return jsonify(_serialize_few_shot(item))


@knowledge_bp.route("/knowledge/experiences", methods=["GET"])
@require_auth
async def list_experiences():
    """只读检索历史经历片段 (experience_episodes)。"""
    try:
        limit, offset = _page_args(max_limit=200)
        conn = _connection()
        if conn is None:
            return jsonify(error_payload("service_unavailable", "Experience store is unavailable", retryable=True)), 503
        if not _table_exists(conn, "experience_episodes"):
            return jsonify(page_response([], total=0, limit=limit, offset=offset))

        columns = _columns(conn, "experience_episodes")
        where = ["1=1"]
        params: list[Any] = []

        bot_id = str(request.args.get("bot_id") or "").strip()
        group_id = str(request.args.get("group_id") or "").strip()
        if not bot_id or not group_id:
            return jsonify(page_response([], total=0, limit=limit, offset=offset))
        if "bot_id" in columns:
            where.append("bot_id=?")
            params.append(bot_id)
        if "group_id" in columns:
            where.append("group_id=?")
            params.append(group_id)

        search = str(request.args.get("search") or "").strip()
        if search:
            searchable = [
                c for c in (
                    "trigger_text", "bot_inner_thought", "bot_action",
                    "bot_reply", "user_reaction", "outcome", "episode_type",
                ) if c in columns
            ]
            if searchable:
                where.append("(" + " OR ".join(f'"{c}" LIKE ?' for c in searchable) + ")")
                params.extend([f"%{search}%"] * len(searchable))

        episode_type = str(request.args.get("episode_type") or "").strip()
        if episode_type and "episode_type" in columns:
            where.append("episode_type=?")
            params.append(episode_type)

        min_weight = request.args.get("min_emotional_weight")
        if min_weight is not None and "emotional_weight" in columns:
            try:
                where.append("emotional_weight >= ?")
                params.append(float(min_weight))
            except ValueError:
                pass

        where_sql = " WHERE " + " AND ".join(where)
        total = int(conn.execute("SELECT COUNT(*) FROM experience_episodes" + where_sql, params).fetchone()[0])
        order = "created_at DESC, id DESC" if "created_at" in columns else "id DESC"
        items = _row_dicts(conn.execute(
            f"SELECT * FROM experience_episodes{where_sql} ORDER BY {order} LIMIT ? OFFSET ?",
            (*params, limit, offset),
        ))
        return jsonify(page_response(items, total=total, limit=limit, offset=offset))
    except (TypeError, ValueError):
        return jsonify(error_payload("invalid_pagination", "Invalid pagination parameters")), 400
    except Exception as exc:
        return jsonify(error_payload("service_unavailable", str(exc), retryable=True)), 503


__all__ = ["knowledge_bp"]
