"""KnowledgeGraph Blueprint — 统一知识图谱查询层 (M1)

从 facts + tag_relations 聚合语义图谱，替代 cooccurrence 统计共现。
不改底层表结构，纯查询层虚拟图。
"""

from __future__ import annotations

import json
import time

from quart import Blueprint, jsonify, request

from ..container import get_container
from ..middleware.auth import require_auth

kg_bp = Blueprint("kg", __name__, url_prefix="/api/kg")

# 全景图缓存（按 facts+relations 行数版本缓存）
_overview_cache: dict = {"version": None, "data": None, "ts": 0}
_CACHE_TTL = 120  # 2 分钟


def _table_exists(conn, table: str) -> bool:
    """检查表是否存在。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


@kg_bp.route("/overview")
@require_auth
async def overview():
    """全景知识图谱（可配置）。

    参数：
    - max_nodes: 最大节点数(50-500, default 150)
    - min_weight: 最小边权重阈值(0-5, default 0.5)
    - relation_types: 逗号分隔的关系类型筛选(空=全部)
    - node_types: 逗号分隔的节点类型(空=全部)
    - days: 时间范围(7/30/90/0=全部, default 0)
    """
    c = get_container()
    try:
        max_nodes = int(request.args.get("max_nodes", 150))
    except (ValueError, TypeError):
        max_nodes = 150
    max_nodes = max(30, min(500, max_nodes))
    try:
        min_weight = float(request.args.get("min_weight", 0.5))
    except (ValueError, TypeError):
        min_weight = 0.5
    relation_types_raw = request.args.get("relation_types", "")
    node_types_raw = request.args.get("node_types", "")
    try:
        days = int(request.args.get("days", 0))
    except (ValueError, TypeError):
        days = 0

    relation_filter = set(relation_types_raw.split(",")) - {""} if relation_types_raw else None
    node_filter = set(node_types_raw.split(",")) - {""} if node_types_raw else None

    # 版本缓存 key 包含参数
    now = time.time()
    cache_key = f"{max_nodes}:{min_weight}:{relation_types_raw}:{node_types_raw}:{days}"
    if not _table_exists(c.db.conn, "facts") or not _table_exists(c.db.conn, "tag_relations"):
        return jsonify({"nodes": [], "edges": []})
    try:
        version = c.db.conn.execute(
            "SELECT (SELECT COUNT(*) FROM facts) + (SELECT COUNT(*) FROM tag_relations)"
        ).fetchone()[0]
    except Exception:
        version = 0
    full_version = f"{version}:{cache_key}"
    if _overview_cache["version"] == full_version and _overview_cache["data"] and (now - _overview_cache["ts"]) < _CACHE_TTL:
        return jsonify(_overview_cache["data"])

    # 时间过滤
    time_cond = ""
    time_param: list = []
    if days > 0:
        cutoff = now - days * 86400
        time_cond = " AND created_at >= ?"
        time_param = [cutoff]

    # Step 1: 从 facts 提取实体和边
    fact_rows = c.db.conn.execute(
        f"SELECT subject, predicate, object, confidence FROM facts WHERE 1=1{time_cond} ORDER BY confidence DESC LIMIT 3000",
        time_param,
    ).fetchall()

    # Step 2: 从 tag_relations 提取实体和边
    rel_weight_cond = f" AND tr.weight >= {min_weight}" if min_weight > 0 else ""
    rel_rows = c.db.conn.execute(
        f"""SELECT t1.name, tr.relation_type, t2.name, tr.weight, t1.tag_type, t2.tag_type
           FROM tag_relations tr
           JOIN tags t1 ON tr.source_tag_id = t1.id
           JOIN tags t2 ON tr.target_tag_id = t2.id
           WHERE 1=1{rel_weight_cond}
           ORDER BY tr.weight DESC LIMIT 3000"""
    ).fetchall()

    # Step 3: 构建实体度数表
    entity_degree: dict[str, int] = {}
    entity_type: dict[str, str] = {}
    edges_raw: list[tuple] = []

    for subj, pred, obj, conf in fact_rows:
        if not subj or not obj:
            continue
        subj = subj.strip()[:30]
        obj = obj.strip()[:30]
        label = pred.strip()[:20] if pred else "relates"
        if relation_filter and label not in relation_filter and "fact" not in relation_filter:
            continue
        entity_degree[subj] = entity_degree.get(subj, 0) + 1
        entity_degree[obj] = entity_degree.get(obj, 0) + 1
        entity_type.setdefault(subj, "entity")
        entity_type.setdefault(obj, "entity")
        edges_raw.append((subj, obj, label, float(conf or 1)))

    for src_name, rel_type, tgt_name, weight, src_type, tgt_type in rel_rows:
        if not src_name or not tgt_name:
            continue
        src_name = src_name.strip()[:30]
        tgt_name = tgt_name.strip()[:30]
        label = rel_type or "relates"
        if relation_filter and label not in relation_filter:
            continue
        entity_degree[src_name] = entity_degree.get(src_name, 0) + 1
        entity_degree[tgt_name] = entity_degree.get(tgt_name, 0) + 1
        entity_type.setdefault(src_name, src_type or "topic")
        entity_type.setdefault(tgt_name, tgt_type or "topic")
        edges_raw.append((src_name, tgt_name, label, float(weight or 1)))

    # ═══ 以边为中心构图（修复稀疏图"一坨"问题）═══
    # 先选 top 边 → 再从边端点建节点 → 保证每个节点至少有一条边 → 图有结构

    # Step 4: 实体消歧（name→QQ 合并）
    name_to_qq: dict[str, str] = {}
    qq_to_main: dict[str, str] = {}  # qq → 主名
    try:
        sender_rows = c.db.conn.execute(
            """SELECT sender_name, sender_id, COUNT(*) as cnt FROM memories
               WHERE sender_id != '' AND sender_name != ''
               GROUP BY sender_name, sender_id ORDER BY cnt DESC"""
        ).fetchall()
        for sname, sid, cnt in sender_rows:
            key = sname.strip()[:30]
            name_to_qq[key] = sid
            if sid not in qq_to_main:
                qq_to_main[sid] = key  # 第一个（最高频）作为主名
    except Exception:
        pass

    def resolve_name(n: str) -> str:
        """消歧：同 QQ 的名字合并到主名。"""
        qq = name_to_qq.get(n)
        if qq:
            return qq_to_main.get(qq, n)
        return n

    # Step 5: 构建边（消歧后），按权重排序取 top
    max_edges = max_nodes * 2
    edge_list: list[tuple[str, str, str, float]] = []  # (src, tgt, label, weight)
    seen_pairs: set = set()

    for src, tgt, label, weight in edges_raw:
        src_r = resolve_name(src)
        tgt_r = resolve_name(tgt)
        if src_r == tgt_r:
            continue  # 自环（消歧后同一实体）
        pair = (src_r, tgt_r, label)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        edge_list.append((src_r, tgt_r, label, weight))

    # 按权重排序取 top
    edge_list.sort(key=lambda x: x[3], reverse=True)
    edge_list = edge_list[:max_edges]

    # Step 6: 从边端点构建节点集
    node_degree: dict[str, int] = {}
    for src, tgt, _, _ in edge_list:
        node_degree[src] = node_degree.get(src, 0) + 1
        node_degree[tgt] = node_degree.get(tgt, 0) + 1

    # 限制节点数（优先保留高度数）
    if len(node_degree) > max_nodes:
        sorted_nd = sorted(node_degree.items(), key=lambda x: x[1], reverse=True)[:max_nodes]
        top_set = {n for n, _ in sorted_nd}
        # 过滤边：只保留两端都在 top 里的
        edge_list = [(s, t, l, w) for s, t, l, w in edge_list if s in top_set and t in top_set]
        # 重算度数
        node_degree = {}
        for src, tgt, _, _ in edge_list:
            node_degree[src] = node_degree.get(src, 0) + 1
            node_degree[tgt] = node_degree.get(tgt, 0) + 1
    else:
        top_set = set(node_degree.keys())

    # Step 7: 构建节点（按 node_filter 筛选）
    nodes = []
    name_to_id: dict[str, int] = {}
    for idx, (name, degree) in enumerate(sorted(node_degree.items(), key=lambda x: x[1], reverse=True)):
        etype = "person" if name in name_to_qq else entity_type.get(name, "entity")
        if node_filter and etype not in node_filter:
            continue
        nid = idx + 1
        name_to_id[name] = nid
        nodes.append({"id": nid, "name": name, "type": etype, "degree": degree})

    # Step 8: 构建边（映射到 node id）
    edges = []
    for src, tgt, label, weight in edge_list:
        src_id = name_to_id.get(src)
        tgt_id = name_to_id.get(tgt)
        if src_id and tgt_id:
            edges.append({"source": src_id, "target": tgt_id, "label": label, "weight": round(weight, 2)})

    data = {"nodes": nodes, "edges": edges}
    _overview_cache.update({"version": full_version, "data": data, "ts": now})
    return jsonify(data)


@kg_bp.route("/entity/<entity_name>")
@require_auth
async def entity_detail(entity_name: str):
    """实体详情：该实体相关的 facts + tag_relations + 关联记忆 + 人物画像(若为人物)。"""
    c = get_container()
    from urllib.parse import unquote
    name = unquote(entity_name).strip()
    limit = int(request.args.get("limit", 15))

    # 人物检测：通过 sender_name 反查 QQ
    person = None
    person_row = c.db.conn.execute(
        "SELECT sender_id, COUNT(*) FROM memories WHERE sender_name = ? AND sender_id != '' GROUP BY sender_id ORDER BY 2 DESC LIMIT 1",
        (name,),
    ).fetchone()
    if person_row:
        qq_id = person_row[0]
        # 聚合所有别名
        aliases = [r[0] for r in c.db.conn.execute(
            "SELECT DISTINCT sender_name FROM memories WHERE sender_id = ? AND sender_name != ''", (qq_id,)
        ).fetchall()]
        msg_count = c.db.conn.execute("SELECT COUNT(*) FROM memories WHERE sender_id = ?", (qq_id,)).fetchone()[0]
        # 好感度 + personality_tags（取最新/最高）
        profile = c.db.conn.execute(
            "SELECT affection, personality_tags, nickname FROM user_profiles WHERE user_id = ? ORDER BY affection DESC LIMIT 1",
            (qq_id,),
        ).fetchone()
        person = {
            "qq_id": qq_id,
            "name": name,
            "aliases": [a for a in aliases if a != name],
            "msg_count": msg_count,
            "affection": profile[0] if profile else 0,
            "personality_tags": json.loads(profile[1] or "[]") if profile and profile[1] else [],
        }

    # 相关 facts（如果是人物，搜所有别名）
    search_names = [name] + (person["aliases"] if person else [])
    facts_all = []
    for n in search_names[:5]:
        rows = c.db.conn.execute(
            "SELECT rowid, subject, predicate, object, confidence FROM facts WHERE subject = ? OR object = ? LIMIT ?",
            (n, n, limit),
        ).fetchall()
        facts_all.extend(rows)
    # 去重
    seen = set()
    facts = []
    for r in facts_all:
        key = (r[1], r[2], r[3])
        if key not in seen:
            seen.add(key)
            facts.append(r)

    # 相关 tag_relations
    relations_all = []
    for n in search_names[:5]:
        rows = c.db.conn.execute(
            """SELECT t1.name, tr.relation_type, t2.name, tr.weight
               FROM tag_relations tr
               JOIN tags t1 ON tr.source_tag_id = t1.id
               JOIN tags t2 ON tr.target_tag_id = t2.id
               WHERE t1.name = ? OR t2.name = ?
               LIMIT ?""",
            (n, n, limit),
        ).fetchall()
        relations_all.extend(rows)
    relations = list({(r[0],r[1],r[2]): r for r in relations_all}.values())

    # 关联记忆（人物按 QQ 查更准）
    if person:
        memories = c.db.conn.execute(
            "SELECT id, content, sender_name, timestamp FROM memories WHERE sender_id = ? ORDER BY timestamp DESC LIMIT ?",
            (person["qq_id"], limit),
        ).fetchall()
    else:
        memories = c.db.conn.execute(
            """SELECT m.id, m.content, m.sender_name, m.timestamp
               FROM memories m JOIN memory_tags mt ON m.id = mt.memory_id JOIN tags t ON mt.tag_id = t.id
               WHERE t.name = ? ORDER BY m.timestamp DESC LIMIT ?""",
            (name, limit),
        ).fetchall()

    return jsonify({
        "name": name,
        "person": person,
        "facts": [{"id": r[0], "subject": r[1], "predicate": r[2], "object": r[3], "confidence": r[4]} for r in facts[:limit]],
        "relations": [{"source": r[0], "type": r[1], "target": r[2], "weight": r[3]} for r in relations[:limit]],
        "memories": [{"id": r[0], "content": r[1], "sender": r[2] or "", "ts": r[3]} for r in memories],
    })


@kg_bp.route("/add-fact", methods=["POST"])
@require_auth
async def add_fact():
    """手动添加事实三元组到知识图谱。"""
    c = get_container()
    body = await request.get_json(silent=True) or {}
    subject = (body.get("subject") or "").strip()
    predicate = (body.get("predicate") or "").strip()
    obj = (body.get("object") or "").strip()
    if not subject or not predicate or not obj:
        return jsonify({"ok": False, "error": "subject/predicate/object required"})
    try:
        c.db.insert_fact(subject, predicate, obj, confidence=1.0)
        return jsonify({"ok": True})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)})


@kg_bp.route("/payment", methods=["POST"])
@require_auth
async def payment_webhook():
    """好感符 webhook：手机收款通知推送到这里，bot 确认并加好感。

    POST body: {"amount": 5.0, "note": "微信支付到账", "raw": "完整通知文本"}
    无需 auth（手机 Tasker/MacroDroid 直接调，用 secret token 验证）。
    """
    c = get_container()
    body = await request.get_json(silent=True) or {}
    amount = float(body.get("amount", 0))
    note = body.get("note", "")
    raw = body.get("raw", "")
    secret = body.get("secret", "")

    # 简单 token 验证（防止恶意调用）
    expected_secret = (c.plugin_config or {}).get("payment_secret", "wavemoney")
    if secret != expected_secret:
        return jsonify({"ok": False, "error": "invalid secret"}), 403

    if amount <= 0:
        return jsonify({"ok": False, "error": "amount must be > 0"})

    # 好感度映射
    if amount >= 100:
        bonus = 30
    elif amount >= 50:
        bonus = 15
    elif amount >= 10:
        bonus = 5
    elif amount >= 5:
        bonus = 3
    else:
        bonus = 1

    # 记录到 facts（留痕）
    try:
        c.db.insert_fact("好感符", f"收到{amount}元", f"好感+{bonus}", confidence=1.0)
    except Exception:
        pass

    # 返回结果（实际加好感需要知道是谁付的——由前端/群内认领机制处理）
    return jsonify({
        "ok": True,
        "amount": amount,
        "bonus": bonus,
        "message": f"收到 {amount} 元，好感 +{bonus}",
        "note": note,
    })


@kg_bp.route("/stats")
@require_auth
async def kg_stats():
    """图谱统计。"""
    c = get_container()
    conn = c.db.conn
    def _safe_count(table, cond=""):
        if not _table_exists(conn, table):
            return 0
        try:
            sql = f"SELECT COUNT(*) FROM {table}"
            if cond:
                sql += f" WHERE {cond}"
            return conn.execute(sql).fetchone()[0]
        except Exception:
            return 0
    return jsonify({
        "facts": _safe_count("facts"),
        "tag_relations": _safe_count("tag_relations"),
        "beliefs": _safe_count("beliefs"),
        "concerns": _safe_count("concerns"),
        "jargon": _safe_count("jargon", "is_jargon=1"),
        "persons": conn.execute("SELECT COUNT(DISTINCT sender_id) FROM memories WHERE sender_id!=''").fetchone()[0] if _table_exists(conn, "memories") else 0,
    })


@kg_bp.route("/config")
@require_auth
async def kg_config():
    """图谱可用的筛选选项(供前端配置面板)。"""
    c = get_container()
    rel_types = [r[0] for r in c.db.conn.execute(
        "SELECT DISTINCT relation_type FROM tag_relations WHERE relation_type IS NOT NULL ORDER BY relation_type"
    ).fetchall()]
    rel_types += ["fact"]
    node_types = ["person", "topic", "entity", "event", "emotion", "fact", "location", "time"]
    return jsonify({
        "relation_types": rel_types,
        "node_types": node_types,
        "defaults": {"max_nodes": 150, "min_weight": 0.5, "days": 0},
    })


@kg_bp.route("/full")
@require_auth
async def kg_full():
    """全量知识图谱数据（按图层返回）。

    参数 layers: 逗号分隔(facts,beliefs,concerns,jargon,affinity,communities)
    默认只返回 facts 图层。前端可勾选多图层叠加。
    """
    c = get_container()
    layers_raw = request.args.get("layers", "facts")
    layers = set(layers_raw.split(",")) - {""}

    now = time.time()
    cache_key = f"full:{layers_raw}"
    if _overview_cache.get(cache_key) and (now - _overview_cache.get(f"{cache_key}_ts", 0)) < 300:
        return jsonify(_overview_cache[cache_key])

    # 消歧映射
    name_to_qq: dict[str, str] = {}
    qq_to_main: dict[str, str] = {}
    try:
        rows = c.db.conn.execute(
            "SELECT sender_name, sender_id, COUNT(*) FROM memories WHERE sender_id!='' AND sender_name!='' GROUP BY sender_name, sender_id ORDER BY 3 DESC"
        ).fetchall()
        for sname, sid, _ in rows:
            key = sname.strip()[:25]
            name_to_qq[key] = sid
            if sid not in qq_to_main:
                qq_to_main[sid] = key
    except Exception:
        pass

    def resolve(n: str) -> str:
        qq = name_to_qq.get(n)
        return qq_to_main.get(qq, n) if qq else n

    edges = []
    seen = set()

    # ─── 图层: facts (facts + tag_relations) ───
    if "facts" in layers:
        for r in c.db.conn.execute(
            "SELECT t1.name, tr.relation_type, t2.name, tr.weight, t1.tag_type, t2.tag_type, tr.created_at FROM tag_relations tr JOIN tags t1 ON tr.source_tag_id=t1.id JOIN tags t2 ON tr.target_tag_id=t2.id"
        ).fetchall():
            s, t = resolve((r[0] or "").strip()[:25]), resolve((r[2] or "").strip()[:25])
            if not s or not t or s == t:
                continue
            key = (s, t, r[1])
            if key not in seen:
                seen.add(key)
                edges.append({"s": s, "t": t, "l": r[1] or "relates", "w": round(r[3] or 1, 2), "st": r[4] or "topic", "tt": r[5] or "topic", "ts": r[6] or 0, "layer": "facts"})

        for r in c.db.conn.execute("SELECT subject, predicate, object, confidence, created_at FROM facts"):
            if not r[0] or not r[2]:
                continue
            s, t = resolve(r[0].strip()[:25]), resolve(r[2].strip()[:25])
            if not s or not t or s == t:
                continue
            label = (r[1] or "relates").strip()[:15]
            key = (s, t, label)
            if key not in seen:
                seen.add(key)
                edges.append({"s": s, "t": t, "l": label, "w": round(r[3] or 1, 2), "st": "entity", "tt": "entity", "ts": r[4] or 0, "layer": "facts"})

    # ─── 图层: beliefs ───
    if "beliefs" in layers:
        for r in c.db.conn.execute("SELECT content, type, strength, bot_id FROM beliefs WHERE status='active'"):
            bot = qq_to_main.get("bot", "bot") if r[3] == "bot" else resolve(r[3] or "bot")
            edges.append({"s": bot, "t": r[0][:25], "l": "believes", "w": round(r[2] or 0.5, 2), "st": "person", "tt": "belief", "ts": 0, "layer": "beliefs"})

    # ─── 图层: concerns ───
    if "concerns" in layers:
        for r in c.db.conn.execute("SELECT topic, intensity, bot_id FROM concerns"):
            bot = resolve(r[2] or "bot")
            edges.append({"s": bot, "t": (r[0] or "")[:25], "l": "关注", "w": round(r[1] or 0.5, 2), "st": "person", "tt": "concern", "ts": 0, "layer": "concerns"})

    # ─── 图层: jargon ───
    if "jargon" in layers:
        for r in c.db.conn.execute("SELECT word, meaning, frequency, group_id FROM jargon WHERE is_jargon=1"):
            edges.append({"s": f"群{r[3]}" if r[3] else "全局", "t": r[0], "l": "黑话", "w": min(r[2] or 1, 10), "st": "entity", "tt": "jargon", "ts": 0, "layer": "jargon"})

    # ─── 图层: affinity (好感度) ───
    if "affinity" in layers:
        try:
            for r in c.db.conn.execute("SELECT user_id, nickname, affection, bot_id FROM user_profiles WHERE affection != 0 LIMIT 200"):
                person = resolve(r[1] or r[0])
                bot = resolve(r[3] or "bot")
                label = f"好感{r[2]}"
                edges.append({"s": bot, "t": person, "l": label, "w": abs(r[2]) / 20.0, "st": "person", "tt": "person", "ts": 0, "layer": "affinity"})
        except Exception:
            pass

    # ─── 图层: communities ───
    if "communities" in layers and c.cooccurrence:
        try:
            communities = c.cooccurrence.detect_communities(min_community_size=5)
            for cid, members in list(communities.items())[:20]:
                hub = members[0] if members else f"社区{cid}"
                hub_name = ""
                try:
                    row = c.db.conn.execute("SELECT name FROM tags WHERE id=?", (hub,)).fetchone()
                    hub_name = row[0] if row else f"tag#{hub}"
                except Exception:
                    hub_name = f"tag#{hub}"
                for mid in members[1:5]:
                    try:
                        row = c.db.conn.execute("SELECT name FROM tags WHERE id=?", (mid,)).fetchone()
                        member_name = row[0] if row else f"tag#{mid}"
                    except Exception:
                        member_name = f"tag#{mid}"
                    edges.append({"s": hub_name, "t": member_name, "l": "同社区", "w": 1.0, "st": "topic", "tt": "topic", "ts": 0, "layer": "communities"})
        except Exception:
            pass

    # 标记人物
    for e in edges:
        if e["s"] in name_to_qq:
            e["st"] = "person"
        if e["t"] in name_to_qq:
            e["tt"] = "person"

    data = {"edges": edges, "total": len(edges), "layers": list(layers)}
    _overview_cache[cache_key] = data
    _overview_cache[f"{cache_key}_ts"] = now
    return jsonify(data)


@kg_bp.route("/entity/<entity_name>/timeline")
@require_auth
async def entity_timeline(entity_name: str):
    """实体时间线：该实体相关的 facts + memories 按时间排列。"""
    c = get_container()
    from urllib.parse import unquote
    name = unquote(entity_name).strip()
    limit = int(request.args.get("limit", 30))

    # 查该实体所有别名（人物消歧）
    names = [name]
    qq_row = c.db.conn.execute(
        "SELECT sender_id FROM memories WHERE sender_name = ? AND sender_id != '' LIMIT 1", (name,)
    ).fetchone()
    if qq_row:
        aliases = c.db.conn.execute(
            "SELECT DISTINCT sender_name FROM memories WHERE sender_id = ? AND sender_name != ''", (qq_row[0],)
        ).fetchall()
        names = list({name} | {a[0] for a in aliases})

    # Facts 时间线
    events = []
    for n in names[:5]:
        rows = c.db.conn.execute(
            "SELECT subject, predicate, object, created_at, source_memory_id FROM facts WHERE (subject=? OR object=?) AND created_at IS NOT NULL ORDER BY created_at DESC LIMIT ?",
            (n, n, limit),
        ).fetchall()
        for r in rows:
            events.append({"type": "fact", "ts": r[3], "subject": r[0], "predicate": r[1], "object": r[2], "source_id": r[4]})

    # 关键记忆（按时间）
    if qq_row:
        mem_rows = c.db.conn.execute(
            "SELECT id, content, sender_name, timestamp FROM memories WHERE sender_id = ? ORDER BY timestamp DESC LIMIT ?",
            (qq_row[0], limit),
        ).fetchall()
    else:
        mem_rows = c.db.conn.execute(
            "SELECT m.id, m.content, m.sender_name, m.timestamp FROM memories m JOIN memory_tags mt ON m.id=mt.memory_id JOIN tags t ON mt.tag_id=t.id WHERE t.name=? ORDER BY m.timestamp DESC LIMIT ?",
            (name, limit),
        ).fetchall()
    for r in mem_rows:
        events.append({"type": "memory", "ts": r[3], "id": r[0], "content": r[1], "sender": r[2] or ""})

    # 按时间排序
    events.sort(key=lambda e: e.get("ts") or 0, reverse=True)
    return jsonify({"name": name, "events": events[:limit]})


@kg_bp.route("/path", methods=["POST"])
@require_auth
async def kg_path():
    """多跳路径：两个实体间的最短关系链（BFS on tag_relations + facts）。

    每跳返回关系类型 + 两端实体名，用户能看到 A→关系→B→关系→C 的完整语义链。
    """
    c = get_container()
    body = await request.get_json(silent=True) or {}
    from_name = (body.get("from") or "").strip()
    to_name = (body.get("to") or "").strip()
    max_depth = int(body.get("max_depth", 5))

    if not from_name or not to_name:
        return jsonify({"path": [], "edges": [], "error": "from and to required"})

    # 构建邻接表（name→name，带关系标签）from tag_relations + facts
    adj: dict[str, list[tuple[str, str]]] = {}  # name → [(neighbor, label)]

    # tag_relations
    rel_rows = c.db.conn.execute(
        """SELECT t1.name, tr.relation_type, t2.name FROM tag_relations tr
           JOIN tags t1 ON tr.source_tag_id=t1.id JOIN tags t2 ON tr.target_tag_id=t2.id"""
    ).fetchall()
    for src, rtype, tgt in rel_rows:
        adj.setdefault(src, []).append((tgt, rtype or "relates"))
        adj.setdefault(tgt, []).append((src, rtype or "relates"))

    # facts
    fact_rows = c.db.conn.execute("SELECT subject, predicate, object FROM facts").fetchall()
    for subj, pred, obj in fact_rows:
        if subj and obj:
            adj.setdefault(subj, []).append((obj, pred or "relates"))
            adj.setdefault(obj, []).append((subj, pred or "relates"))

    # BFS
    from collections import deque
    visited: dict[str, tuple] = {from_name: (None, None)}  # name → (parent, edge_label)
    queue = deque([(from_name, 0)])
    found = False

    while queue:
        current, depth = queue.popleft()
        if depth >= max_depth:
            continue
        for neighbor, label in adj.get(current, []):
            if neighbor not in visited:
                visited[neighbor] = (current, label)
                if neighbor == to_name:
                    found = True
                    break
                queue.append((neighbor, depth + 1))
        if found:
            break

    if not found:
        return jsonify({"path": [], "edges": [], "nodes": []})

    # 回溯路径
    path_names = []
    path_edges = []
    node = to_name
    while node is not None:
        path_names.append(node)
        parent, label = visited[node]
        if parent is not None:
            path_edges.append({"source": parent, "target": node, "label": label or "relates"})
        node = parent
    path_names.reverse()
    path_edges.reverse()

    # 构建节点（for graph rendering）
    nodes = [{"id": i+1, "name": n, "type": "entity", "degree": 1} for i, n in enumerate(path_names)]

    return jsonify({"path": path_names, "edges": path_edges, "nodes": nodes})


@kg_bp.route("/facts/<int:fact_id>", methods=["DELETE"])
@require_auth
async def delete_fact(fact_id: int):
    """删除事实。"""
    c = get_container()
    if not _table_exists(c.db.conn, "facts"):
        return jsonify({"ok": False, "error": "facts table not found"})
    c.db.conn.execute("DELETE FROM facts WHERE rowid = ?", (fact_id,))
    c.db.conn.commit()
    return jsonify({"ok": True, "deleted": fact_id})


@kg_bp.route("/facts/<int:fact_id>", methods=["PUT"])
@require_auth
async def update_fact(fact_id: int):
    """修改事实（subject/predicate/object/confidence）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "facts"):
        return jsonify({"ok": False, "error": "facts table not found"})
    body = await request.get_json(silent=True) or {}
    sets = []
    params = []
    for field in ("subject", "predicate", "object"):
        if field in body and body[field] is not None:
            sets.append(f"{field} = ?")
            params.append(str(body[field]).strip())
    if "confidence" in body and body["confidence"] is not None:
        try:
            sets.append("confidence = ?")
            params.append(float(body["confidence"]))
        except (ValueError, TypeError):
            pass
    if not sets:
        return jsonify({"ok": False, "error": "No valid fields to update"}), 400
    params.append(fact_id)
    c.db.conn.execute(f"UPDATE facts SET {', '.join(sets)} WHERE rowid = ?", params)
    c.db.conn.commit()
    return jsonify({"ok": True, "fact_id": fact_id})


@kg_bp.route("/tag-relations/<int:rel_id>", methods=["DELETE"])
@require_auth
async def delete_tag_relation(rel_id: int):
    """删除 tag 关系。"""
    c = get_container()
    if not _table_exists(c.db.conn, "tag_relations"):
        return jsonify({"ok": False, "error": "tag_relations table not found"})
    c.db.conn.execute("DELETE FROM tag_relations WHERE id = ?", (rel_id,))
    c.db.conn.commit()
    return jsonify({"ok": True, "deleted": rel_id})
