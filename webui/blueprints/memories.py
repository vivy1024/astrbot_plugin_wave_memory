"""Memories Blueprint — 记忆查询、导入、统计"""

from __future__ import annotations

import asyncio
import json
import time

from quart import Blueprint, jsonify, request, Response

from ..container import get_container
from ..middleware.auth import require_auth

memories_bp = Blueprint("memories", __name__, url_prefix="/api")

_import_lock = asyncio.Lock()

# 无过滤全表计数缓存（COUNT(*) 在十万行约 110ms，过滤计数则更慢，故仅缓存全表）
_total_cache: dict = {"value": None, "ts": 0.0}
_TOTAL_TTL = 30.0


def _table_exists(conn, table: str) -> bool:
    """检查表是否存在。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _safe_int(val, default):
    """安全 int 转换。"""
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


@memories_bp.route("/memories", methods=["GET"])
@require_auth
async def list_memories():
    """分页查看记忆列表（兼容 page/size 与 limit/offset，支持搜索与筛选）。"""
    c = get_container()

    # 分页：优先 page/size（前端），回退 limit/offset
    page = request.args.get("page")
    size = request.args.get("size")
    if page is not None or size is not None:
        size_i = max(1, min(200, _safe_int(size or 30, 30)))
        page_i = max(1, _safe_int(page or 1, 1))
        limit = size_i
        offset = (page_i - 1) * size_i
    else:
        limit = max(1, min(200, _safe_int(request.args.get("limit", 50), 50)))
        offset = max(0, _safe_int(request.args.get("offset", 0), 0))

    source = request.args.get("source")
    sender_id = request.args.get("sender_id")
    sender = request.args.get("sender")  # 按 sender_name
    group_id = request.args.get("group_id")
    search = (request.args.get("search") or "").strip()
    has_tags = request.args.get("has_tags")      # 'true'/'false'
    has_vector = request.args.get("has_vector")  # 'true'/'false'
    before_id = request.args.get("before_id")    # keyset 游标：取 id < before_id（深翻页 O(1)）
    bot_id = request.args.get("bot_id")           # 按 bot 的 QQ 号过滤 sender_id

    where = ["1=1"]
    params = []
    real_filter = False  # before_id 是游标翻页，不算"过滤"（无过滤时仍可用 total 缓存）
    if before_id:
        where.append("id < ?"); params.append(_safe_int(before_id, 0))
        offset = 0  # keyset 模式忽略 offset
    if source:
        where.append("source = ?"); params.append(source); real_filter = True
    if sender_id:
        where.append("sender_id = ?"); params.append(sender_id); real_filter = True
    if sender:
        where.append("sender_name = ?"); params.append(sender); real_filter = True
    if group_id:
        where.append("group_id = ?"); params.append(group_id); real_filter = True
    if bot_id:
        where.append("sender_id = ?"); params.append(bot_id); real_filter = True
    if search:
        where.append("content LIKE ?"); params.append(f"%{search}%"); real_filter = True
    if has_vector == "true":
        where.append("vector IS NOT NULL"); real_filter = True
    elif has_vector == "false":
        where.append("vector IS NULL"); real_filter = True
    if has_tags == "true":
        where.append("EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id = memories.id)"); real_filter = True
    elif has_tags == "false":
        where.append("NOT EXISTS (SELECT 1 FROM memory_tags mt WHERE mt.memory_id = memories.id)"); real_filter = True

    where_sql = " AND ".join(where)
    # 多取 1 条用于判断是否还有下一页（避免昂贵的过滤 COUNT）
    sql = (
        f"SELECT id, content, sender_id, sender_name, group_id, source, timestamp, "
        f"vector IS NOT NULL FROM memories WHERE {where_sql} ORDER BY id DESC LIMIT ? OFFSET ?"
    )
    rows = c.db.conn.execute(sql, params + [limit + 1, offset]).fetchall()
    has_more = len(rows) > limit
    rows = rows[:limit]

    # total：无筛选时用带缓存的全表计数（~110ms）；有筛选时跳过精确 COUNT
    # （LIKE / vector IS NULL 全表扫描需 ~2.7s），返回 null + has_more 供前端游标翻页
    if real_filter:
        total = None
    else:
        now = time.time()
        if _total_cache["value"] is not None and (now - _total_cache["ts"]) < _TOTAL_TTL:
            total = _total_cache["value"]
        else:
            total = c.db.conn.execute("SELECT COUNT(*) FROM memories").fetchone()[0]
            _total_cache["value"] = total
            _total_cache["ts"] = now

    items = [
        {"id": r[0], "content": r[1], "sender_id": r[2], "sender_name": r[3],
         "group_id": r[4], "source": r[5], "timestamp": r[6], "has_vector": bool(r[7])}
        for r in rows
    ]
    return jsonify({"items": items, "total": total, "has_more": has_more, "limit": limit, "offset": offset})


@memories_bp.route("/memories/senders", methods=["GET"])
@require_auth
async def list_senders():
    """发送者列表（按记忆数排序，供筛选下拉）。"""
    c = get_container()
    limit = max(1, min(500, _safe_int(request.args.get("limit", 100), 100)))
    rows = c.db.conn.execute(
        """SELECT sender_name, COUNT(*) AS cnt FROM memories
           WHERE sender_name IS NOT NULL AND sender_name != ''
           GROUP BY sender_name ORDER BY cnt DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return jsonify({"senders": [{"name": r[0], "count": r[1]} for r in rows]})


@memories_bp.route("/memories/<int:memory_id>", methods=["GET"])
@require_auth
async def get_memory(memory_id: int):
    """记忆详情。"""
    c = get_container()
    detail = c.db.get_memory_detail(memory_id)
    if not detail:
        return jsonify({"error": "not found"}), 404
    return jsonify(detail)


@memories_bp.route("/memories/<int:memory_id>", methods=["PUT"])
@require_auth
async def update_memory(memory_id: int):
    """更新记忆 content / importance。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    content = body.get("content")
    importance = body.get("importance")
    c.db.update_memory(memory_id, content=content, importance=importance)
    return jsonify({"ok": True, "memory_id": memory_id})


@memories_bp.route("/memories/<int:memory_id>", methods=["DELETE"])
@require_auth
async def delete_memory(memory_id: int):
    """删除单条记忆。"""
    c = get_container()
    c.db.delete_memory(memory_id)
    return jsonify({"ok": True, "deleted": memory_id})


@memories_bp.route("/memories/<int:memory_id>/re-embed", methods=["POST"])
@require_auth
async def re_embed_memory(memory_id: int):
    """重新向量化单条记忆。"""
    c = get_container()
    detail = c.db.get_memory_detail(memory_id)
    if not detail:
        return jsonify({"error": "not found"}), 404
    vec = await c.embedding_service.get_embedding(detail["content"] or "")
    if vec is None:
        return jsonify({"ok": False, "error": "embedding failed"}), 500
    c.db.update_memory_vector(memory_id, vec)
    try:
        if c.memory_index:
            c.memory_index.add([memory_id], [vec])
    except Exception:
        pass
    return jsonify({"ok": True, "memory_id": memory_id})


@memories_bp.route("/memories/batch/delete", methods=["POST"])
@require_auth
async def batch_delete_memories():
    """批量删除记忆。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    ids = [_safe_int(x, 0) for x in (body.get("ids") or [])]
    ids = [i for i in ids if i > 0]
    if not ids:
        return jsonify({"error": "ids required"}), 400
    placeholders = ",".join("?" * len(ids))
    c.db.conn.execute(f"DELETE FROM memory_tags WHERE memory_id IN ({placeholders})", ids)
    c.db.conn.execute(f"DELETE FROM memories WHERE id IN ({placeholders})", ids)
    # 同时清理 facts 表中 source_memory_id IN (...) 的引用
    if _table_exists(c.db.conn, "facts"):
        try:
            c.db.conn.execute(f"DELETE FROM facts WHERE source_memory_id IN ({placeholders})", ids)
        except Exception:
            pass
    c.db.conn.commit()
    return jsonify({"ok": True, "deleted": len(ids)})


@memories_bp.route("/memories/batch/re-embed", methods=["POST"])
@require_auth
async def batch_re_embed():
    """批量重新向量化（SSE 流）。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    ids = [_safe_int(x, 0) for x in (body.get("ids") or [])]
    ids = [i for i in ids if i > 0]

    async def stream():
        total = len(ids)
        yield f"data: {json.dumps({'progress': 0, 'total': total})}\n\n"
        done = errors = 0
        for mid in ids:
            try:
                detail = c.db.get_memory_detail(mid)
                if detail and detail.get("content"):
                    vec = await c.embedding_service.get_embedding(detail["content"])
                    if vec is not None:
                        c.db.update_memory_vector(mid, vec)
                        if c.memory_index:
                            c.memory_index.add([mid], [vec])
            except Exception:
                errors += 1
            done += 1
            yield f"data: {json.dumps({'progress': round(done/total, 3) if total else 1, 'processed': done, 'total': total, 'errors': errors})}\n\n"
        yield f"data: {json.dumps({'progress': 1.0, 'processed': done, 'total': total, 'errors': errors, 'done': True})}\n\n"

    return Response(stream(), content_type="text/event-stream")


@memories_bp.route("/memories/batch/extract-tags", methods=["POST"])
@require_auth
async def batch_extract_tags_for_ids():
    """对选中记忆批量提取 Tag（SSE 流）。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    ids = [_safe_int(x, 0) for x in (body.get("ids") or [])]
    ids = [i for i in ids if i > 0]

    async def stream():
        total = len(ids)
        if not c.tag_extractor:
            yield f"data: {json.dumps({'error': 'Tag extractor 未配置'})}\n\n"
            return
        yield f"data: {json.dumps({'progress': 0, 'total': total})}\n\n"
        done = tagged = errors = 0
        for mid in ids:
            try:
                row = c.db.conn.execute("SELECT content, sender_name FROM memories WHERE id=?", (mid,)).fetchone()
                if row and row[0] and len(row[0]) >= 4:
                    tags = await c.tag_extractor.extract_tags(row[0][:800], sender=row[1] or "")
                    if tags:
                        names = [t["name"] for t in tags]
                        vecs = await c.embedding_service.get_embeddings(names)
                        for tag_info, tv in zip(tags, vecs):
                            tid = c.db.add_tag_extended(name=tag_info["name"], tag_type=tag_info.get("type", "keyword"), vector=tv, confidence=tag_info.get("confidence", 0.8))
                            c.db.conn.execute("INSERT OR IGNORE INTO memory_tags (memory_id, tag_id, position, relevance) VALUES (?, ?, ?, ?)", (mid, tid, 1, tag_info.get("confidence", 0.8)))
                        c.db.conn.commit()
                        tagged += 1
            except Exception:
                errors += 1
            done += 1
            yield f"data: {json.dumps({'progress': round(done/total, 3) if total else 1, 'processed': done, 'total': total, 'tagged': tagged, 'errors': errors})}\n\n"
        yield f"data: {json.dumps({'progress': 1.0, 'processed': done, 'total': total, 'tagged': tagged, 'errors': errors, 'done': True})}\n\n"

    return Response(stream(), content_type="text/event-stream")



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
    """手动修改记忆属性（如 source）。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    allowed = {"source", "sender_name", "group_id"}
    sets = []
    params = []
    for key in allowed:
        if key in body:
            sets.append(f"{key} = ?")
            params.append(body[key])
    if not sets:
        return jsonify({"error": "No valid fields to update"}), 400
    params.append(memory_id)
    c.db.conn.execute(f"UPDATE memories SET {', '.join(sets)} WHERE id = ?", params)
    c.db.conn.commit()
    return jsonify({"ok": True, "memory_id": memory_id})


@memories_bp.route("/query", methods=["POST"])
@require_auth
async def query_test():
    """向量检索测试。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    text = body.get("text", "")
    top_k = _safe_int(body.get("top_k", 5), 5)
    enable_spike = body.get("enable_spike", True)
    enable_pyramid = body.get("enable_pyramid", True)
    enable_epa = body.get("enable_epa", False)
    enable_geodesic = body.get("enable_geodesic", False)

    timing = {}
    debug_info = {}

    t0 = time.perf_counter()
    query_vec = await c.embedding_service.get_embedding(text)
    timing["embedding_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    if query_vec is None:
        return jsonify({"results": [], "timing": timing, "debug": {"error": "embedding failed"}})

    debug_info["query_vector_dim"] = len(query_vec)

    t0 = time.perf_counter()
    candidates = c.memory_index.search(query_vec, k=top_k * 4)
    timing["vector_search_ms"] = round((time.perf_counter() - t0) * 1000, 1)
    debug_info["candidates_before_rerank"] = len(candidates)

    ids = [x[0] for x in candidates]
    distances = [x[1] for x in candidates]

    # Spike Routing
    timing["spike_routing_ms"] = 0
    energy_field = {}
    if enable_spike and c.spike_router:
        t0 = time.perf_counter()
        try:
            seed_results = c.tag_index.search(query_vec, k=5)
            seed_tags = [{"tag_id": tid, "weight": 1.0 - dist} for tid, dist in seed_results if (1.0 - dist) > 0.3]
            if seed_tags:
                spike_result = c.spike_router.propagate(seed_tags)
                energy_field = spike_result.get("energy_field", {})
                debug_info["spike_seeds"] = len(seed_tags)
                debug_info["spike_activated"] = len(spike_result.get("activated_tags", []))
        except Exception as e:
            debug_info["spike_error"] = str(e)
        timing["spike_routing_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    # Geodesic Rerank
    timing["geodesic_ms"] = 0
    if enable_geodesic and c.geodesic and energy_field:
        t0 = time.perf_counter()
        try:
            rerank_candidates = [{"id": mid, "score": 1.0 - distances[i] if i < len(distances) else 0} for i, mid in enumerate(ids)]
            reranked = c.geodesic.rerank(rerank_candidates, energy_field)
            ids = [x["id"] for x in reranked]
            distances = [1.0 - x["score"] for x in reranked]
        except Exception as e:
            debug_info["geodesic_error"] = str(e)
        timing["geodesic_ms"] = round((time.perf_counter() - t0) * 1000, 1)

    results = []
    for i, mid in enumerate(ids[:top_k]):
        mem = c.db.get_memory_brief(mid)
        if mem:
            score = 1.0 - distances[i] if i < len(distances) else 0
            mem["score"] = round(score, 4)
            results.append(mem)

    timing["total_ms"] = round(sum(timing.values()), 1)
    return jsonify({"results": results, "timing": timing, "debug": debug_info})


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


@memories_bp.route("/import/start", methods=["POST"])
@require_auth
async def import_start():
    """开始导入（SSE 流）。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    source = body.get("source", "")
    re_embed = body.get("re_embed", True)
    extract_tags = body.get("extract_tags", True)
    batch_size = _safe_int(body.get("batch_size", 20), 20)

    from ..importer import WaveMemoryImporter
    importer = WaveMemoryImporter(
        c.db, c.embedding_service, c.tag_extractor,
        memory_index=c.memory_index, writer=c.writer,
    )

    async def event_stream():
        async for event in importer.run(source=source, re_embed=re_embed, extract_tags=extract_tags, batch_size=batch_size):
            yield f"data: {event}\n\n"

    return Response(event_stream(), content_type="text/event-stream")


@memories_bp.route("/import/from-source", methods=["POST"])
@require_auth
async def import_from_source():
    """从指定数据源导入（SSE 流）。"""
    c = get_container()
    source_id = request.args.get("source_id", "")
    limit = _safe_int(request.args.get("limit", 5000), 5000)

    if _import_lock.locked():
        async def locked_msg():
            yield f"data: {json.dumps({'error': '另一个导入/提取任务正在运行'})}\n\n"
        return Response(locked_msg(), content_type="text/event-stream")

    from ..source_discovery import SourceDiscovery, UniversalImporter
    discovery = SourceDiscovery()
    all_sources = discovery.discover_all()
    source = next((s for s in all_sources if s["id"] == source_id), None)

    if not source:
        return jsonify({"error": f"Source not found: {source_id}"}), 404

    importer = UniversalImporter(
        c.db, c.embedding_service,
        tag_extractor=c.tag_extractor,
        memory_index=c.memory_index,
    )

    async def event_stream():
        async with _import_lock:
            if source["type"] == "known":
                async for event in importer.import_known(source, limit=limit):
                    yield f"data: {event}\n\n"
            else:
                analysis = source.get("analysis", {})
                importable = analysis.get("importable_tables", [])
                if importable:
                    table_info = importable[0]
                    cols = [col.lower() for col in table_info["columns"]]
                    content_field = next((col for col in cols if col in ("content", "text", "message")), cols[0] if cols else "content")
                    sender_field = next((col for col in cols if col in ("sender", "sender_name", "sender_id")), None)
                    ts_field = next((col for col in cols if col in ("timestamp", "created_at", "time", "ts")), None)
                    group_field = next((col for col in cols if col in ("group_id", "group", "session_id")), None)
                    mapping = {
                        "table": table_info["name"],
                        "content_field": content_field,
                        "sender_field": sender_field,
                        "timestamp_field": ts_field,
                        "group_field": group_field,
                        "filter": f"LENGTH({content_field}) >= 10",
                    }
                    async for event in importer.import_with_llm_mapping({"db_path": source["db_path"]}, mapping, limit=limit):
                        yield f"data: {event}\n\n"
                else:
                    yield f"data: {json.dumps({'error': 'No importable tables found'})}\n\n"

    return Response(event_stream(), content_type="text/event-stream")
