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

        @app.get("/maintain", response_class=HTMLResponse)
        async def maintain():
            maintain_path = Path(__file__).parent / "static" / "maintain.html"
            if maintain_path.exists():
                return maintain_path.read_text(encoding="utf-8")
            return "<h1>Wave Memory</h1><p>maintain.html not found</p>"

        # ─── Explore API（神经云图多视角）───

        @app.get("/api/explore/galaxy")
        async def explore_galaxy():
            """全局星图：社区聚类 + 核心节点。"""
            if not self.cooccurrence:
                return {"nodes": [], "edges": [], "communities": []}
            return self.cooccurrence.get_galaxy_data(max_nodes=300, max_edges=800)

        @app.get("/api/explore/community/{community_id}")
        async def explore_community(community_id: int, max_nodes: int = Query(50, ge=10, le=200)):
            """展开某个社区的详细节点。"""
            if not self.cooccurrence:
                return {"nodes": [], "edges": []}

            communities = self.cooccurrence.detect_communities(min_community_size=5)
            if community_id not in communities:
                return {"nodes": [], "edges": []}

            members = communities[community_id]

            # 度数
            degree: dict = {}
            for m in members:
                d = len(self.cooccurrence.forward.get(m, {})) + len(self.cooccurrence.backward.get(m, {}))
                degree[m] = d

            # 取 Top 节点
            sorted_members = sorted(members, key=lambda n: degree.get(n, 0), reverse=True)[:max_nodes]
            selected = set(sorted_members)

            # 边
            edges = []
            for src in selected:
                for tgt, w in self.cooccurrence.forward.get(src, {}).items():
                    if tgt in selected and w >= 0.03:
                        edges.append({"source": src, "target": tgt, "weight": round(w, 3)})

            # 节点信息
            if selected:
                placeholders = ",".join("?" * len(selected))
                rows = self.db.conn.execute(
                    f"SELECT id, name, tag_type FROM tags WHERE id IN ({placeholders})",
                    list(selected),
                ).fetchall()
                nodes = [{"id": r[0], "name": r[1], "type": r[2], "degree": degree.get(r[0], 0), "community": community_id} for r in rows]
            else:
                nodes = []

            return {"nodes": nodes, "edges": edges}

        @app.get("/api/explore/person/{qq_id}")
        async def explore_person(qq_id: str, max_memories: int = Query(80, ge=10, le=200)):
            """人物记忆网络：该人物的记忆 + 记忆间通过共享 Tag 连线。"""
            # 获取人物信息
            person = self.db.conn.execute(
                "SELECT qq_id, display_name, message_count FROM person_registry WHERE qq_id = ?",
                (qq_id,),
            ).fetchone()
            if not person:
                return {"person": None, "nodes": [], "edges": []}

            # 获取该人物的记忆
            rows = self.db.conn.execute(
                """SELECT m.id, m.content, m.sender_name, m.timestamp
                   FROM memories m
                   WHERE m.sender_id = ?
                   ORDER BY m.timestamp DESC LIMIT ?""",
                (qq_id, max_memories),
            ).fetchall()

            if not rows:
                return {"person": {"id": qq_id, "name": person[1], "count": person[2]}, "nodes": [], "edges": []}

            mem_ids = [r[0] for r in rows]
            nodes = [{"id": f"m{r[0]}", "memId": r[0], "name": (r[2] or "")[:6] + ": " + (r[1] or "")[:20], "content": r[1] or "", "sender": r[2] or "", "ts": r[3], "type": "memory"} for r in rows]

            # 获取这些记忆的 Tag
            if mem_ids:
                placeholders = ",".join("?" * len(mem_ids))
                tag_rows = self.db.conn.execute(
                    f"""SELECT mt.memory_id, t.id, t.name, t.tag_type
                        FROM memory_tags mt JOIN tags t ON mt.tag_id = t.id
                        WHERE mt.memory_id IN ({placeholders})""",
                    mem_ids,
                ).fetchall()

                # 构建 memory → tags 映射
                from collections import defaultdict as ddict
                mem_tags: dict = ddict(set)
                tag_info: dict = {}
                for tr in tag_rows:
                    mem_tags[tr[0]].add(tr[1])
                    tag_info[tr[1]] = {"id": f"t{tr[1]}", "tagId": tr[1], "name": tr[2], "type": tr[3]}

                # 添加高频 Tag 节点（出现在 >= 2 条记忆中的 Tag）
                tag_count: dict = ddict(int)
                for tags in mem_tags.values():
                    for t in tags:
                        tag_count[t] += 1

                shared_tags = {t for t, c in tag_count.items() if c >= 2}
                for t in list(shared_tags)[:50]:
                    if t in tag_info:
                        nodes.append(tag_info[t])

                # 边：记忆 → 共享 Tag
                edges = []
                for mid, tags in mem_tags.items():
                    for t in tags:
                        if t in shared_tags:
                            edges.append({"source": f"m{mid}", "target": f"t{t}", "weight": 0.5})
            else:
                edges = []

            return {
                "person": {"id": qq_id, "name": person[1], "count": person[2]},
                "nodes": nodes,
                "edges": edges,
            }

        @app.get("/api/explore/persons")
        async def explore_persons(limit: int = Query(30, ge=5, le=100)):
            """人物列表（按消息数排序）。"""
            rows = self.db.conn.execute(
                "SELECT qq_id, display_name, message_count FROM person_registry ORDER BY message_count DESC LIMIT ?",
                (limit,),
            ).fetchall()
            return [{"id": r[0], "name": r[1], "count": r[2]} for r in rows]

        @app.post("/api/explore/path")
        async def explore_path(request: Request):
            """路径查找：两个 Tag 之间的最短路径（BFS）。"""
            body = await request.json()
            source_id = body.get("source_id")
            target_id = body.get("target_id")
            max_depth = body.get("max_depth", 5)

            if not source_id or not target_id or not self.cooccurrence:
                return {"path": [], "nodes": [], "edges": []}

            # BFS
            from collections import deque
            visited = {source_id: None}
            queue = deque([(source_id, 0)])
            found = False

            while queue:
                current, depth = queue.popleft()
                if depth >= max_depth:
                    continue
                neighbors = list(self.cooccurrence.forward.get(current, {}).keys()) + \
                            list(self.cooccurrence.backward.get(current, {}).keys())
                for neighbor in neighbors:
                    if neighbor not in visited:
                        visited[neighbor] = current
                        if neighbor == target_id:
                            found = True
                            break
                        queue.append((neighbor, depth + 1))
                if found:
                    break

            if not found:
                return {"path": [], "nodes": [], "edges": [], "message": "未找到路径"}

            # 回溯路径
            path = []
            current = target_id
            while current is not None:
                path.append(current)
                current = visited[current]
            path.reverse()

            # 获取路径节点信息
            if path:
                placeholders = ",".join("?" * len(path))
                rows = self.db.conn.execute(
                    f"SELECT id, name, tag_type FROM tags WHERE id IN ({placeholders})",
                    path,
                ).fetchall()
                nodes = [{"id": r[0], "name": r[1], "type": r[2], "onPath": True} for r in rows]
            else:
                nodes = []

            # 路径上的边
            edges = []
            for i in range(len(path) - 1):
                w = self.cooccurrence.forward.get(path[i], {}).get(path[i+1], 0) or \
                    self.cooccurrence.backward.get(path[i], {}).get(path[i+1], 0)
                edges.append({"source": path[i], "target": path[i+1], "weight": round(w, 3)})

            return {"path": path, "nodes": nodes, "edges": edges}

        # ─── Stats ───

        @app.get("/api/stats")
        async def get_stats():
            total = self.db.get_memory_count()
            with_vec = self.db.get_memory_count_with_vector()
            tags = self.db.get_tag_count()
            groups = self.db.get_group_list()
            today_new = self.db.get_today_new_count()
            cooc_edges = self.cooccurrence.edge_count if self.cooccurrence else 0

            # 新功能状态
            facts_count = self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            mood_count = self.db.conn.execute("SELECT COUNT(*) FROM bot_mood").fetchone()[0]
            active_moods = self.db.conn.execute(
                "SELECT group_id, mood_type, description FROM bot_mood WHERE is_active = 1",
            ).fetchall()

            return {
                "total_memories": total,
                "memories_with_vector": with_vec,
                "total_tags": tags,
                "total_groups": len(groups),
                "today_new": today_new,
                "cooccurrence_edges": cooc_edges,
                "groups": groups,
                "facts_count": facts_count,
                "mood_history_count": mood_count,
                "active_moods": [
                    {"group_id": m[0], "mood_type": m[1], "description": m[2]}
                    for m in active_moods
                ],
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
        async def get_tag_graph(query: str = Query(None), limit: int = Query(30, ge=5, le=100)):
            """返回有向共现图数据（兼容 vis-network）。支持 query 参数做子图查询。"""
            # 优先从 DirectedCooccurrence 获取有向边
            if hasattr(self, 'cooccurrence') and self.cooccurrence and hasattr(self.cooccurrence, 'forward'):
                cooc = self.cooccurrence

                # 如果有 query，找到匹配的 tag 并返回其邻居子图
                if query:
                    # 找到匹配的 tag
                    matched_rows = self.db.conn.execute(
                        "SELECT id, name, tag_type FROM tags WHERE name LIKE ? LIMIT 5",
                        (f"%{query}%",),
                    ).fetchall()
                    if not matched_rows:
                        return {"nodes": [], "edges": [], "directed": True}

                    # 收集种子 tag 及其邻居
                    seed_ids = set(r[0] for r in matched_rows)
                    neighbor_ids = set()
                    edges = []
                    for seed_id in seed_ids:
                        if seed_id in cooc.forward:
                            for tgt_id, weight in sorted(cooc.forward[seed_id].items(), key=lambda x: x[1], reverse=True)[:limit]:
                                edges.append({"from": seed_id, "to": tgt_id, "value": round(weight, 3), "direction": "forward"})
                                neighbor_ids.add(tgt_id)
                        # 也看反向边
                        if hasattr(cooc, 'backward') and seed_id in cooc.backward:
                            for src_id, weight in sorted(cooc.backward[seed_id].items(), key=lambda x: x[1], reverse=True)[:limit]:
                                edges.append({"from": src_id, "to": seed_id, "value": round(weight, 3), "direction": "forward"})
                                neighbor_ids.add(src_id)

                    # 获取所有相关节点信息
                    all_ids = list(seed_ids | neighbor_ids)[:200]
                    if not all_ids:
                        return {"nodes": [], "edges": [], "directed": True}
                    placeholders = ",".join("?" * len(all_ids))
                    tag_rows = self.db.conn.execute(
                        f"""SELECT t.id, t.name, t.tag_type,
                                  (SELECT COUNT(*) FROM memory_tags mt WHERE mt.tag_id = t.id) as mem_count
                           FROM tags t WHERE t.id IN ({placeholders})""",
                        all_ids,
                    ).fetchall()
                    nodes = [{"id": r[0], "label": r[1], "type": r[2] or "keyword", "value": r[3], "isSeed": r[0] in seed_ids} for r in tag_rows]
                    return {"nodes": nodes, "edges": edges, "directed": True}
                edges = []
                tag_ids_in_edges = set()
                for src_id, neighbors in cooc.forward.items():
                    for tgt_id, weight in sorted(neighbors.items(), key=lambda x: x[1], reverse=True)[:10]:
                        edges.append({
                            "from": src_id,
                            "to": tgt_id,
                            "value": round(weight, 3),
                            "direction": "forward",
                        })
                        tag_ids_in_edges.add(src_id)
                        tag_ids_in_edges.add(tgt_id)
                        if len(edges) >= 400:
                            break
                    if len(edges) >= 400:
                        break

                # 确保每种类型都有代表性节点：每种类型取关联记忆最多的 top N
                type_supplement_ids = set()
                for ttype in ('person', 'topic', 'event', 'emotion', 'entity', 'fact', 'location'):
                    type_top = self.db.conn.execute(
                        """SELECT t.id FROM tags t
                           WHERE t.tag_type = ?
                           ORDER BY (SELECT COUNT(*) FROM memory_tags mt WHERE mt.tag_id = t.id) DESC
                           LIMIT 15""",
                        (ttype,),
                    ).fetchall()
                    for row in type_top:
                        type_supplement_ids.add(row[0])

                # 为补充节点添加共现边
                for sid in type_supplement_ids:
                    if sid in cooc.forward:
                        for tgt_id, weight in sorted(cooc.forward[sid].items(), key=lambda x: x[1], reverse=True)[:5]:
                            if tgt_id in tag_ids_in_edges or tgt_id in type_supplement_ids:
                                edges.append({"from": sid, "to": tgt_id, "value": round(weight, 3), "direction": "forward"})
                    if hasattr(cooc, 'backward') and sid in cooc.backward:
                        for src_id, weight in sorted(cooc.backward[sid].items(), key=lambda x: x[1], reverse=True)[:5]:
                            if src_id in tag_ids_in_edges or src_id in type_supplement_ids:
                                edges.append({"from": src_id, "to": sid, "value": round(weight, 3), "direction": "forward"})

                all_node_ids = tag_ids_in_edges | type_supplement_ids

                # 获取节点信息
                nodes = []
                if all_node_ids:
                    limited_ids = list(all_node_ids)[:300]
                    placeholders = ",".join("?" * len(limited_ids))
                    tag_rows = self.db.conn.execute(
                        f"""SELECT t.id, t.name, t.tag_type,
                                  (SELECT COUNT(*) FROM memory_tags mt WHERE mt.tag_id = t.id) as mem_count
                           FROM tags t WHERE t.id IN ({placeholders})""",
                        limited_ids,
                    ).fetchall()
                    nodes = [{"id": r[0], "label": r[1], "type": r[2] or "keyword", "value": r[3]} for r in tag_rows]

                return {"nodes": nodes, "edges": edges, "directed": True}
            else:
                # 退化：使用旧的无向图
                nodes, edges = self.db.get_tag_graph_data()
                return {"nodes": nodes, "edges": edges, "directed": False}

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

        @app.post("/api/tags/merge")
        async def merge_tags(body: dict):
            """合并多个 Tag 到目标 Tag。"""
            source_ids = body.get("source_ids", [])
            target_id = body.get("target_id")
            if not source_ids or not target_id:
                return {"error": "source_ids and target_id required"}
            if target_id in source_ids:
                return {"error": "target_id cannot be in source_ids"}

            # 获取目标 Tag 信息
            target = self.db.conn.execute(
                "SELECT id, name, aliases FROM tags WHERE id = ?", (target_id,)
            ).fetchone()
            if not target:
                return {"error": f"target tag {target_id} not found"}

            merged_names = []
            for src_id in source_ids:
                src = self.db.conn.execute(
                    "SELECT id, name FROM tags WHERE id = ?", (src_id,)
                ).fetchone()
                if not src:
                    continue
                merged_names.append(src[1])
                # 将 memory_tags 指向目标
                self.db.conn.execute(
                    "UPDATE OR IGNORE memory_tags SET tag_id = ? WHERE tag_id = ?",
                    (target_id, src_id),
                )
                # 删除冲突的重复关联
                self.db.conn.execute(
                    "DELETE FROM memory_tags WHERE tag_id = ?", (src_id,)
                )
                # 删除源 Tag
                self.db.conn.execute("DELETE FROM tags WHERE id = ?", (src_id,))

            # 更新目标 Tag 的 aliases
            existing_aliases = target[2] or ""
            all_aliases = [a for a in existing_aliases.split(",") if a] + merged_names
            self.db.conn.execute(
                "UPDATE tags SET aliases = ? WHERE id = ?",
                (",".join(all_aliases), target_id),
            )
            self.db.conn.commit()
            return {"merged": len(merged_names), "target_id": target_id, "aliases": all_aliases}

        @app.put("/api/tags/{tag_id}/core")
        async def set_tag_core(tag_id: int, body: dict = None):
            """标记/取消标记核心 Tag。"""
            body = body or {}
            is_core = body.get("is_core", True)
            self.db.conn.execute(
                "UPDATE tags SET is_core = ? WHERE id = ?", (1 if is_core else 0, tag_id)
            )
            self.db.conn.commit()
            return {"tag_id": tag_id, "is_core": is_core}

        @app.post("/api/tags/cleanup")
        async def cleanup_low_quality_tags():
            """清理低质 Tag（frequency=1 且 confidence<0.5）。"""
            # 找出低质 Tag
            low_quality = self.db.conn.execute("""
                SELECT id, name FROM tags
                WHERE frequency <= 1 AND confidence < 0.5
            """).fetchall()

            if not low_quality:
                return {"removed": 0}

            ids_to_remove = [r[0] for r in low_quality]
            placeholders = ",".join("?" * len(ids_to_remove))

            # 删除关联
            self.db.conn.execute(
                f"DELETE FROM memory_tags WHERE tag_id IN ({placeholders})", ids_to_remove
            )
            # 删除 Tag
            self.db.conn.execute(
                f"DELETE FROM tags WHERE id IN ({placeholders})", ids_to_remove
            )
            self.db.conn.commit()

            return {"removed": len(ids_to_remove), "names": [r[1] for r in low_quality[:20]]}

        @app.get("/api/tags/quality")
        async def tag_quality_stats():
            """Tag 质量统计。"""
            total_memories = self.db.conn.execute(
                "SELECT COUNT(*) FROM memories WHERE LENGTH(content) >= 10"
            ).fetchone()[0]
            with_tags = self.db.conn.execute(
                "SELECT COUNT(DISTINCT memory_id) FROM memory_tags"
            ).fetchone()[0]
            total_tags = self.db.conn.execute("SELECT COUNT(*) FROM tags").fetchone()[0]
            avg_tags = self.db.conn.execute(
                "SELECT AVG(cnt) FROM (SELECT COUNT(*) as cnt FROM memory_tags GROUP BY memory_id)"
            ).fetchone()[0] or 0

            # 类型分布
            type_dist = self.db.conn.execute(
                "SELECT tag_type, COUNT(*) FROM tags GROUP BY tag_type"
            ).fetchall()

            # 低质 Tag 数
            low_quality_count = self.db.conn.execute(
                "SELECT COUNT(*) FROM tags WHERE frequency <= 1 AND confidence < 0.5"
            ).fetchone()[0]

            # 核心 Tag 数
            core_count = self.db.conn.execute(
                "SELECT COUNT(*) FROM tags WHERE is_core = 1"
            ).fetchone()[0]

            coverage = with_tags / total_memories if total_memories > 0 else 0

            return {
                "coverage": round(coverage, 4),
                "total_memories": total_memories,
                "memories_with_tags": with_tags,
                "total_tags": total_tags,
                "avg_tags_per_memory": round(avg_tags, 2),
                "type_distribution": {r[0] or "unknown": r[1] for r in type_dist},
                "low_quality_count": low_quality_count,
                "core_tag_count": core_count,
            }

        @app.get("/api/tags/residuals")
        async def get_tag_residuals():
            """返回 Tag 内生残差数据（供热力图使用）。"""
            rows = self.db.conn.execute("""
                SELECT t.id, t.name, t.frequency, COALESCE(r.residual_energy, 0.5) as residual
                FROM tags t
                LEFT JOIN tag_intrinsic_residuals r ON t.id = r.tag_id
                ORDER BY residual DESC
                LIMIT 200
            """).fetchall()
            return {
                "items": [
                    {"id": r[0], "name": r[1], "frequency": r[2], "residual": round(r[3], 4)}
                    for r in rows
                ],
                "total": len(rows),
            }

        # ─── Tag 审计工作台 API ───

        @app.get("/api/tags/audit/trigger")
        async def trigger_audit(
            strategy: str = Query("mixed"),
            batch_size: int = Query(50),
            total_count: int = Query(500),
        ):
            """触发 LLM Tag 审计任务（SSE 流式返回进度）。"""
            from fastapi.responses import StreamingResponse
            from ..services.tag_auditor import TagAuditor

            if not self.tag_extractor or not self.tag_extractor.provider_id:
                return {"error": "No LLM provider configured"}

            # 并发保护
            if getattr(self, '_audit_running', False):
                return {"error": "Audit already in progress"}
            self._audit_running = True

            auditor = TagAuditor(
                db=self.db,
                context=self.tag_extractor.context,
                provider_id=self.tag_extractor.provider_id,
            )

            async def event_stream():
                try:
                    async for event in auditor.run_audit(batch_size=batch_size, strategy=strategy, total_count=total_count):
                        yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                finally:
                    self._audit_running = False

            return StreamingResponse(event_stream(), media_type="text/event-stream")

        @app.get("/api/tags/audit/suggestions")
        async def get_audit_suggestions(
            status: str = Query("pending"),
            limit: int = Query(50),
            offset: int = Query(0),
            action: str = Query(None),
        ):
            """获取审计建议列表。"""
            from ..services.tag_auditor import TagAuditor
            auditor = TagAuditor(db=self.db)
            suggestions = auditor.get_suggestions(status=status, limit=limit, offset=offset, action=action)
            counts = auditor.get_suggestion_counts()
            return {"suggestions": suggestions, "counts": counts}

        @app.post("/api/tags/audit/resolve")
        async def resolve_audit_suggestion(request: Request):
            """批准或拒绝审计建议。"""
            from ..services.tag_auditor import TagAuditor
            body = await request.json()
            suggestion_id = body.get("suggestion_id")
            decision = body.get("decision")  # "approve" or "reject"

            if not suggestion_id or decision not in ("approve", "reject"):
                return {"error": "suggestion_id and decision (approve/reject) required"}

            auditor = TagAuditor(db=self.db)
            result = auditor.resolve_suggestion(suggestion_id, decision)
            return result

        @app.post("/api/tags/audit/resolve-batch")
        async def resolve_audit_batch(request: Request):
            """批量处理审计建议。"""
            from ..services.tag_auditor import TagAuditor
            body = await request.json()
            items = body.get("items", [])  # [{"id": 1, "decision": "approve"}, ...]

            # 兼容前端简化格式: {suggestion_ids: [...], decision: "approve"}
            if not items:
                ids = body.get("suggestion_ids", [])
                decision = body.get("decision")
                if ids and decision:
                    items = [{"id": sid, "decision": decision} for sid in ids]

            if not items:
                return {"error": "items or suggestion_ids+decision required"}

            auditor = TagAuditor(db=self.db)
            results = []
            for item in items:
                sid = item.get("id")
                decision = item.get("decision")
                if sid and decision in ("approve", "reject"):
                    r = auditor.resolve_suggestion(sid, decision)
                    results.append(r)

            return {"processed": len(results), "results": results}

        @app.post("/api/tags/retype")
        async def retype_tag(request: Request):
            """手动修改 Tag 类型。"""
            body = await request.json()
            tag_id = body.get("tag_id")
            new_type = body.get("new_type")

            if not tag_id or not new_type:
                return {"error": "tag_id and new_type required"}

            valid_types = {"keyword", "topic", "event", "entity", "fact", "emotion", "person", "location", "time"}
            if new_type not in valid_types:
                return {"error": f"Invalid type. Valid: {sorted(valid_types)}"}

            self.db.conn.execute("UPDATE tags SET tag_type = ? WHERE id = ?", (new_type, tag_id))
            self.db.conn.commit()
            return {"tag_id": tag_id, "new_type": new_type}

        @app.post("/api/tags/batch-delete")
        async def batch_delete_tags(request: Request):
            """批量删除 Tag。"""
            body = await request.json()
            tag_ids = body.get("tag_ids", [])

            if not tag_ids:
                return {"error": "tag_ids required"}

            placeholders = ",".join("?" * len(tag_ids))
            # 删除关联
            self.db.conn.execute(f"DELETE FROM memory_tags WHERE tag_id IN ({placeholders})", tag_ids)
            self.db.conn.execute(
                f"DELETE FROM tag_relations WHERE source_tag_id IN ({placeholders}) OR target_tag_id IN ({placeholders})",
                tag_ids + tag_ids
            )
            # 删除 tag
            self.db.conn.execute(f"DELETE FROM tags WHERE id IN ({placeholders})", tag_ids)
            self.db.conn.commit()
            return {"deleted": len(tag_ids)}

        @app.post("/api/tags/rename")
        async def rename_tag(request: Request):
            """重命名 Tag。"""
            body = await request.json()
            tag_id = body.get("tag_id")
            new_name = body.get("new_name", "").strip()

            if not tag_id or not new_name:
                return {"error": "tag_id and new_name required"}

            # 检查是否已存在同名 tag
            existing = self.db.conn.execute("SELECT id FROM tags WHERE name = ? AND id != ?", (new_name, tag_id)).fetchone()
            if existing:
                return {"error": f"Tag '{new_name}' already exists (id={existing[0]})"}

            old_name = self.db.conn.execute("SELECT name FROM tags WHERE id = ?", (tag_id,)).fetchone()
            if not old_name:
                return {"error": f"Tag {tag_id} not found"}

            # 旧名加入 aliases
            aliases_row = self.db.conn.execute("SELECT aliases FROM tags WHERE id = ?", (tag_id,)).fetchone()
            old_aliases = (aliases_row[0] or "").split(",") if aliases_row and aliases_row[0] else []
            if old_name[0] not in old_aliases:
                old_aliases.append(old_name[0])
            old_aliases = [a for a in old_aliases if a and a != new_name]

            self.db.conn.execute(
                "UPDATE tags SET name = ?, aliases = ? WHERE id = ?",
                (new_name, ",".join(old_aliases), tag_id)
            )
            self.db.conn.commit()
            return {"tag_id": tag_id, "old_name": old_name[0], "new_name": new_name}

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
        async def batch_extract_tags(batch_size: int = Query(50, ge=1, le=500)):
            """后台批量为无 Tag 的记忆提取 Tag（SSE 流）。"""
            from fastapi.responses import StreamingResponse

            if _import_lock.locked():
                return StreamingResponse(
                    iter([f"data: {json.dumps({'error': '另一个导入/提取任务正在运行，请等待完成后再试'})}\n\n"]),
                    media_type="text/event-stream"
                )

            async def run_batch():
                async with _import_lock:
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

            # 新功能状态
            facts_count = self.db.conn.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
            active_moods = self.db.conn.execute(
                "SELECT group_id, mood_type, intensity, description FROM bot_mood WHERE is_active = 1",
            ).fetchall()
            person_count = self.db.conn.execute("SELECT COUNT(*) FROM person_registry").fetchone()[0]
            user_profiles_count = self.db.conn.execute("SELECT COUNT(*) FROM user_profiles").fetchone()[0]

            return {
                "memories": {"total": total_mem, "with_vector": with_vec, "with_tags": tagged_memories},
                "tags": {"total": total_tags, "structured": structured_tags, "type_distribution": {r[0]: r[1] for r in type_dist}},
                "coverage": {"vector_pct": round(with_vec / total_mem * 100, 1) if total_mem > 0 else 0, "tag_pct": round(tagged_memories / total_mem * 100, 1) if total_mem > 0 else 0},
                "cooccurrence": {"nodes": cooc_nodes, "edges": cooc_edges},
                "epa": {"initialized": self.epa.initialized if self.epa else False},
                "lifecycle": {
                    "facts": facts_count,
                    "persons": person_count,
                    "user_profiles": user_profiles_count,
                    "active_moods": [
                        {"group_id": m[0], "type": m[1], "intensity": m[2], "desc": m[3]}
                        for m in active_moods
                    ],
                },
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
                "lifecycle": cfg.get("Lifecycle_Settings", {}),
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
                "lifecycle": "Lifecycle_Settings",
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

        # ─── 热调参 API ───

        @app.get("/api/config/hot")
        async def get_hot_config():
            """返回可热调参数及其当前值和范围。"""
            from ..services.hot_config import HotConfig
            hot = HotConfig()
            params = hot.get_tunable_params()
            current = hot.get_all()
            for p in params:
                p["current"] = hot.get(p["key"], p["default"])
            return {"params": params, "config": current}

        @app.post("/api/config/hot")
        async def update_hot_config(request: Request):
            """热更新参数（无需重启）。"""
            from ..services.hot_config import HotConfig
            body = await request.json()
            hot = HotConfig()

            # 校验范围
            params_meta = {p["key"]: p for p in hot.get_tunable_params()}
            validated = {}
            errors = []

            for key, value in body.items():
                if key not in params_meta:
                    errors.append(f"Unknown param: {key}")
                    continue
                meta = params_meta[key]
                try:
                    if meta["type"] == "float":
                        value = float(value)
                    elif meta["type"] == "int":
                        value = int(value)
                    if value < meta["min"] or value > meta["max"]:
                        errors.append(f"{key}: value {value} out of range [{meta['min']}, {meta['max']}]")
                        continue
                    validated[key] = value
                except (ValueError, TypeError) as e:
                    errors.append(f"{key}: invalid value - {e}")

            if validated:
                hot.update(validated)

            return {"ok": len(errors) == 0, "updated": list(validated.keys()), "errors": errors}

        # ─── 查询诊断 API ───

        @app.post("/api/query/debug")
        async def query_debug(req: QueryRequest):
            """诊断查询：返回各阶段中间结果和耗时。"""
            import numpy as np

            text = req.text
            timing = {}
            debug_info = {}

            t0 = time.time()

            # Embedding
            query_vec = await self.embedding_service.get_embedding(text)
            timing["embedding_ms"] = (time.time() - t0) * 1000

            if query_vec is None:
                return {"error": "Embedding failed", "timing": timing}

            # EPA
            t1 = time.time()
            epa_result = {}
            if self.epa and hasattr(self.epa, "initialized") and self.epa.initialized:
                epa_result = self.epa.analyze(query_vec)
            timing["epa_ms"] = (time.time() - t1) * 1000
            debug_info["epa"] = epa_result

            # Tag 匹配
            t2 = time.time()
            matched_tags = []
            if self.tag_index and self.tag_index.count >= 10:
                tag_results = self.tag_index.search(query_vec, k=10)
                for tid, dist in tag_results:
                    sim = 1.0 - dist
                    if sim > 0.2:
                        matched_tags.append({"tag_id": tid, "similarity": round(sim, 4)})
            timing["tag_match_ms"] = (time.time() - t2) * 1000
            debug_info["matched_tags"] = matched_tags[:10]

            # Spike 传播
            t3 = time.time()
            spike_result = {}
            if self.spike_router and matched_tags:
                seed_tags = [{"tag_id": t["tag_id"], "weight": t["similarity"]} for t in matched_tags[:10]]
                spike_result = self.spike_router.propagate(seed_tags, epa_result=epa_result)
                # 序列化
                spike_result = {
                    "activated_count": len(spike_result.get("activated_tags", [])),
                    "emergent_count": sum(1 for t in spike_result.get("activated_tags", []) if t.get("is_emergent")),
                    "energy_field_size": len(spike_result.get("energy_field", {})),
                    "top_energies": sorted(
                        [{"tag_id": k, "energy": round(v, 4)} for k, v in spike_result.get("energy_field", {}).items()],
                        key=lambda x: x["energy"], reverse=True
                    )[:10],
                }
            timing["spike_ms"] = (time.time() - t3) * 1000
            debug_info["spike"] = spike_result

            # 检索
            t4 = time.time()
            results = self.memory_index.search(query_vec, k=10)
            timing["search_ms"] = (time.time() - t4) * 1000
            debug_info["raw_results"] = len(results)

            timing["total_ms"] = (time.time() - t0) * 1000

            return {"timing": timing, "debug": debug_info}

        # ─── LLM Tag 提取（使用配置中固定的 provider） ───

        @app.post("/api/import/llm-extract")
        async def llm_import_extract(
            batch_size: int = Query(500, ge=1, le=500),
            limit: int = Query(20000, ge=1, le=50000),
        ):
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
                       ORDER BY m.id DESC LIMIT ?""",
                    (limit,),
                ).fetchall()

                total = len(rows)
                if total == 0:
                    yield f"data: {json.dumps({'progress': 1.0, 'message': 'All memories already have tags'})}\n\n"
                    return

                logger.info(f"[WaveMemory] LLM tag extraction started: total={total}, batch_size={batch_size}, provider={extractor.provider_id}")
                yield f"data: {json.dumps({'progress': 0, 'total': total, 'provider': extractor.provider_id, 'message': f'Starting LLM tag extraction ({total} memories, batch={batch_size})...'})}\n\n"

                processed = 0
                tagged = 0
                no_tags = 0
                errors = 0

                for i in range(0, total, batch_size):
                    batch = rows[i:i + batch_size]
                    messages = [
                        {"id": mem_id, "content": content[:800], "sender": sender_name or ""}
                        for mem_id, content, sender_name in batch
                    ]
                    try:
                        batch_tags = await extractor.extract_tags_batch(messages)
                    except Exception as e:
                        logger.warning(f"[WaveMemory] Batch tag extraction error: {e}")
                        batch_tags = [[] for _ in batch]
                        errors += len(batch)

                    # 防御：LLM 返回数量异常时补齐/截断，避免错位
                    if len(batch_tags) < len(batch):
                        batch_tags.extend([[] for _ in range(len(batch) - len(batch_tags))])
                    elif len(batch_tags) > len(batch):
                        batch_tags = batch_tags[:len(batch)]

                    for (mem_id, _content, _sender_name), tags in zip(batch, batch_tags):
                        try:
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
                            else:
                                no_tags += 1
                        except Exception as e:
                            errors += 1
                            if errors <= 3:
                                logger.warning(f"[WaveMemory] Link tags failed for memory {mem_id}: {e}")
                        processed += 1

                    yield f"data: {json.dumps({'progress': round(processed/total, 3), 'processed': processed, 'total': total, 'tagged': tagged, 'no_tags': no_tags, 'errors': errors, 'message': f'{processed}/{total} (标记:{tagged} 空:{no_tags} 失败:{errors})'})}\n\n"
                    import asyncio
                    await asyncio.sleep(0.05)

                logger.info(f"[WaveMemory] LLM tag extraction done: processed={processed}, tagged={tagged}, no_tags={no_tags}, errors={errors}")
                yield f"data: {json.dumps({'progress': 1.0, 'processed': total, 'total': total, 'tagged': tagged, 'no_tags': no_tags, 'errors': errors, 'message': f'Complete: {tagged}/{total} tagged, {no_tags} empty, {errors} errors'})}\n\n"

            return StreamingResponse(run(), media_type="text/event-stream")

        # ─── 数据源发现（通用，带缓存） ───

        _sources_cache = {"data": None, "ts": 0}
        _import_lock = asyncio.Lock()  # 导入/提取互斥锁

        @app.get("/api/import/sources")
        async def discover_sources(refresh: bool = False):
            """通用数据源发现 — 结果缓存 10min，?refresh=true 强制刷新。"""
            now = time.time()
            if not refresh and _sources_cache["data"] and now - _sources_cache["ts"] < 600:
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
        async def import_from_source(source_id: str = Query(...), limit: int = Query(5000, ge=1, le=50000)):
            """从指定数据源导入记忆到 Wave Memory（SSE 流）。"""
            from fastapi.responses import StreamingResponse
            from .source_discovery import SourceDiscovery, UniversalImporter

            if _import_lock.locked():
                return StreamingResponse(
                    iter([f"data: {json.dumps({'error': '另一个导入/提取任务正在运行，请等待完成后再试'})}\n\n"]),
                    media_type="text/event-stream"
                )

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
                async with _import_lock:
                    if source["type"] == "known":
                        async for event in importer.import_known(source, limit=limit):
                            yield f"data: {event}\n\n"
                    elif source.get("llm_mapping"):
                        async for event in importer.import_with_llm_mapping(source, source["llm_mapping"], limit=limit):
                            yield f"data: {event}\n\n"
                    else:
                        # 未知源且无 LLM mapping — 尝试启发式导入
                        analysis = source.get("analysis", {})
                        importable = analysis.get("importable_tables", [])
                        if importable:
                            table_info = importable[0]
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
