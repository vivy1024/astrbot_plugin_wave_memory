"""Soul Blueprint — 关切/情绪/时间锚点 CRUD (US-1.3)"""

from __future__ import annotations

import time

from quart import Blueprint, jsonify, request

from ..container import get_container
from ..middleware.auth import require_auth

soul_bp = Blueprint("soul", __name__, url_prefix="/api")


def _table_exists(conn, table: str) -> bool:
    """检查表是否存在。"""
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (table,)
    ).fetchone()
    return row is not None


def _safe_int(val, default):
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _safe_float(val, default):
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


# ═══════════════════════════════════════════
#  Concerns — 关切管理 CRUD
# ═══════════════════════════════════════════

@soul_bp.route("/concerns", methods=["GET"])
@require_auth
async def list_concerns():
    """查看当前关切列表（按强度排序）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "concerns"):
        return jsonify({"items": []})
    bot_id = request.args.get("bot_id")
    sql = "SELECT id, topic, intensity, bot_id, origin_memory_id, created_at, last_triggered FROM concerns WHERE 1=1"
    params = []
    if bot_id:
        sql += " AND bot_id = ?"
        params.append(bot_id)
    sql += " ORDER BY intensity DESC, created_at DESC LIMIT 50"
    rows = c.db.conn.execute(sql, params).fetchall()
    items = [
        {"id": r[0], "topic": r[1], "intensity": r[2], "bot_id": r[3],
         "origin_memory_id": r[4], "created_at": r[5], "last_triggered": r[6]}
        for r in rows
    ]
    return jsonify({"items": items})


@soul_bp.route("/concerns", methods=["POST"])
@require_auth
async def create_concern():
    """创建 concern。"""
    c = get_container()
    if not _table_exists(c.db.conn, "concerns"):
        return jsonify({"ok": False, "error": "concerns table not found"}), 500
    body = await request.get_json(silent=True) or {}
    topic = (body.get("topic") or "").strip()
    if not topic:
        return jsonify({"ok": False, "error": "topic required"}), 400
    intensity = _safe_float(body.get("intensity"), 0.7)
    intensity = max(0.0, min(1.0, intensity))
    bot_id = body.get("bot_id", "")
    origin_memory_id = _safe_int(body.get("origin_memory_id"), 0)
    now = int(time.time())
    cur = c.db.conn.execute(
        "INSERT INTO concerns (topic, intensity, bot_id, origin_memory_id, created_at, last_triggered) VALUES (?, ?, ?, ?, ?, ?)",
        (topic, intensity, bot_id, origin_memory_id, now, now),
    )
    c.db.conn.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@soul_bp.route("/concerns/<int:concern_id>", methods=["PUT"])
@require_auth
async def edit_concern(concern_id: int):
    """编辑 concern topic/intensity。"""
    c = get_container()
    if not _table_exists(c.db.conn, "concerns"):
        return jsonify({"ok": False, "error": "concerns table not found"}), 500
    body = await request.get_json(silent=True) or {}
    sets = []
    params = []
    if "topic" in body and body["topic"]:
        sets.append("topic = ?")
        params.append(str(body["topic"]).strip())
    if "intensity" in body and body["intensity"] is not None:
        sets.append("intensity = ?")
        params.append(max(0.0, min(1.0, _safe_float(body["intensity"], 0.7))))
    if not sets:
        return jsonify({"ok": False, "error": "No valid fields to update"}), 400
    params.append(concern_id)
    c.db.conn.execute(f"UPDATE concerns SET {', '.join(sets)} WHERE id = ?", params)
    c.db.conn.commit()
    return jsonify({"ok": True, "id": concern_id})


@soul_bp.route("/concerns/<int:concern_id>", methods=["DELETE"])
@require_auth
async def delete_concern(concern_id: int):
    """删除 concern。"""
    c = get_container()
    if not _table_exists(c.db.conn, "concerns"):
        return jsonify({"ok": False, "error": "concerns table not found"}), 500
    c.db.conn.execute("DELETE FROM concerns WHERE id = ?", (concern_id,))
    c.db.conn.commit()
    return jsonify({"ok": True, "deleted": concern_id})


@soul_bp.route("/concerns/<int:concern_id>/approve", methods=["POST"])
@require_auth
async def approve_concern(concern_id: int):
    """标记为已审核（intensity 提升 + last_triggered 更新）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "concerns"):
        return jsonify({"ok": False, "error": "concerns table not found"}), 500
    now = int(time.time())
    c.db.conn.execute(
        "UPDATE concerns SET last_triggered = ?, intensity = MIN(1.0, intensity + 0.1) WHERE id = ?",
        (now, concern_id),
    )
    c.db.conn.commit()
    return jsonify({"ok": True, "id": concern_id})


@soul_bp.route("/concerns/<int:concern_id>/reject", methods=["POST"])
@require_auth
async def reject_concern(concern_id: int):
    """降级 concern（intensity 降低）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "concerns"):
        return jsonify({"ok": False, "error": "concerns table not found"}), 500
    c.db.conn.execute(
        "UPDATE concerns SET intensity = MAX(0.0, intensity - 0.3) WHERE id = ?",
        (concern_id,),
    )
    c.db.conn.commit()
    return jsonify({"ok": True, "id": concern_id})


# ═══════════════════════════════════════════
#  Time Anchors — 时间锚点 CRUD
# ═══════════════════════════════════════════

@soul_bp.route("/time-anchors", methods=["GET"])
@require_auth
async def time_anchors():
    """时间锚点列表（情感权重高的关键事件）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "time_anchors"):
        return jsonify({"items": []})
    bot_id = request.args.get("bot_id")
    limit = max(1, min(500, _safe_int(request.args.get("limit", 50), 50)))
    sql = "SELECT id, event_summary, timestamp, emotional_weight, bot_id FROM time_anchors WHERE 1=1"
    params = []
    if bot_id:
        sql += " AND bot_id = ?"
        params.append(bot_id)
    sql += " ORDER BY timestamp DESC LIMIT ?"
    params.append(limit)
    rows = c.db.conn.execute(sql, params).fetchall()
    items = [
        {"id": r[0], "event_summary": r[1], "timestamp": r[2],
         "emotional_weight": r[3], "bot_id": r[4]}
        for r in rows
    ]
    return jsonify({"items": items})


@soul_bp.route("/time-anchors", methods=["POST"])
@require_auth
async def create_time_anchor():
    """创建时间锚点。"""
    c = get_container()
    if not _table_exists(c.db.conn, "time_anchors"):
        return jsonify({"ok": False, "error": "time_anchors table not found"}), 500
    body = await request.get_json(silent=True) or {}
    event_summary = (body.get("event_summary") or "").strip()
    if not event_summary:
        return jsonify({"ok": False, "error": "event_summary required"}), 400
    timestamp = _safe_float(body.get("timestamp"), time.time())
    emotional_weight = _safe_float(body.get("emotional_weight"), 0.5)
    emotional_weight = max(0.0, min(1.0, emotional_weight))
    bot_id = body.get("bot_id", "")
    cur = c.db.conn.execute(
        "INSERT INTO time_anchors (event_summary, timestamp, emotional_weight, bot_id) VALUES (?, ?, ?, ?)",
        (event_summary, timestamp, emotional_weight, bot_id),
    )
    c.db.conn.commit()
    return jsonify({"ok": True, "id": cur.lastrowid})


@soul_bp.route("/time-anchors/<int:anchor_id>", methods=["PUT"])
@require_auth
async def edit_time_anchor(anchor_id: int):
    """编辑时间锚点。"""
    c = get_container()
    if not _table_exists(c.db.conn, "time_anchors"):
        return jsonify({"ok": False, "error": "time_anchors table not found"}), 500
    body = await request.get_json(silent=True) or {}
    sets = []
    params = []
    if "event_summary" in body and body["event_summary"]:
        sets.append("event_summary = ?")
        params.append(str(body["event_summary"]).strip())
    if "emotional_weight" in body and body["emotional_weight"] is not None:
        sets.append("emotional_weight = ?")
        params.append(max(0.0, min(1.0, _safe_float(body["emotional_weight"], 0.5))))
    if "timestamp" in body and body["timestamp"] is not None:
        sets.append("timestamp = ?")
        params.append(_safe_float(body["timestamp"], time.time()))
    if not sets:
        return jsonify({"ok": False, "error": "No valid fields to update"}), 400
    params.append(anchor_id)
    c.db.conn.execute(f"UPDATE time_anchors SET {', '.join(sets)} WHERE id = ?", params)
    c.db.conn.commit()
    return jsonify({"ok": True, "id": anchor_id})


@soul_bp.route("/time-anchors/<int:anchor_id>", methods=["DELETE"])
@require_auth
async def delete_time_anchor(anchor_id: int):
    """删除时间锚点。"""
    c = get_container()
    if not _table_exists(c.db.conn, "time_anchors"):
        return jsonify({"ok": False, "error": "time_anchors table not found"}), 500
    c.db.conn.execute("DELETE FROM time_anchors WHERE id = ?", (anchor_id,))
    c.db.conn.commit()
    return jsonify({"ok": True, "deleted": anchor_id})


@soul_bp.route("/time-anchors/<int:anchor_id>/approve", methods=["POST"])
@require_auth
async def approve_time_anchor(anchor_id: int):
    """标记为已审核（emotional_weight 提升）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "time_anchors"):
        return jsonify({"ok": False, "error": "time_anchors table not found"}), 500
    c.db.conn.execute(
        "UPDATE time_anchors SET emotional_weight = MIN(1.0, emotional_weight + 0.1) WHERE id = ?",
        (anchor_id,),
    )
    c.db.conn.commit()
    return jsonify({"ok": True, "id": anchor_id})


@soul_bp.route("/time-anchors/<int:anchor_id>/reject", methods=["POST"])
@require_auth
async def reject_time_anchor(anchor_id: int):
    """降级时间锚点（emotional_weight 降低）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "time_anchors"):
        return jsonify({"ok": False, "error": "time_anchors table not found"}), 500
    c.db.conn.execute(
        "UPDATE time_anchors SET emotional_weight = MAX(0.0, emotional_weight - 0.3) WHERE id = ?",
        (anchor_id,),
    )
    c.db.conn.commit()
    return jsonify({"ok": True, "id": anchor_id})


# ═══════════════════════════════════════════
#  Mood — 情绪轨迹 + 编辑/删除
# ═══════════════════════════════════════════

@soul_bp.route("/mood/trajectory", methods=["GET"])
@require_auth
async def mood_trajectory():
    """情绪轨迹（折线图数据）。"""
    c = get_container()
    if not _table_exists(c.db.conn, "bot_mood"):
        return jsonify({"items": []})
    group_id = request.args.get("group_id")
    limit = max(1, min(500, _safe_int(request.args.get("limit", 100), 100)))

    sql = "SELECT id, group_id, mood_type, intensity, description, start_time, end_time, is_active FROM bot_mood WHERE typeof(start_time) IN ('real', 'integer')"
    params = []
    if group_id:
        sql += " AND group_id = ?"
        params.append(group_id)
    sql += " ORDER BY start_time DESC LIMIT ?"
    params.append(limit)

    rows = c.db.conn.execute(sql, params).fetchall()
    items = [
        {"id": r[0], "group_id": r[1], "type": r[2], "intensity": r[3], "desc": r[4],
         "ts": r[5], "end_time": r[6], "is_active": bool(r[7])}
        for r in rows
    ]
    # 时间正序用于前端折线图
    items.reverse()
    return jsonify({"items": items})


@soul_bp.route("/mood/<int:mood_id>", methods=["PUT"])
@require_auth
async def edit_mood(mood_id: int):
    """编辑情绪描述。"""
    c = get_container()
    if not _table_exists(c.db.conn, "bot_mood"):
        return jsonify({"ok": False, "error": "bot_mood table not found"}), 500
    body = await request.get_json(silent=True) or {}
    sets = []
    params = []
    if "description" in body and body["description"] is not None:
        sets.append("description = ?")
        params.append(str(body["description"]))
    if "intensity" in body and body["intensity"] is not None:
        sets.append("intensity = ?")
        params.append(max(0.0, min(1.0, _safe_float(body["intensity"], 0.5))))
    if "mood_type" in body and body["mood_type"]:
        sets.append("mood_type = ?")
        params.append(str(body["mood_type"]))
    if not sets:
        return jsonify({"ok": False, "error": "No valid fields to update"}), 400
    params.append(mood_id)
    c.db.conn.execute(f"UPDATE bot_mood SET {', '.join(sets)} WHERE id = ?", params)
    c.db.conn.commit()
    return jsonify({"ok": True, "id": mood_id})


@soul_bp.route("/mood/<int:mood_id>", methods=["DELETE"])
@require_auth
async def delete_mood(mood_id: int):
    """删除情绪记录。"""
    c = get_container()
    if not _table_exists(c.db.conn, "bot_mood"):
        return jsonify({"ok": False, "error": "bot_mood table not found"}), 500
    c.db.conn.execute("DELETE FROM bot_mood WHERE id = ?", (mood_id,))
    c.db.conn.commit()
    return jsonify({"ok": True, "deleted": mood_id})
