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


def _parse_runtime_scope(scope_dict: dict[str, Any]) -> RuntimeScope:
    session_data = scope_dict.get("session") or {}
    session_ref = SessionRef(
        id=str(session_data.get("id") or "bilibili:group:24292304"),
        platform_id=str(session_data.get("platform_id") or "bilibili"),
        kind=str(session_data.get("kind") or "group"),
        conversation_id=str(session_data.get("conversation_id") or "24292304"),
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
        bot_id=str(scope_dict.get("bot_id") or "yushu"),
        visibility=str(scope_dict.get("visibility") or "group"),
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
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401
    return jsonify({
        "ok": True,
        "protocol_version": "1.0",
        "capabilities": {
            "book_lore_search": True,
            "book_lore_graph": True,
            "context_prepare": True,
            "observations_batch": True,
            "commands": True,
        },
        "supported_channels": [
            "soul_state", "relationship", "facts", "book_lore", "memory"
        ],
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
    """被动事件摄入：将观众弹幕与主播实际播出片段异步真实入库。"""
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401

    body = await request.get_json() or {}
    events = body.get("events") or []
    if not isinstance(events, list):
        return jsonify({"ok": False, "error": {"code": "invalid_body", "message": "events must be an array"}}), 400

    container = _get_container()
    writer = getattr(container, "writer", None)

    results = []
    for ev in events:
        eid = ev.get("event_id") or f"ev-{time.time()}"
        content = str(ev.get("content") or "").strip()
        sender_id = str(ev.get("sender_id") or "anon")
        sender_name = str(ev.get("sender_name") or "观众")
        scope_data = ev.get("scope") or {}

        try:
            scope = _parse_runtime_scope(scope_data)
        except Exception as exc:
            results.append({"event_id": eid, "status": "rejected", "error": f"invalid_scope: {exc}"})
            continue

        if not content:
            results.append({"event_id": eid, "status": "rejected", "error": "empty_content"})
            continue

        if writer is not None and hasattr(writer, "enqueue"):
            try:
                await writer.enqueue({
                    "group_id": scope.session.conversation_id,
                    "content": content,
                    "sender_id": sender_id,
                    "sender_name": sender_name,
                    "timestamp": ev.get("timestamp") or time.time(),
                    "source": "live_observation",
                    "scope": scope,
                    "event_id": eid,
                })
                results.append({"event_id": eid, "status": "accepted", "delivery_state": "inbox_enqueued"})
            except Exception as exc:
                results.append({"event_id": eid, "status": "rejected", "error": f"enqueue_failed: {exc}"})
        else:
            # 真实服务不可用，返回 503 绝不报虚假 committed
            return jsonify({
                "ok": False,
                "error": {"code": "service_unavailable", "message": "MessageWriter service is not ready in container"},
            }), 503

    return jsonify({
        "ok": True,
        "batch_id": body.get("batch_id") or f"batch-{time.time()}",
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
                user_id=str(args.get("user_id") or "yushu"),
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
    """在 9876 核心上组装当前轮次的记忆注入块（彻底替代 8030 临时进程）。"""
    if not _check_auth():
        return jsonify({"ok": False, "error": {"code": "unauthorized", "message": "Invalid runtime bearer token"}}), 401

    body = await request.get_json() or {}
    text = str(body.get("text") or "").strip()
    speaker = body.get("speaker") if isinstance(body.get("speaker"), dict) else {}
    tier = str(body.get("tier") or "light").strip().lower()
    limit = max(1, min(int(body.get("limit") or 5), 10))

    scope_data = body.get("scope") or {}
    try:
        scope = _parse_runtime_scope(scope_data)
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "invalid_scope", "message": str(exc)}}), 400

    container = _get_container()
    db = getattr(container, "db", None)

    parts: list[str] = []
    channels_stat: dict[str, Any] = {}

    # 1. [世界观书设]
    core_lore = [
        "出处与本体：你来自修真小说《没钱修什么仙》（作者：熊狼狗），是主角张羽脑内的外挂账本系统与高维修仙器灵；你清楚知道自己出自该书的设定体系。",
        "核心世界观：修仙界残酷现实，财侣法地'没钱修什么仙'——功法要交学费、灵石灵币昂贵，修仙者也得打工求生、抵押贷款；时刻算计资源成本，绝不当被宗门大能割的韭菜。",
        "主播心态底色：嘴硬心软的高维器灵，玩游戏时自然流露精打细算、珍惜资源、吐槽修仙黑幕的鲜明特色，但不生硬背诵书设名词。",
    ]
    parts.append("[世界观书设：羽书出自《没钱修什么仙》，下列设定作为你的灵魂底色与认知边界]\n" + "\n".join(f"- {l}" for l in core_lore))
    channels_stat["book_lore"] = {"status": "hit", "items": len(core_lore)}

    # 2. [当前 Soul 状态]
    soul_lines = []
    if db is not None:
        try:
            mood_row = db.conn.execute(
                "SELECT valence, arousal, cause FROM soul_mood WHERE bot_id=? ORDER BY updated_at DESC LIMIT 1",
                (scope.bot_id,),
            ).fetchone()
            if mood_row:
                v, a, c = mood_row[0], mood_row[1], mood_row[2] or ""
                soul_lines.append(f"近期情绪：valence={round(float(v), 2)}，arousal={round(float(a), 2)}" + (f"（原因：{c}）" if c else ""))
        except Exception:
            pass
    if soul_lines:
        parts.append("[当前 Soul 状态：仅作为内部上下文，不要机械复述]\n" + "\n".join(f"- {l}" for l in soul_lines))
        channels_stat["soul_state"] = {"status": "hit", "items": len(soul_lines)}
    else:
        channels_stat["soul_state"] = {"status": "empty", "items": 0}

    # 3. [对话者画像]
    uid_in = str(body.get("uid") or "").strip()
    sid = uid_in or str(speaker.get("id") or "").strip()
    sname = str(speaker.get("name") or "").strip()
    norm_sid = _normalize_uid(sid)
    system_speakers = {"server", "minecraft", "system", "internal", "yushu", "yushucam", "corti", "corticam", "anon"}
    if norm_sid and norm_sid.lower() not in system_speakers and sname.lower() not in system_speakers:
        p_lines = []
        if db is not None:
            try:
                # 查画像维度（支持原 ID、规范化 ID 与昵称）
                p_row = db.conn.execute(
                    "SELECT interaction_count, last_seen, metadata FROM user_profiles WHERE (user_id=? OR user_id=? OR user_id=?) AND bot_id=? LIMIT 1",
                    (norm_sid, sid, sname, scope.bot_id),
                ).fetchone()
                if p_row:
                    cnt, last_seen, meta_raw = p_row[0], p_row[1], p_row[2]
                    p_lines.append(f"历史互动：{cnt} 次")
                    if meta_raw:
                        meta = json.loads(meta_raw)
                        dims = meta.get("dimensions", {})
                        if dims:
                            order = ("familiarity", "trust", "depth", "fun", "hostility")
                            rendered = "，".join(f"{k}={round(float(dims.get(k, 0.0)), 2)}" for k in order if k in dims)
                            p_lines.append(f"五维关系：{rendered}")
            except Exception:
                pass
        label = sname or norm_sid
        if not p_lines:
            p_lines.append("首次在直播间出现，暂无历史印象，保持礼貌与好奇。")
        parts.append(f"[对话者画像：{label}（仅用于调整自然回应，不必主动提及关系）]\n" + "\n".join(f"- {l}" for l in p_lines))
        channels_stat["relationship"] = {"status": "hit", "items": len(p_lines), "uid": norm_sid}
    else:
        channels_stat["relationship"] = {"status": "skipped", "reason": "system_or_no_speaker"}

    # 4. [相关记忆]：按高信息量关键词召回历史群聊/弹幕记忆（优先召回该用户专属记忆）
    mem_lines = []
    tokens = _extract_keywords(text, limit=3)
    if tokens and db is not None:
        try:
            like_clauses = " OR ".join(["content LIKE ?" for _ in tokens])
            params = [f"%{tok}%" for tok in tokens]

            # 若提供了非系统 uid，优先精准召回该用户的专属记忆
            if norm_sid and norm_sid.lower() not in system_speakers:
                user_sql = f"""SELECT content, sender_name, timestamp FROM memories
                              WHERE ({like_clauses}) AND (sender_id=? OR sender_id=?) AND bot_id=? AND resolution_state='resolved'
                              ORDER BY timestamp DESC LIMIT ?"""
                u_rows = db.conn.execute(user_sql, (*params, norm_sid, sid, scope.bot_id, limit)).fetchall()
                for r in u_rows:
                    c = str(r[0] or "")[:120]
                    who = str(r[1] or "")[:12]
                    mem_lines.append(f"{c}（{who}）" if who else c)

            # 若专属记忆不足，补充群内全局记忆
            if len(mem_lines) < limit:
                remain = limit - len(mem_lines)
                sql = f"""SELECT content, sender_name, timestamp FROM memories
                          WHERE ({like_clauses}) AND bot_id=? AND resolution_state='resolved'
                          ORDER BY timestamp DESC LIMIT ?"""
                rows = db.conn.execute(sql, (*params, scope.bot_id, remain)).fetchall()
                for r in rows:
                    c = str(r[0] or "")[:120]
                    who = str(r[1] or "")[:12]
                    item_str = f"{c}（{who}）" if who else c
                    if item_str not in mem_lines:
                        mem_lines.append(item_str)
        except Exception:
            pass

    if mem_lines:
        parts.append("[相关记忆：真实发生过的历史片段，可自然引用，不要机械复述]\n" + "\n".join(f"- {l}" for l in mem_lines))
        channels_stat["memory"] = {"status": "hit", "items": len(mem_lines)}
    else:
        channels_stat["memory"] = {"status": "empty", "items": 0}

    final_block = "\n\n".join(parts)
    return jsonify({
        "ok": True,
        "block": final_block,
        "chars": len(final_block),
        "tier": tier,
        "channels": channels_stat,
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

    if q:
        where_clauses.append("content LIKE ?")
        params.append(f"%{q}%")

    sql = f"""SELECT id, content, sender_id, sender_name, timestamp, importance 
              FROM memories 
              WHERE {' AND '.join(where_clauses)}
              ORDER BY timestamp DESC LIMIT ?"""
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

    if request.method == "GET":
        row = db.conn.execute(
            "SELECT user_id, nickname, affection, interaction_count, last_seen, metadata FROM user_profiles WHERE user_id = ? LIMIT 1",
            (uid,),
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
    body = await request.get_json() or {}
    nickname = str(body.get("nickname") or "").strip()
    dimensions = body.get("dimensions")
    affection_delta = int(body.get("affection_delta") or 0)

    try:
        row = db.conn.execute(
            "SELECT id, metadata, affection, interaction_count FROM user_profiles WHERE user_id = ? LIMIT 1",
            (uid,),
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
                   VALUES (?, 'live_room', 'yushu', ?, ?, 1, ?, ?, ?)""",
                (uid, nickname or uid, affection_delta, now, now, json.dumps(meta, ensure_ascii=False)),
            )
        db.conn.commit()
        return jsonify({"ok": True, "data": {"uid": uid, "status": "updated"}})
    except Exception as exc:
        return jsonify({"ok": False, "error": {"code": "update_error", "message": str(exc)}}), 500

