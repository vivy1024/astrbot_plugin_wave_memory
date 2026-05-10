"""Wave Memory WebUI — FastAPI 后端服务"""

from __future__ import annotations

import asyncio
import hashlib
import json
import math
import time
from pathlib import Path
from typing import Any, Optional

import uvicorn
from fastapi import FastAPI, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from astrbot.api import logger


class QueryRequest(BaseModel):
    text: str
    top_k: int = 5
    group_id: Optional[str] = None
    enable_spike: bool = True
    enable_pyramid: bool = True
    enable_epa: bool = False
    enable_geodesic: bool = False


class ImportPreviewRequest(BaseModel):
    source: str  # "livingmemory" or "self_learning"


class ImportStartRequest(BaseModel):
    source: str
    re_embed: bool = True
    extract_tags: bool = True
    batch_size: int = 20


class WaveMemoryWebUI:
    """Wave Memory WebUI 服务器"""

    def __init__(
        self,
        db,
        query_engine,
        embedding_service,
        memory_index,
        tag_index,
        cooccurrence,
        spike_router=None,
        residual_pyramid=None,
        epa=None,
        geodesic=None,
        tag_extractor=None,
        writer=None,
        host: str = "0.0.0.0",
        port: int = 7890,
        password: str = "",
        plugin_config: dict = None,
    ):
        self.db = db
        self.query_engine = query_engine
        self.embedding_service = embedding_service
        self.memory_index = memory_index
        self.tag_index = tag_index
        self.cooccurrence = cooccurrence
        self.spike_router = spike_router
        self.residual_pyramid = residual_pyramid
        self.epa = epa
        self.geodesic = geodesic
        self.tag_extractor = tag_extractor
        self.writer = writer
        self.host = host
        self.port = port
        self.password = password
        self.plugin_config = plugin_config or {}
        self._task: Optional[asyncio.Task] = None
        self._sessions: set = set()  # 简单 token 管理

        self.app = self._create_app()

    def _create_app(self) -> FastAPI:
        app = FastAPI(title="Wave Memory WebUI", docs_url="/docs")

        app.add_middleware(
            CORSMiddleware,
            allow_origins=["*"],
            allow_methods=["*"],
            allow_headers=["*"],
        )

        # 静态文件
        static_dir = Path(__file__).parent / "static"
        if static_dir.exists():
            app.mount("/static", StaticFiles(directory=str(static_dir)), name="static")

        # 路由
        self._register_routes(app)

        return app

    def _register_routes(self, app: FastAPI):

        # ─── Auth ───

        @app.post("/api/login")
        async def login(request: dict = None):
            """登录获取 token。"""
            from fastapi import Request as FastAPIRequest
            if not self.password:
                return {"token": "no-auth", "message": "No password required"}
            body = request or {}
            if body.get("password") == self.password:
                import secrets
                token = secrets.token_hex(16)
                self._sessions.add(token)
                return {"token": token, "message": "Login successful"}
            from fastapi import HTTPException
            raise HTTPException(status_code=401, detail="Invalid password")

        @app.get("/api/auth/check")
        async def auth_check():
            """检查是否需要认证。"""
            return {"requires_auth": bool(self.password)}

        @app.get("/", response_class=HTMLResponse)
        async def index():
            index_path = Path(__file__).parent / "static" / "index.html"
            if index_path.exists():
                return index_path.read_text(encoding="utf-8")
            return "<h1>Wave Memory WebUI</h1><p>static/index.html not found</p>"

        @app.get("/explore", response_class=HTMLResponse)
        async def explore():
            explore_path = Path(__file__).parent / "static" / "explore.html"
            if explore_path.exists():
                return explore_path.read_text(encoding="utf-8")
            return "<h1>Wave Memory</h1><p>explore.html not found</p>"

        # ─── Stats ───

        @app.get("/api/stats")
        async def get_stats():
            total = self.db.get_memory_count()
            with_vec = self.db.get_memory_count_with_vector()
            tags = self.db.get_tag_count()
            groups = self.db.get_group_list()
            today_new = self.db.get_today_new_count()
            cooc_edges = self.cooccurrence.edge_count if self.cooccurrence else 0

            return {
                "total_memories": total,
                "memories_with_vector": with_vec,
                "total_tags": tags,
                "total_groups": len(groups),
                "today_new": today_new,
                "cooccurrence_edges": cooc_edges,
                "groups": groups,
            }

        # ─── Memories ───

        @app.get("/api/memories")
        async def list_memories(
            page: int = Query(1, ge=1),
            size: int = Query(20, ge=1, le=100),
            group_id: Optional[str] = None,
            sender: Optional[str] = None,
            from_ts: Optional[float] = None,
            to_ts: Optional[float] = None,
            search: Optional[str] = None,
            has_tags: Optional[bool] = None,
            has_vector: Optional[bool] = None,
        ):
            offset = (page - 1) * size
            items, total = self.db.list_memories(
                offset=offset,
                limit=size,
                group_id=group_id,
                sender=sender,
                from_ts=from_ts,
                to_ts=to_ts,
                search=search,
                has_tags=has_tags,
                has_vector=has_vector,
            )
            return {
                "items": items,
                "total": total,
                "page": page,
                "pages": math.ceil(total / size) if total > 0 else 0,
            }

        @app.get("/api/memories/senders")
        async def list_senders():
            """获取所有发送者列表。"""
            return {"senders": self.db.get_senders_list()}

        @app.get("/api/memories/{memory_id}")
        async def get_memory(memory_id: int):
            memory = self.db.get_memory_detail(memory_id)
            if not memory:
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail="Memory not found")
            return memory

        @app.delete("/api/memories/{memory_id}")
        async def delete_memory(memory_id: int):
            """删除单条记忆。"""
            ok = self.db.delete_memory(memory_id)
            if not ok:
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail="Memory not found")
            # 从向量索引中移除
            if self.memory_index:
                try:
                    self.memory_index.remove([memory_id])
                except Exception:
                    pass
            return {"ok": True, "id": memory_id}

        @app.post("/api/memories/batch/delete")
        async def batch_delete_memories(body: dict):
            """批量删除记忆。"""
            ids = body.get("ids", [])
            if not ids:
                return {"deleted": 0}
            count = self.db.delete_memories(ids)
            if self.memory_index:
                try:
                    self.memory_index.remove(ids)
                except Exception:
                    pass
            return {"deleted": count}

        @app.put("/api/memories/{memory_id}")
        async def update_memory(memory_id: int, body: dict):
            """编辑记忆内容/重要度。"""
            content = body.get("content")
            importance = body.get("importance")
            ok = self.db.update_memory(memory_id, content=content, importance=importance)
            if not ok:
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail="Memory not found or no changes")
            return {"ok": True, "id": memory_id}

        @app.post("/api/memories/{memory_id}/re-embed")
        async def re_embed_memory(memory_id: int):
            """重新生成单条记忆的向量。"""
            detail = self.db.get_memory_detail(memory_id)
            if not detail:
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail="Memory not found")
            vec = await self.embedding_service.get_embedding(detail["content"][:500])
            import numpy as np
            self.db.update_memory_vector(memory_id, np.array(vec))
            if self.memory_index:
                try:
                    self.memory_index.add([memory_id], np.array(vec).reshape(1, -1))
                except Exception:
                    pass
            return {"ok": True, "id": memory_id}

        @app.post("/api/memories/batch/re-embed")
        async def batch_re_embed(body: dict):
            """批量重新 embedding（SSE 流）。"""
            from fastapi.responses import StreamingResponse
            ids = body.get("ids", [])

            async def run():
                total = len(ids)
                done = 0
                errors = 0
                for mid in ids:
                    try:
                        detail = self.db.get_memory_detail(mid)
                        if detail:
                            vec = await self.embedding_service.get_embedding(detail["content"][:500])
                            import numpy as np
                            self.db.update_memory_vector(mid, np.array(vec))
                            if self.memory_index:
                                self.memory_index.add([mid], np.array(vec).reshape(1, -1))
                    except Exception:
                        errors += 1
                    done += 1
                    if done % 5 == 0 or done == total:
                        yield f"data: {json.dumps({'progress': round(done/total, 3), 'done': done, 'total': total, 'errors': errors})}\n\n"
                if self.memory_index:
                    self.memory_index.save()
                yield f"data: {json.dumps({'progress': 1.0, 'done': total, 'total': total, 'errors': errors, 'message': f'完成: {total - errors} 成功, {errors} 失败'})}\n\n"

            return StreamingResponse(run(), media_type="text/event-stream")

        @app.post("/api/memories/batch/extract-tags")
        async def batch_extract_tags_for_ids(body: dict):
            """为指定记忆批量提取 Tag（SSE 流）。"""
            from fastapi.responses import StreamingResponse
            ids = body.get("ids", [])

            async def run():
                if not self.tag_extractor:
                    yield f"data: {json.dumps({'error': 'Tag extractor not configured'})}\n\n"
                    return
                total = len(ids)
                done = 0
                tagged = 0
                errors = 0
                for mid in ids:
                    try:
                        detail = self.db.get_memory_detail(mid)
                        if detail and detail["content"]:
                            tags = await self.tag_extractor.extract_tags(detail["content"][:800], sender=detail.get("sender_name", ""))
                            if tags:
                                tag_names = [t["name"] for t in tags]
                                tag_vecs = await self.embedding_service.get_embeddings(tag_names)
                                tag_ids = []
                                for tag_info, tag_vec in zip(tags, tag_vecs):
                                    tid = self.db.add_tag_extended(
                                        name=tag_info["name"],
                                        tag_type=tag_info.get("type", "keyword"),
                                        vector=tag_vec,
                                        confidence=tag_info.get("confidence", 0.8),
                                    )
                                    tag_ids.append(tid)
                                for pos, (tid, tag_info) in enumerate(zip(tag_ids, tags), 1):
                                    self.db.conn.execute(
                                        "INSERT OR IGNORE INTO memory_tags (memory_id, tag_id, position, relevance) VALUES (?, ?, ?, ?)",
                                        (mid, tid, pos, tag_info.get("confidence", 0.8)),
                                    )
                                self.db.conn.commit()
                                tagged += 1
                    except Exception:
                        errors += 1
                    done += 1
                    if done % 3 == 0 or done == total:
                        yield f"data: {json.dumps({'progress': round(done/total, 3), 'done': done, 'total': total, 'tagged': tagged, 'errors': errors})}\n\n"
                yield f"data: {json.dumps({'progress': 1.0, 'done': total, 'total': total, 'tagged': tagged, 'errors': errors, 'message': f'完成: {tagged} 已标记, {errors} 失败'})}\n\n"

            return StreamingResponse(run(), media_type="text/event-stream")

        # ─── Tags ───

        @app.get("/api/tags")
        async def list_tags(
            page: int = Query(1, ge=1),
            size: int = Query(50, ge=1, le=200),
        ):
            offset = (page - 1) * size
            items, total = self.db.list_tags(offset=offset, limit=size)
            return {
                "items": items,
                "total": total,
                "page": page,
                "pages": math.ceil(total / size) if total > 0 else 0,
            }

        @app.get("/api/tags/graph")
        async def get_tag_graph():
            nodes, edges = self.db.get_tag_graph_data()
            return {"nodes": nodes, "edges": edges}

        @app.get("/api/tags/{tag_id}/memories")
        async def get_tag_memories(tag_id: int, size: int = Query(10, ge=1, le=50)):
            """获取某个 Tag 关联的记忆列表。"""
            rows = self.db.conn.execute(
                """SELECT m.id, m.content, m.sender_name, m.group_id, m.timestamp
                   FROM memories m
                   JOIN memory_tags mt ON m.id = mt.memory_id
                   WHERE mt.tag_id = ?
                   ORDER BY m.timestamp DESC
                   LIMIT ?""",
                (tag_id, size),
            ).fetchall()
            return [
                {"id": r[0], "content": r[1][:100] if r[1] else "", "sender_name": r[2], "group_id": r[3], "timestamp": r[4]}
                for r in rows
            ]

        # ─── Query Test ───

        @app.post("/api/query")
        async def query_test(req: QueryRequest):
            timing = {}
            debug_info = {}

            # Embedding
            t0 = time.perf_counter()
            query_vec = await self.embedding_service.get_embedding(req.text)
            timing["embedding_ms"] = round((time.perf_counter() - t0) * 1000, 1)

            if query_vec is None:
                return {"results": [], "timing": timing, "debug": {"error": "embedding failed"}}

            debug_info["query_vector_dim"] = len(query_vec)

            # 向量检索
            t0 = time.perf_counter()
            candidates = self.memory_index.search(query_vec, k=req.top_k * 4)
            timing["vector_search_ms"] = round((time.perf_counter() - t0) * 1000, 1)
            debug_info["candidates_before_rerank"] = len(candidates)

            # candidates 是 [(id, distance), ...] 列表
            ids = [c[0] for c in candidates]
            distances = [c[1] for c in candidates]

            # Spike Routing — 从查询向量找种子 Tag，然后沿共现图传播
            timing["spike_routing_ms"] = 0
            energy_field = {}
            if req.enable_spike and self.spike_router:
                t0 = time.perf_counter()
                try:
                    # 先找查询向量最近的 Tag 作为种子
                    seed_results = self.tag_index.search(query_vec, k=5)
                    seed_tags = [{"tag_id": tid, "weight": 1.0 - dist} for tid, dist in seed_results if (1.0 - dist) > 0.3]
                    if seed_tags:
                        spike_result = self.spike_router.propagate(seed_tags)
                        activated = spike_result.get("activated_tags", [])
                        energy_field = spike_result.get("energy_field", {})
                        debug_info["spike_seeds"] = len(seed_tags)
                        debug_info["spike_activated"] = len(activated)
                        debug_info["spike_emergent"] = sum(1 for a in activated if a.get("is_emergent"))
                    else:
                        debug_info["spike_seeds"] = 0
                except Exception as e:
                    debug_info["spike_error"] = str(e)
                timing["spike_routing_ms"] = round((time.perf_counter() - t0) * 1000, 1)

            # Residual Pyramid — 多层语义分解
            timing["residual_pyramid_ms"] = 0
            if req.enable_pyramid and self.residual_pyramid:
                t0 = time.perf_counter()
                try:
                    # 使用缓存的 tag 向量映射（每 60s 刷新）
                    tag_vectors_by_id = self._get_tag_vectors_cache()
                    pyramid_result = self.residual_pyramid.analyze(query_vec, tag_vectors_by_id)
                    debug_info["pyramid_levels"] = len(pyramid_result.get("levels", []))
                    debug_info["pyramid_coverage"] = round(pyramid_result.get("coverage", 0), 3)
                    debug_info["pyramid_tag_ids"] = pyramid_result.get("all_tag_ids", [])[:10]
                except Exception as e:
                    debug_info["pyramid_error"] = str(e)
                timing["residual_pyramid_ms"] = round((time.perf_counter() - t0) * 1000, 1)

            # EPA — 查询聚焦度分析
            timing["epa_ms"] = 0
            if req.enable_epa and self.epa:
                t0 = time.perf_counter()
                try:
                    epa_result = self.epa.analyze(query_vec)
                    debug_info["epa_logic_depth"] = round(epa_result.get("logic_depth", 0), 3)
                    debug_info["epa_entropy"] = round(epa_result.get("entropy", 0), 3)
                    debug_info["epa_dominant_axis"] = epa_result.get("dominant_axis", 0)
                except Exception as e:
                    debug_info["epa_error"] = str(e)
                timing["epa_ms"] = round((time.perf_counter() - t0) * 1000, 1)

            # Geodesic Rerank — 基于能量场修正排序
            timing["geodesic_ms"] = 0
            if req.enable_geodesic and self.geodesic and energy_field:
                t0 = time.perf_counter()
                try:
                    # 构建候选列表
                    rerank_candidates = []
                    for i, mid in enumerate(ids):
                        score = 1.0 - distances[i] if i < len(distances) else 0
                        rerank_candidates.append({"id": mid, "score": score})
                    reranked = self.geodesic.rerank(rerank_candidates, energy_field)
                    # 用重排后的顺序替换
                    ids = [c["id"] for c in reranked]
                    distances = [1.0 - c["score"] for c in reranked]
                    debug_info["geodesic_applied"] = True
                except Exception as e:
                    debug_info["geodesic_error"] = str(e)
                timing["geodesic_ms"] = round((time.perf_counter() - t0) * 1000, 1)

            # 组装结果
            results = []
            for i, mid in enumerate(ids[:req.top_k]):
                mem = self.db.get_memory_brief(mid)
                if mem:
                    score = 1.0 - distances[i] if i < len(distances) else 0
                    mem["score"] = round(score, 4)
                    results.append(mem)

            timing["total_ms"] = round(sum(timing.values()), 1)

            return {"results": results, "timing": timing, "debug": debug_info}

        # ─── Import ───

        @app.post("/api/import/preview")
        async def import_preview(req: ImportPreviewRequest):
            from .importer import WaveMemoryImporter
            importer = WaveMemoryImporter(self.db, self.embedding_service, self.tag_extractor)
            return await importer.preview(req.source)

        @app.post("/api/import/start")
        async def import_start(req: ImportStartRequest):
            from fastapi.responses import StreamingResponse
            from .importer import WaveMemoryImporter
            importer = WaveMemoryImporter(
                self.db, self.embedding_service, self.tag_extractor,
                memory_index=self.memory_index,
                writer=self.writer,
            )

            async def event_stream():
                async for event in importer.run(
                    source=req.source,
                    re_embed=req.re_embed,
                    extract_tags=req.extract_tags,
                    batch_size=req.batch_size,
                ):
                    yield f"data: {event}\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        # ─── 批量 Tag 提取 ───

        @app.post("/api/tags/batch-extract")
        async def batch_extract_tags(batch_size: int = Query(20, ge=1, le=100)):
            """后台批量为无 Tag 的记忆提取 Tag（SSE 流）。"""
            from fastapi.responses import StreamingResponse

            async def run_batch():
                if not self.tag_extractor:
                    yield f"data: {json.dumps({'error': 'Tag extractor not configured'})}\n\n"
                    return

                # 查找无 Tag 的记忆
                rows = self.db.conn.execute(
                    """SELECT m.id, m.content, m.sender_name FROM memories m
                       WHERE m.id NOT IN (SELECT DISTINCT memory_id FROM memory_tags)
                       AND LENGTH(m.content) >= 10
                       ORDER BY m.id DESC
                       LIMIT 5000"""
                ).fetchall()

                total = len(rows)
                if total == 0:
                    yield f"data: {json.dumps({'progress': 1.0, 'message': 'All memories already have tags'})}\n\n"
                    return

                yield f"data: {json.dumps({'progress': 0, 'total': total, 'message': f'Starting batch tag extraction for {total} memories...'})}\n\n"

                processed = 0
                tagged = 0
                errors = 0

                for i in range(0, total, batch_size):
                    batch = rows[i:i + batch_size]

                    for mem_id, content, sender_name in batch:
                        try:
                            tags = await self.tag_extractor.extract_tags(content[:800], sender=sender_name or "")
                            if tags:
                                tag_names = [t["name"] for t in tags]
                                tag_vecs = await self.embedding_service.get_embeddings(tag_names)

                                tag_ids = []
                                for tag_info, tag_vec in zip(tags, tag_vecs):
                                    tid = self.db.add_tag_extended(
                                        name=tag_info["name"],
                                        tag_type=tag_info.get("type", "keyword"),
                                        vector=tag_vec,
                                        confidence=tag_info.get("confidence", 0.8),
                                    )
                                    tag_ids.append(tid)

                                for pos, (tid, tag_info) in enumerate(zip(tag_ids, tags), 1):
                                    self.db.conn.execute(
                                        "INSERT OR IGNORE INTO memory_tags (memory_id, tag_id, position, relevance) VALUES (?, ?, ?, ?)",
                                        (mem_id, tid, pos, tag_info.get("confidence", 0.8)),
                                    )
                                self.db.conn.commit()
                                tagged += 1
                        except Exception as e:
                            errors += 1

                        processed += 1

                    progress = processed / total
                    yield f"data: {json.dumps({'progress': round(progress, 3), 'processed': processed, 'total': total, 'tagged': tagged, 'errors': errors, 'message': f'Batch {i // batch_size + 1}: {processed}/{total} ({tagged} tagged, {errors} errors)'})}\n\n"

                    # 让出事件循环
                    import asyncio
                    await asyncio.sleep(0.1)

                yield f"data: {json.dumps({'progress': 1.0, 'processed': total, 'total': total, 'tagged': tagged, 'errors': errors, 'message': f'Complete: {tagged}/{total} tagged, {errors} errors'})}\n\n"

            return StreamingResponse(run_batch(), media_type="text/event-stream")

        # ─── 系统状态 ───

        @app.get("/api/system")
        async def system_status():
            """系统健康状态和进度信息。"""
            total_mem = self.db.get_memory_count()
            with_vec = self.db.get_memory_count_with_vector()
            total_tags = self.db.get_tag_count()

            # Tag 覆盖率
            tagged_memories = self.db.conn.execute(
                "SELECT COUNT(DISTINCT memory_id) FROM memory_tags"
            ).fetchone()[0]

            # 结构化 Tag 比例
            structured_tags = self.db.conn.execute(
                "SELECT COUNT(*) FROM tags WHERE tag_type != 'keyword'"
            ).fetchone()[0]

            # Tag 类型分布
            type_dist = self.db.conn.execute(
                "SELECT tag_type, COUNT(*) FROM tags GROUP BY tag_type ORDER BY COUNT(*) DESC"
            ).fetchall()

            # 共现矩阵状态
            cooc_nodes = self.cooccurrence.node_count if self.cooccurrence else 0
            cooc_edges = self.cooccurrence.edge_count if self.cooccurrence else 0

            return {
                "memories": {"total": total_mem, "with_vector": with_vec, "with_tags": tagged_memories},
                "tags": {"total": total_tags, "structured": structured_tags, "type_distribution": {r[0]: r[1] for r in type_dist}},
                "coverage": {"vector_pct": round(with_vec / total_mem * 100, 1) if total_mem > 0 else 0, "tag_pct": round(tagged_memories / total_mem * 100, 1) if total_mem > 0 else 0},
                "cooccurrence": {"nodes": cooc_nodes, "edges": cooc_edges},
                "epa": {"initialized": self.epa.initialized if self.epa else False},
            }

        # ─── LLM Provider 列表 ───

        @app.get("/api/providers")
        async def list_providers():
            """列出可用的 LLM/Embedding Provider（用于配置选择）。"""
            try:
                providers = []
                seen_ids = set()
                embed_id = self.plugin_config.get("embedding_provider_id", "")
                all_provs = self.embedding_service.context.get_all_providers()
                for prov in all_provs:
                    try:
                        meta = prov.meta()
                        if meta.id not in seen_ids:
                            # 如果是当前配置的 embedding provider，标记为 embedding 类型
                            ptype = "embedding" if meta.id == embed_id else (meta.type or "unknown")
                            providers.append({
                                "id": meta.id,
                                "model": meta.model or "",
                                "type": ptype,
                            })
                            seen_ids.add(meta.id)
                    except Exception:
                        pass

                # 确保当前配置的 embedding provider 也在列表中
                if embed_id and embed_id not in seen_ids:
                    providers.insert(0, {
                        "id": embed_id,
                        "model": embed_id.split("/")[-1] if "/" in embed_id else embed_id,
                        "type": "embedding",
                    })
                    seen_ids.add(embed_id)

                # 确保当前配置的 tag LLM provider 也在列表中
                tag_id = self.plugin_config.get("tag_llm_provider_id", "")
                if tag_id and tag_id not in seen_ids:
                    providers.append({
                        "id": tag_id,
                        "model": tag_id.split("/")[-1] if "/" in tag_id else tag_id,
                        "type": "llm",
                    })

                return {"providers": providers}
            except Exception as e:
                return {"providers": [], "error": str(e)}

        # ─── 当前配置（读写） ───

        @app.get("/api/config")
        async def get_config():
            """返回当前插件运行配置（脱敏）。"""
            cfg = self.plugin_config
            return {
                "embedding_provider_id": cfg.get("embedding_provider_id", ""),
                "embedding_dimension": cfg.get("embedding_dimension", 1024),
                "tag_llm_provider_id": cfg.get("tag_llm_provider_id", ""),
                "query": cfg.get("Query_Settings", {}),
                "tags": cfg.get("Tag_Settings", {}),
                "storage": cfg.get("Storage_Settings", {}),
                "filter": cfg.get("Message_Filter", {}),
                "performance": cfg.get("Performance_Settings", {}),
                "webui": {
                    "enabled": cfg.get("WebUI_Settings", {}).get("webui_enabled", True),
                    "host": cfg.get("WebUI_Settings", {}).get("webui_host", "0.0.0.0"),
                    "port": cfg.get("WebUI_Settings", {}).get("webui_port", 9876),
                },
            }

        @app.post("/api/config")
        async def update_config(request: Request):
            """更新插件配置并持久化。需要重启才能完全生效。"""
            body = await request.json()
            cfg = self.plugin_config

            # 映射前端字段 → 实际配置 key
            field_map = {
                "embedding_provider_id": "embedding_provider_id",
                "embedding_dimension": "embedding_dimension",
                "tag_llm_provider_id": "tag_llm_provider_id",
                "query": "Query_Settings",
                "tags": "Tag_Settings",
                "storage": "Storage_Settings",
                "filter": "Message_Filter",
                "performance": "Performance_Settings",
            }

            changed = []
            for front_key, cfg_key in field_map.items():
                if front_key in body:
                    val = body[front_key]
                    if isinstance(val, dict):
                        # 合并嵌套 object
                        existing = cfg.get(cfg_key, {})
                        existing.update(val)
                        cfg[cfg_key] = existing
                    else:
                        cfg[cfg_key] = val
                    changed.append(front_key)

            # 持久化
            if changed and hasattr(cfg, "save_config"):
                cfg.save_config()

            return {"ok": True, "changed": changed, "message": "配置已保存，部分参数需重启生效"}

        # ─── LLM Tag 提取（使用配置中固定的 provider） ───

        @app.post("/api/import/llm-extract")
        async def llm_import_extract(batch_size: int = Query(10, ge=1, le=50)):
            """使用配置中的 tag_llm_provider_id 为无 Tag 记忆批量提取结构化 Tag（SSE 流）。"""
            from fastapi.responses import StreamingResponse

            async def run():
                extractor = self.tag_extractor
                if not extractor:
                    yield f"data: {json.dumps({'error': 'Tag extractor not configured. Set tag_llm_provider_id in plugin config.'})}\n\n"
                    return

                rows = self.db.conn.execute(
                    """SELECT m.id, m.content, m.sender_name FROM memories m
                       WHERE m.id NOT IN (SELECT DISTINCT memory_id FROM memory_tags)
                       AND LENGTH(m.content) >= 10
                       ORDER BY m.id DESC LIMIT 2000"""
                ).fetchall()

                total = len(rows)
                if total == 0:
                    yield f"data: {json.dumps({'progress': 1.0, 'message': 'All memories already have tags'})}\n\n"
                    return

                yield f"data: {json.dumps({'progress': 0, 'total': total, 'provider': extractor.provider_id, 'message': f'Starting LLM tag extraction ({total} memories)...'})}\n\n"

                processed = 0
                tagged = 0
                errors = 0

                for i in range(0, total, batch_size):
                    batch = rows[i:i + batch_size]
                    for mem_id, content, sender_name in batch:
                        try:
                            tags = await extractor.extract_tags(content[:800], sender=sender_name or "")
                            if tags:
                                tag_names = [t["name"] for t in tags]
                                tag_vecs = await self.embedding_service.get_embeddings(tag_names)
                                tag_ids = []
                                for tag_info, tag_vec in zip(tags, tag_vecs):
                                    tid = self.db.add_tag_extended(
                                        name=tag_info["name"],
                                        tag_type=tag_info.get("type", "keyword"),
                                        vector=tag_vec,
                                        confidence=tag_info.get("confidence", 0.8),
                                    )
                                    tag_ids.append(tid)
                                for pos, (tid, tag_info) in enumerate(zip(tag_ids, tags), 1):
                                    self.db.conn.execute(
                                        "INSERT OR IGNORE INTO memory_tags (memory_id, tag_id, position, relevance) VALUES (?, ?, ?, ?)",
                                        (mem_id, tid, pos, tag_info.get("confidence", 0.8)),
                                    )
                                self.db.conn.commit()
                                tagged += 1
                        except Exception:
                            errors += 1
                        processed += 1

                    yield f"data: {json.dumps({'progress': round(processed/total, 3), 'processed': processed, 'total': total, 'tagged': tagged, 'errors': errors, 'message': f'{processed}/{total} ({tagged} tagged)'})}\n\n"
                    import asyncio
                    await asyncio.sleep(0.05)

                yield f"data: {json.dumps({'progress': 1.0, 'processed': total, 'total': total, 'tagged': tagged, 'errors': errors, 'message': f'Complete: {tagged}/{total} tagged, {errors} errors'})}\n\n"

            return StreamingResponse(run(), media_type="text/event-stream")

        # ─── 数据源发现（通用，带缓存） ───

        _sources_cache = {"data": None, "ts": 0}

        @app.get("/api/import/sources")
        async def discover_sources(refresh: bool = False):
            """通用数据源发现 — 结果缓存 60s，?refresh=true 强制刷新。"""
            now = time.time()
            if not refresh and _sources_cache["data"] and now - _sources_cache["ts"] < 60:
                return _sources_cache["data"]

            from .source_discovery import SourceDiscovery
            discovery = SourceDiscovery()
            sources = discovery.discover_all()
            result = []
            for s in sources:
                progress = discovery.estimate_imported(s, self.db)
                result.append({
                    "id": s["id"],
                    "name": s["name"],
                    "description": s["description"],
                    "count": s["count"],
                    "type": s["type"],
                    "db_path": s.get("db_path", ""),
                    "has_adapter": s["type"] == "known",
                    "imported_pct": progress["estimated_pct"],
                    "remaining": progress["estimated_remaining"],
                })
            resp = {"sources": result}
            _sources_cache["data"] = resp
            _sources_cache["ts"] = now
            return resp

        # ─── 从数据源导入（通用） ───

        @app.post("/api/import/from-source")
        async def import_from_source(source_id: str = Query(...), limit: int = Query(500, ge=1, le=5000)):
            """从指定数据源导入记忆到 Wave Memory（SSE 流）。"""
            from fastapi.responses import StreamingResponse
            from .source_discovery import SourceDiscovery, UniversalImporter

            discovery = SourceDiscovery()
            all_sources = discovery.discover_all()
            source = next((s for s in all_sources if s["id"] == source_id), None)

            if not source:
                from fastapi import HTTPException
                raise HTTPException(status_code=404, detail=f"Source not found: {source_id}")

            importer = UniversalImporter(
                self.db, self.embedding_service,
                tag_extractor=self.tag_extractor,
                memory_index=self.memory_index,
            )

            async def event_stream():
                if source["type"] == "known":
                    async for event in importer.import_known(source, limit=limit):
                        yield f"data: {event}\n\n"
                elif source.get("llm_mapping"):
                    async for event in importer.import_with_llm_mapping(source, source["llm_mapping"], limit=limit):
                        yield f"data: {event}\n\n"
                else:
                    # 未知源且无 LLM mapping — 尝试启发式导入
                    # 用第一个 importable table 的启发式映射
                    analysis = source.get("analysis", {})
                    importable = analysis.get("importable_tables", [])
                    if importable:
                        table_info = importable[0]
                        # 启发式字段映射
                        cols = [c.lower() for c in table_info["columns"]]
                        content_field = next((c for c in cols if c in ("content", "text", "message", "judgment", "summary", "note")), cols[0] if cols else "content")
                        sender_field = next((c for c in cols if c in ("sender", "sender_name", "sender_id", "user_name", "author")), None)
                        ts_field = next((c for c in cols if c in ("timestamp", "created_at", "time", "ts")), None)
                        group_field = next((c for c in cols if c in ("group_id", "group", "session_id", "conversation_id", "channel_id")), None)

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
                        yield f"data: {json.dumps({'error': 'No importable tables found in this source'})}\n\n"

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        # ─── LLM 分析未知数据源 ───

        @app.post("/api/import/analyze")
        async def analyze_source(db_path: str = Query(...), table_name: str = Query(...)):
            """用 LLM 分析未知数据库表结构，生成导入映射。"""
            from .source_discovery import SourceDiscovery, ANALYZE_SOURCE_PROMPT

            if not self.tag_extractor or not self.tag_extractor.provider_id:
                return {"error": "No LLM provider configured. Set tag_llm_provider_id in plugin config."}

            discovery = SourceDiscovery()
            schema = discovery.get_table_schema(db_path, table_name)

            provider = self.tag_extractor.context.get_provider_by_id(self.tag_extractor.provider_id)
            if not provider:
                return {"error": f"Provider '{self.tag_extractor.provider_id}' not found"}

            prompt = ANALYZE_SOURCE_PROMPT.format(schema_json=json.dumps(schema, ensure_ascii=False, indent=2))
            response = await provider.text_chat(prompt=prompt)

            if not response or not response.completion_text:
                return {"error": "LLM returned empty response"}

            # 解析 JSON
            import re
            text = response.completion_text.strip()
            text = re.sub(r'^```(?:json)?\s*', '', text, flags=re.MULTILINE)
            text = re.sub(r'```\s*$', '', text, flags=re.MULTILINE)
            json_match = re.search(r'\{[\s\S]*\}', text)
            if json_match:
                try:
                    result = json.loads(json_match.group())
                    return {"analysis": result, "schema": schema}
                except json.JSONDecodeError:
                    pass

            return {"error": "Failed to parse LLM response", "raw": text[:500]}

    def _get_tag_vectors_cache(self):
        """获取 tag 向量缓存（60s TTL）。"""
        now = time.time()
        if not hasattr(self, '_tag_vec_cache') or now - self._tag_vec_cache_ts > 60:
            tag_data = self.db.get_all_tag_vectors()
            self._tag_vec_cache = {t[0]: t[2] for t in tag_data}
            self._tag_vec_cache_ts = now
        return self._tag_vec_cache

    async def start(self):
        """启动 WebUI 服务器（后台任务）"""
        config = uvicorn.Config(
            self.app,
            host=self.host,
            port=self.port,
            log_level="warning",
        )
        server = uvicorn.Server(config)
        self._task = asyncio.create_task(server.serve())
        logger.info(f"[WaveMemory] WebUI started at http://{self.host}:{self.port}")

    def stop(self):
        if self._task:
            self._task.cancel()
