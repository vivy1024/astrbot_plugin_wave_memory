"""Runtime v1 API: 供 Cortico 等外部应用安全复用 WaveMemory 认知服务。"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from pathlib import Path
from typing import Any
from quart import Blueprint, current_app, jsonify, request

try:
    from ...domain.scope import CatalogScope, RuntimeScope, SessionRef
except ImportError:
    from domain.scope import CatalogScope, RuntimeScope, SessionRef

logger = logging.getLogger(__name__)

runtime_bp = Blueprint("runtime_v1", __name__, url_prefix="/api/runtime/v1")

TOKEN_ENV = "WAVEMEMORY_RUNTIME_TOKEN"
DEFAULT_DEV_TOKEN = "yushu-dev-token"


def _check_auth() -> bool:
    expected = os.environ.get(TOKEN_ENV, DEFAULT_DEV_TOKEN)
    auth = request.headers.get("Authorization", "")
    if auth.startswith("Bearer "):
        token = auth[7:].strip()
    else:
        token = auth.strip()
    return bool(expected and token == expected)


def _normalize_uid(raw_uid: Any) -> str:
    """归一化用户标识符（支持纯数字、bili:123、qq:123 或全称 URI）。"""
    t = str(raw_uid or "").strip()
    if not t:
        return ""
    if ":" in t:
        # 处理 bilibili:user:123456 或 qq:user:123456
        parts = t.split(":")
        if len(parts) >= 3 and parts[1] == "user":
            return f"{parts[0]}:{parts[2]}"
        return t
    return t


def _bot_registry() -> Any:
    return getattr(_get_container(), "bot_registry", None)


def _resolve_bot_profile(scope_dict: dict[str, Any]) -> Any:
    """按请求里的 bot_id 或 Cortico 部署名找到 Bot；找不到直接拒绝，不再默认落到某个 Bot 名下。"""
    bot_id = str(scope_dict.get("bot_id") or "").strip()
    deployment = str(scope_dict.get("deployment") or "").strip()
    if not deployment:
        try:
            deployment = str(request.headers.get("X-Cortico-Deployment") or "").strip()
        except RuntimeError:  # 不在请求上下文（单测直接调用）
            deployment = ""
    registry = _bot_registry()
    if registry is None or not hasattr(registry, "resolve"):
        if not bot_id:
            raise ValueError("bot_required: scope.bot_id is required")
        return None
    profile = registry.resolve(bot_id=bot_id, deployment=deployment)
    if profile is None:
        target = bot_id or (f"deployment={deployment}" if deployment else "")
        raise ValueError(f"unknown_bot: no enabled bot matches {target or '(empty)'}")
    return profile


def _default_session(profile: Any) -> dict[str, Any]:
    """请求没带会话时，用 Bot 绑定的直播间作为默认会话。"""
    for binding in getattr(profile, "bindings", None) or ():
        if getattr(binding, "host", "") == "bilibili" and getattr(binding, "room", ""):
            room = binding.room
            return {"id": f"bilibili:group:{room}", "platform_id": "bilibili", "kind": "group", "conversation_id": room}
    raise ValueError("session_required: scope.session is required (bot has no bilibili binding)")


def _parse_runtime_scope(scope_dict: dict[str, Any]) -> RuntimeScope:
    profile = _resolve_bot_profile(scope_dict)
    bot_id = profile.db_id if profile is not None else str(scope_dict.get("bot_id")).strip()
    session_data = scope_dict.get("session") or {}
    if not session_data:
        session_data = _default_session(profile)
    session_id = str(session_data.get("id") or "").strip()
    if not session_id:
        raise ValueError("session_required: scope.session.id is required")
    parts = session_id.split(":", 2)
    session_ref = SessionRef(
        id=session_id,
        platform_id=str(session_data.get("platform_id") or (parts[0] if len(parts) == 3 else "")),
        kind=str(session_data.get("kind") or (parts[1] if len(parts) == 3 else "group")),
        conversation_id=str(session_data.get("conversation_id") or (parts[2] if len(parts) == 3 else "")),
    )
    subject = scope_dict.get("subject_principal_id")
    if subject:
        sub_str = str(subject).strip()
        prefix = f"{session_ref.platform_id}:user:"
        if not sub_str.startswith(prefix):
            # 补齐标准前缀
            raw_id = sub_str.split(":")[-1]
            subject = f"{prefix}{raw_id}"
        else:
            subject = sub_str
    return RuntimeScope(
        bot_id=bot_id,
        visibility=str(scope_dict.get("visibility") or session_ref.kind or "group"),
        session=session_ref,
        subject_principal_id=subject,
    )


def _get_container() -> Any:
    # 优先从 current_app.extensions 获取，其次调用单例 get_container()
    try:
        from ..container import get_container
        return get_container()
    except Exception:
        pass
    try:
        from container import get_container
        return get_container()
    except Exception:
        pass
    return getattr(current_app, "extensions", {}).get("wave_api_contract", {}).get("container", None)


def _get_book_lore_service():
    try:
        from ...services.book_lore_search import BookLoreSearchService
    except ImportError:
        try:
            from services.book_lore_search import BookLoreSearchService
        except ImportError:
            from ...services.book_lore_search import BookLoreSearchService

    container = _get_container()
    database_path = getattr(getattr(container, "db", None), "db_path", None)
    lore_db_path = None
    for name in ("book_lore_db_path", "lore_db_path"):
        val = getattr(container, name, None)
        if val:
            lore_db_path = str(val)
            break
    if not lore_db_path and database_path:
        sibling = Path(database_path).expanduser().resolve().with_name("book_lore.db")
        if sibling.is_file():
            lore_db_path = str(sibling)

    # 尝试检查工作区常见路径
    if not lore_db_path:
        for cand in [
            Path("data/book_lore.db"),
            Path("data/plugins/astrbot_plugin_wave_memory/data/book_lore.db"),
            Path("astrbot_plugin_wave_memory/data/book_lore.db"),
        ]:
            if cand.is_file():
                lore_db_path = str(cand.resolve())
                break

    return BookLoreSearchService(
        lore_db_path=lore_db_path or "",
        book_lore_index=getattr(container, "book_lore_index", None),
        embedding_service=getattr(container, "embedding_service", None),
    )


@runtime_bp.route("/capabilities", methods=["GET"])
async def capabilities():
    """Runtime 能力清单。``?bot_id=`` 时工具列表按该 Bot 的工具开关过滤。"""
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401
    container = _get_container()
    registry = getattr(container, "tool_registry", None)
    profile = None
    bot_id = str(request.args.get("bot_id") or "").strip()
    bots = _bot_registry()
    if bot_id and bots is not None and hasattr(bots, "get"):
        profile = bots.get(bot_id)
    tools = registry.describe(profile=profile, runtime_only=True) if registry is not None else []
    channels = [getattr(ch, "name", "") for ch in (getattr(container, "injection_channels", None) or [])]
    try:
        from ...utils.build_info import build_info
        build = build_info()
    except Exception:
        build = {}
    return jsonify({
        "ok": True,
        "protocol_version": "1.1",
        "build": build,
        "capabilities": {
            "book_lore_search": True,
            "book_lore_graph": True,
            "context_prepare": getattr(container, "runtime_context_preparer", None) is not None,
            "observations_batch": True,
            "commands": True,
            "tools": registry is not None,
        },
        "supported_channels": [name for name in channels if name],
        "tiers": ["full", "light", "minimal"],
        "tools": [tool for tool in tools if tool["enabled"]],
        # 调用方据此拼出与 AstrBot 路径一致的会话 id（如 QQ 群用 <session_prefix>:group:<群号>）。
        "bot": {
            "db_id": profile.db_id,
            "name": profile.name,
            "session_prefix": profile.session_prefix,
            "self_ids": profile.self_ids,
        } if profile is not None else None,
    })


@runtime_bp.route("/tools/<name>", methods=["POST"])
async def invoke_tool(name: str):
    """用请求里的作用域调用与 AstrBot 相同的工具实例。请求体：``scope``、``arguments``。"""
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401
    container = _get_container()
    registry = getattr(container, "tool_registry", None)
    if registry is None:
        return jsonify({"ok": False, "error": {"code": "tools_unavailable", "message": "工具注册表未就绪"}}), 503
    body = await request.get_json(silent=True) or {}
    try:
        scope = _parse_runtime_scope(dict(body.get("scope") or {}))
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_scope", "message": str(exc)}}), 400
    bots = _bot_registry()
    profile = bots.get(scope.bot_id) if bots is not None and hasattr(bots, "get") else None
    arguments = body.get("arguments") if isinstance(body.get("arguments"), dict) else {}
    try:
        from ...services.tool_registry import ToolRegistryError
    except ImportError:
        from services.tool_registry import ToolRegistryError
    started = time.perf_counter()
    try:
        result = await registry.invoke(name, scope=scope, arguments=arguments, profile=profile, source=str(body.get("source") or "cortico"))
    except ToolRegistryError as exc:
        status = {"tool_not_found": 404, "tool_disabled": 403, "tool_denied": 403}.get(exc.code, 400)
        return jsonify({"ok": False, "error": {"code": exc.code, "message": str(exc)}}), status
    except Exception as exc:
        logger.warning(f"[Runtime API] tool {name} failed: {exc}")
        return jsonify({"ok": False, "error": {"code": "tool_failed", "message": str(exc)}}), 500
    return jsonify({
        "ok": True,
        "tool": name,
        "result": result,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
    })


@runtime_bp.route("/book-lore/search", methods=["POST"])
async def book_lore_search():
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401

    body = await request.get_json() or {}
    query = str(body.get("query") or "").strip()
    mode = str(body.get("mode") or "hybrid").strip()
    top_k = int(body.get("top_k") or 5)
    resources = body.get("resources") or ["entities", "communities", "notes"]

    scope_dict = body.get("scope") or {}
    catalog_scope = CatalogScope(
        catalog_id=str(scope_dict.get("catalog_id") or "book-lore"),
        corpus_id=str(scope_dict.get("corpus_id") or "default"),
        version=str(scope_dict.get("version") or "current"),
    )

    try:
        service = _get_book_lore_service()
        res = await service.search(
            query=query,
            scope=catalog_scope,
            mode=mode,
            resources=resources,
            top_k=top_k,
        )
        return jsonify({
            "ok": True,
            "request_id": body.get("request_id") or "req-search",
            "data": res,
        })
    except Exception as exc:
        logger.warning(f"[Runtime API] book-lore search error: {exc}")
        return jsonify({
            "ok": False,
            "request_id": body.get("request_id") or "req-search",
            "error": {"code": "search_error", "message": str(exc)},
        }), 500


@runtime_bp.route("/book-lore/graph", methods=["POST"])
async def book_lore_graph():
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401

    body = await request.get_json() or {}
    entity_name = str(body.get("entity_name") or "").strip()
    depth = int(body.get("depth") or 1)
    max_edges = int(body.get("max_edges") or 20)

    scope_dict = body.get("scope") or {}
    catalog_scope = CatalogScope(
        catalog_id=str(scope_dict.get("catalog_id") or "book-lore"),
        corpus_id=str(scope_dict.get("corpus_id") or "default"),
        version=str(scope_dict.get("version") or "current"),
    )

    try:
        service = _get_book_lore_service()
        res = await service.graph_traverse(
            entity_name=entity_name,
            scope=catalog_scope,
            depth=depth,
            max_edges=max_edges,
        )
        return jsonify({
            "ok": True,
            "request_id": body.get("request_id") or "req-graph",
            "data": res,
        })
    except Exception as exc:
        logger.warning(f"[Runtime API] book-lore graph error: {exc}")
        return jsonify({
            "ok": False,
            "request_id": body.get("request_id") or "req-graph",
            "error": {"code": "graph_error", "message": str(exc)},
        }), 500


@runtime_bp.route("/observations/batch", methods=["POST"])
async def observations_batch():
    """被动事件摄入：QQ 群消息、弹幕、Bot 自己说的话，走与 AstrBot 消息钩子相同的处理。

    每个事件：``event_id``（平台原始消息号，用于去重）、``kind``（message/danmaku/self）、
    ``content``、``sender_id``、``sender_name``、``timestamp``、``scope``、可选 ``group_name``、
    ``is_at_bot``。逐条返回回执；任何一条失败不影响其他条。
    """
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401

    body = await request.get_json(silent=True) or {}
    events = body.get("events") or []
    if not isinstance(events, list):
        return jsonify({"ok": False, "error": {"code": "invalid_body", "message": "events must be an array"}}), 400
    if len(events) > 500:
        return jsonify({"ok": False, "error": {"code": "batch_too_large", "message": "at most 500 events per batch"}}), 400

    container = _get_container()
    ingest = getattr(container, "observation_ingestor", None)
    if not callable(ingest):
        # 真实服务不可用，返回 503 绝不报虚假 committed
        return jsonify({
            "ok": False,
            "error": {"code": "service_unavailable", "message": "WaveMemory 写入流程未就绪"},
        }), 503

    results = []
    for ev in events:
        if not isinstance(ev, dict):
            results.append({"event_id": None, "status": "rejected", "error": "event_must_be_object"})
            continue
        eid = ev.get("event_id")
        if eid in (None, ""):
            results.append({"event_id": None, "status": "rejected", "error": "event_id_required"})
            continue
        scope_data = dict(ev.get("scope") or {})
        sender_id = str(ev.get("sender_id") or "").strip()
        if sender_id and not scope_data.get("subject_principal_id") and str(ev.get("kind") or "message") != "self":
            scope_data["subject_principal_id"] = sender_id
        try:
            scope = _parse_runtime_scope(scope_data)
        except Exception as exc:
            results.append({"event_id": eid, "status": "rejected", "error": f"invalid_scope: {exc}"})
            continue
        try:
            receipt = await ingest(ev, scope)
        except Exception as exc:
            logger.warning(f"[Runtime API] observation {eid} failed: {exc}")
            receipt = {"status": "rejected", "error": f"ingest_failed: {exc}"}
        results.append({"event_id": eid, **receipt})

    accepted = sum(1 for item in results if item.get("status") == "accepted")
    return jsonify({
        "ok": True,
        "batch_id": body.get("batch_id") or f"batch-{time.time()}",
        "accepted": accepted,
        "rejected": len(results) - accepted,
        "receipts": results,
    })


@runtime_bp.route("/commands", methods=["POST"])
async def commands():
    """主动认知命令：处理事实提审、经历记录等操作。"""
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401

    body = await request.get_json() or {}
    cmd = str(body.get("command") or "").strip()
    operation_key = str(body.get("operation_key") or f"op-{time.time()}").strip()
    args = body.get("arguments") or {}
    scope_data = body.get("scope") or {}

    try:
        scope = _parse_runtime_scope(scope_data)
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_scope", "message": str(exc)}}), 400

    container = _get_container()

    if cmd == "propose_fact":
        subject = str(args.get("subject") or "").strip()
        predicate = str(args.get("predicate") or "").strip()
        obj = str(args.get("object") or "").strip()
        quote = str(args.get("source_quote") or "").strip()

        if not subject or not predicate or not obj:
            return jsonify({"ok": False, "error": {"code": "invalid_arguments", "message": "subject, predicate, object are required"}}), 400

        db = getattr(container, "db", None)
        repo = getattr(db, "scoped_knowledge", None)
        if repo is None:
            return jsonify({
                "ok": False,
                "error": {"code": "service_unavailable", "message": "ScopedKnowledge repository is not ready in container"},
            }), 503

        try:
            fid = repo.upsert_scoped_fact(
                scope=scope,
                subject=subject,
                predicate=predicate,
                object=obj,
                confidence=0.85,
                status="pending",
                provenance={"source_quote": quote, "operation_key": operation_key},
            )
            return jsonify({
                "ok": True,
                "operation_id": operation_key,
                "data": {
                    "command": "propose_fact",
                    "entity": {"kind": "fact", "id": str(fid), "revision": 1},
                    "transport_state": "committed",
                    "domain_state": "pending",
                    "projection_state": "not_applicable",
                    "evidence_status": "quote_attached" if quote else "missing_quote",
                    "duplicate": False,
                },
            })
        except Exception as exc:
            return jsonify({"ok": False, "error": {"code": "database_error", "message": str(exc)}}), 500

    elif cmd == "note_episode":
        episode_type = str(args.get("episode_type") or "live_milestone").strip()
        trigger_text = str(args.get("trigger_text") or "").strip()
        outcome = str(args.get("outcome") or "completed").strip()

        gateway = getattr(container, "write_gateway", None)
        if gateway is None or not hasattr(gateway, "record_episode"):
            return jsonify({
                "ok": False,
                "error": {"code": "service_unavailable", "message": "ProductionWriteGateway service is not ready in container"},
            }), 503

        try:
            eid = await gateway.record_episode(
                scope=scope,
                group_id=scope.session.conversation_id,
                user_id=str(args.get("user_id") or scope.bot_id),
                episode_type=episode_type,
                fields={"trigger_text": trigger_text, "outcome": outcome},
                idempotency_hint=operation_key,
            )
            return jsonify({
                "ok": True,
                "operation_id": operation_key,
                "data": {
                    "command": "note_episode",
                    "entity": {"kind": "episode", "id": str(eid), "revision": 1},
                    "transport_state": "committed",
                    "domain_state": "active",
                    "projection_state": "ready",
                    "duplicate": False,
                },
            })
        except Exception as exc:
            return jsonify({"ok": False, "error": {"code": "gateway_error", "message": str(exc)}}), 500

    return jsonify({"ok": False, "error": {"code": "unknown_command", "message": f"Unsupported command: {cmd}"}}), 400


# ─── 收敛 8030 旧桥：直接在 9876 上提供 /inject 与 /context/prepare 记忆拼装 ───

def _extract_keywords(text: str, limit: int = 4) -> list[str]:
    candidates = re.findall(r"[\w\u4e00-\u9fff]{2,}", text or "")
    words: list[str] = []
    stopwords = {
        "saved the game", "已安静", "新事件", "socketclosed", "tick", "平原", "草方块",
        "主世界", "现在是", "看不见", "水面", "实心山体", "水下", "上浮", "找岸", "氧气",
        "我的世界", "服务器", "系统", "测试", "饥饿", "生命", "站着", "没动", "面朝",
        "腐肉", "僵尸", "走进了", "夜幕降临", "深夜", "黄昏", "上午", "怪物", "这个", "那个",
    }
    for c in candidates:
        w = str(c or "").strip()
        if len(w) >= 2 and w.lower() not in stopwords and w not in words:
            words.append(w)
        if len(words) >= limit:
            break
    return words


@runtime_bp.route("/context/prepare", methods=["POST"])
@runtime_bp.route("/inject", methods=["POST"])
async def context_prepare():
    """外部应用的记忆注入：与 AstrBot 走同一个编排器、同一套通道、同一份通道配置。

    请求体：``text``、``speaker{id,name}``/``uid``、``tier``（full/light/minimal）、
    ``recent``（可选，最近几条对话文本）、``channels``（可选，只跑这些通道）、
    ``source``（写进 trace 的来源标签，默认 cortico）、``dry_run``。
    """
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401

    started = time.perf_counter()
    body = await request.get_json(silent=True) or {}
    text = str(body.get("text") or "").strip()
    speaker = body.get("speaker") if isinstance(body.get("speaker"), dict) else {}
    tier = str(body.get("tier") or "light").strip().lower()
    source = re.sub(r"[^a-z0-9_.-]", "", str(body.get("source") or "cortico").strip().lower())[:32] or "cortico"

    scope_data = dict(body.get("scope") or {})
    speaker_id = str(body.get("uid") or speaker.get("id") or "").strip()
    speaker_name = str(speaker.get("name") or "").strip()
    if speaker_id and not scope_data.get("subject_principal_id"):
        scope_data["subject_principal_id"] = speaker_id
    try:
        scope = _parse_runtime_scope(scope_data)
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_scope", "message": str(exc)}}), 400

    container = _get_container()
    preparer = getattr(container, "runtime_context_preparer", None)
    if preparer is None or not preparer.available():
        return jsonify({
            "ok": False,
            "error": {"code": "injection_unavailable", "message": "注入编排器未就绪（检查 Channel_Settings 与启动日志）"},
        }), 503

    try:
        from ...services.injection.runtime_prepare import channel_stats, persona_lore_block
    except ImportError:
        from services.injection.runtime_prepare import channel_stats, persona_lore_block

    recent = body.get("recent")
    recent_context = [str(item) for item in recent] if isinstance(recent, list) else None
    requested_channels = body.get("channels")
    channel_filter = [str(item) for item in requested_channels] if isinstance(requested_channels, list) else None
    try:
        result = await preparer.prepare(
            scope=scope,
            message=text,
            sender_id=speaker_id,
            sender_name=speaker_name,
            tier=tier,
            recent_context=recent_context,
            source=source,
            dry_run=bool(body.get("dry_run")),
            channel_filter=channel_filter,
        )
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_scope", "message": str(exc)}}), 400
    except Exception as exc:
        logger.warning(f"[Runtime API] context prepare failed: {exc}")
        return jsonify({"ok": False, "error": {"code": "injection_failed", "message": str(exc)}}), 500

    registry = _bot_registry()
    profile = registry.get(scope.bot_id) if registry is not None and hasattr(registry, "get") else None
    parts = [block for block in (persona_lore_block(profile) if tier != "minimal" else "", result.final_text) if block]
    final_block = "\n\n".join(parts)
    return jsonify({
        "ok": True,
        "block": final_block,
        "chars": len(final_block),
        "tier": tier,
        "trace_id": result.trace_id,
        "elapsed_ms": round((time.perf_counter() - started) * 1000, 1),
        "channels": channel_stats(result),
    })


# ─── 多用户（UID）隔离记忆与画像管理接口 ───

@runtime_bp.route("/memories/store", methods=["POST"])
async def memory_store():
    """为指定 UID 存入专属记忆（支持即时入队与长久归档）。"""
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401

    body = await request.get_json() or {}
    uid = _normalize_uid(body.get("uid") or "anon")
    content = str(body.get("content") or "").strip()
    if not content:
        return jsonify({"ok": False, "error": {"code": "empty_content", "message": "content is required"}}), 400

    scope_data = body.get("scope") or {}
    scope_data["subject_principal_id"] = uid
    try:
        scope = _parse_runtime_scope(scope_data)
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_scope", "message": str(exc)}}), 400

    container = _get_container()
    writer = getattr(container, "writer", None)
    if writer is None or not hasattr(writer, "enqueue"):
        return jsonify({"ok": False, "error": {"code": "service_unavailable", "message": "MessageWriter not ready"}}), 503

    eid = str(body.get("event_id") or f"mem-{uid}-{int(time.time() * 1000)}")
    try:
        await writer.enqueue({
            "group_id": scope.session.conversation_id,
            "content": content,
            "sender_id": uid,
            "sender_name": str(body.get("sender_name") or uid),
            "timestamp": float(body.get("timestamp") or time.time()),
            "importance": float(body.get("importance") or 1.0),
            "source": str(body.get("source") or "user_direct"),
            "scope": scope,
            "event_id": eid,
        })
        return jsonify({
            "ok": True,
            "data": {
                "event_id": eid,
                "uid": uid,
                "status": "enqueued",
                "scope": scope.session.id,
            },
        })
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "enqueue_error", "message": str(exc)}}), 500


@runtime_bp.route("/memories/query", methods=["POST"])
async def memory_query():
    """按 UID 查询专属或关联记忆（支持 uid_only 严格隔离选项）。"""
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401

    body = await request.get_json() or {}
    uid = _normalize_uid(body.get("uid") or "")
    q = str(body.get("query") or "").strip()
    limit = max(1, min(int(body.get("limit") or 5), 20))
    uid_only = bool(body.get("uid_only", False))

    scope_data = body.get("scope") or {}
    try:
        scope = _parse_runtime_scope(scope_data)
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_scope", "message": str(exc)}}), 400

    container = _get_container()
    db = getattr(container, "db", None)
    if db is None:
        return jsonify({"ok": False, "error": {"code": "database_unavailable", "message": "DB not ready"}}), 503

    where_clauses = ["bot_id = ?", "resolution_state = 'resolved'"]
    params: list[Any] = [scope.bot_id]

    if uid_only:
        if not uid:
            return jsonify({"ok": False, "error": {"code": "missing_uid", "message": "uid is required when uid_only is true"}}), 400
        where_clauses.append("sender_id = ?")
        params.append(uid)
    elif uid:
        where_clauses.append("(sender_id = ? OR visibility = 'group')")
        params.append(uid)

    # v6：关键词走 FTS5 全文索引（与注入的 fts5 通道同一套分词），不再对 4 GB 的记忆表做 LIKE 全表扫描。
    match_expr = ""
    if q:
        try:
            from ...services.injection.channels.fts5 import _keywords, _match_expr
        except ImportError:
            from services.injection.channels.fts5 import _keywords, _match_expr
        try:
            from ...engine.db import fts_cjk
        except ImportError:
            from engine.db import fts_cjk
        fts_table = "fts_memories"
        if fts_cjk.is_ready(db.conn):
            # 中文全文索引（两字片段）就绪后优先用它：「张羽」能命中「张羽师兄」。
            fts_table = fts_cjk.TABLE
            match_expr = fts_cjk.match_expr(_keywords(q))
        else:
            words_expr = _match_expr(_keywords(q))
            # 只匹配正文列：fts_memories 还索引了发言人昵称，不限定列时会把"叫这个名字的人说的任何话"也搜出来。
            match_expr = f"content : ({words_expr})" if words_expr else ""
        if not match_expr:
            return jsonify({"ok": False, "error": {"code": "query_too_short", "message": "query has no usable keywords (need 2+ characters)"}}), 400
    elif not uid:
        return jsonify({"ok": False, "error": {"code": "missing_query", "message": "query or uid is required"}}), 400

    qualified = [clause.replace("bot_id", "m.bot_id").replace("resolution_state", "m.resolution_state")
                 .replace("sender_id", "m.sender_id").replace("visibility", "m.visibility") for clause in where_clauses]
    if match_expr:
        sql = f"""SELECT m.id, m.content, m.sender_id, m.sender_name, m.timestamp, m.importance
                  FROM {fts_table} JOIN memories AS m ON m.id = {fts_table}.rowid
                  WHERE {fts_table} MATCH ? AND {' AND '.join(qualified)}
                  ORDER BY bm25({fts_table}), m.timestamp DESC LIMIT ?"""
        params = [match_expr, *params, limit]
    else:
        sql = f"""SELECT m.id, m.content, m.sender_id, m.sender_name, m.timestamp, m.importance
                  FROM memories AS m
                  WHERE {' AND '.join(qualified)}
                  ORDER BY m.timestamp DESC LIMIT ?"""
        params.append(limit)

    try:
        rows = db.conn.execute(sql, tuple(params)).fetchall()
        items = []
        for r in rows:
            items.append({
                "id": r[0],
                "content": str(r[1] or ""),
                "sender_id": str(r[2] or ""),
                "sender_name": str(r[3] or ""),
                "timestamp": float(r[4] or 0),
                "importance": float(r[5] or 1.0),
            })
        return jsonify({"ok": True, "data": {"items": items, "count": len(items), "uid": uid}})
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "query_error", "message": str(exc)}}), 500


@runtime_bp.route("/memories/forget", methods=["POST"])
async def memory_forget():
    """安全遗忘/清理指定 UID 的记忆（软删除，保证合规与数据控制权）。"""
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401

    body = await request.get_json() or {}
    uid = _normalize_uid(body.get("uid") or "")
    if not uid:
        return jsonify({"ok": False, "error": {"code": "missing_uid", "message": "uid is required for forget operation"}}), 400

    memory_id = body.get("memory_id")
    forget_all = bool(body.get("all", False))

    container = _get_container()
    db = getattr(container, "db", None)
    if db is None:
        return jsonify({"ok": False, "error": {"code": "database_unavailable", "message": "DB not ready"}}), 503

    try:
        if memory_id is not None:
            # 单条遗忘：必须严格匹配 sender_id=uid，防止越权删除他人记忆
            cursor = db.conn.execute(
                "UPDATE memories SET resolution_state = 'deleted' WHERE id = ? AND sender_id = ?",
                (int(memory_id), uid),
            )
            db.conn.commit()
            aff = cursor.rowcount
        elif forget_all:
            # 全量遗忘该用户的全部记忆
            cursor = db.conn.execute(
                "UPDATE memories SET resolution_state = 'deleted' WHERE sender_id = ?",
                (uid,),
            )
            db.conn.commit()
            aff = cursor.rowcount
        else:
            return jsonify({"ok": False, "error": {"code": "missing_target", "message": "specify memory_id or all=true"}}), 400

        return jsonify({"ok": True, "data": {"affected": aff, "uid": uid, "status": "deleted"}})
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "forget_error", "message": str(exc)}}), 500


@runtime_bp.route("/users/<path:raw_uid>/profile", methods=["GET", "POST"])
async def user_profile(raw_uid: str):
    """读取或更新特定 UID 的社交账本画像（支持五维属性与好感度）。"""
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401

    uid = _normalize_uid(raw_uid)
    container = _get_container()
    db = getattr(container, "db", None)
    if db is None:
        return jsonify({"ok": False, "error": {"code": "database_unavailable", "message": "DB not ready"}}), 503

    body = (await request.get_json(silent=True) or {}) if request.method == "POST" else {}
    try:
        profile = _resolve_bot_profile({
            "bot_id": request.args.get("bot_id") or body.get("bot_id"),
            "deployment": request.args.get("deployment") or body.get("deployment"),
        })
    except ValueError as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_scope", "message": str(exc)}}), 400
    bot_id = profile.db_id if profile is not None else str(request.args.get("bot_id") or body.get("bot_id")).strip()

    if request.method == "GET":
        row = db.conn.execute(
            "SELECT user_id, nickname, affection, interaction_count, last_seen, metadata FROM user_profiles WHERE user_id = ? AND bot_id = ? LIMIT 1",
            (uid, bot_id),
        ).fetchone()
        if not row:
            return jsonify({
                "ok": True,
                "data": {
                    "uid": uid,
                    "exists": False,
                    "message": "首次记录，暂无档案",
                }
            })
        meta = json.loads(row[5]) if row[5] else {}
        return jsonify({
            "ok": True,
            "data": {
                "uid": row[0],
                "exists": True,
                "nickname": row[1] or "",
                "affection": row[2] or 0,
                "interaction_count": row[3] or 0,
                "last_seen": row[4],
                "dimensions": meta.get("dimensions", {}),
            }
        })

    # POST 更新画像
    nickname = str(body.get("nickname") or "").strip()
    dimensions = body.get("dimensions")
    affection_delta = int(body.get("affection_delta") or 0)

    try:
        row = db.conn.execute(
            "SELECT id, metadata, affection, interaction_count FROM user_profiles WHERE user_id = ? AND bot_id = ? LIMIT 1",
            (uid, bot_id),
        ).fetchone()

        now = time.time()
        if row:
            pid, meta_raw, aff, cnt = row
            meta = json.loads(meta_raw) if meta_raw else {}
            if isinstance(dimensions, dict):
                meta.setdefault("dimensions", {}).update(dimensions)
            new_aff = (aff or 0) + affection_delta
            new_cnt = (cnt or 0) + 1
            db.conn.execute(
                """UPDATE user_profiles 
                   SET nickname = COALESCE(NULLIF(?, ''), nickname),
                       affection = ?, interaction_count = ?, last_seen = ?, metadata = ?
                   WHERE id = ?""",
                (nickname, new_aff, new_cnt, now, json.dumps(meta, ensure_ascii=False), pid),
            )
        else:
            meta = {"dimensions": dimensions} if isinstance(dimensions, dict) else {}
            db.conn.execute(
                """INSERT INTO user_profiles (user_id, group_id, bot_id, nickname, affection, interaction_count, first_seen, last_seen, metadata)
                   VALUES (?, 'live_room', ?, ?, ?, 1, ?, ?, ?)""",
                (uid, bot_id, nickname or uid, affection_delta, now, now, json.dumps(meta, ensure_ascii=False)),
            )
        db.conn.commit()
        return jsonify({"ok": True, "data": {"uid": uid, "status": "updated"}})
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "update_error", "message": str(exc)}}), 500

