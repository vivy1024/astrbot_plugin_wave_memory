"""回忆回放：把一段时间内的真实注入 trace 映射到标签图上。

每次注入（羽书准备回复一句话）真正注入了哪些记忆记录在 trace 的 ``details.items`` 里；
这些记忆关联的标签就是那一刻"被想起"的节点。标签图按被想起的次数优先选点，
所以回放里点亮的节点都画在图上；没有标签的记忆只计入统计。

直播舞台页用 ``all_sessions``：星空仍是请求作用域（主群）的标签图，事件取该 Bot 全部会话
（弹幕、各个群），其它会话的标签按名字对到星空上的同名标签。``since`` + ``include_graph=False``
用于每隔几秒增量拉新事件，不重建图。
"""

from __future__ import annotations

import time
from collections import Counter
from typing import Any, Iterable

from .graph_projection import _clip, _scope_params, build_tag_graph_projection

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - plugin root may be imported directly
    from domain.scope import RuntimeScope

# 事件卡片上最多展示几条被想起的记忆
EVENT_MEMORY_PREVIEWS = 6
# 每个事件最多返回多少个标签名（前端按名字对到星空节点）
EVENT_TAG_NAMES = 40


def _normalize_name(name: Any) -> str:
    return str(name or "").strip().lower()


def _memory_tags(conn: Any, scope: RuntimeScope, memory_ids: list[int], *, all_sessions: bool) -> dict[int, list[tuple[int, str]]]:
    """记忆 → [(标签 id, 标签名)]（按序位）。默认只查当前作用域；all_sessions 时查该 Bot 全部会话。"""
    bot_id, session_id, visibility = _scope_params(scope)
    result: dict[int, list[tuple[int, str]]] = {}
    for start in range(0, len(memory_ids), 500):
        chunk = memory_ids[start:start + 500]
        scope_filter = "" if all_sessions else " AND mt.session_id=? AND mt.visibility=?"
        params: list[Any] = [bot_id] + ([] if all_sessions else [session_id, visibility]) + list(chunk)
        rows = conn.execute(
            f"""SELECT mt.memory_id, mt.tag_id, t.name FROM scoped_memory_tags mt
                  JOIN scoped_tags t ON t.id = mt.tag_id
                 WHERE mt.bot_id=?{scope_filter} AND mt.memory_id IN ({','.join('?' * len(chunk))})
                 ORDER BY mt.memory_id, mt.position, mt.tag_id""",
            params,
        ).fetchall()
        for memory_id, tag_id, name in rows:
            result.setdefault(int(memory_id), []).append((int(tag_id), str(name or "")))
    return result


def _scope_tag_ids_by_name(conn: Any, scope: RuntimeScope, names: Iterable[str]) -> dict[str, int]:
    """作用域内同名标签的 id（名字不区分大小写）。"""
    wanted = sorted({_normalize_name(name) for name in names if _normalize_name(name)})
    if not wanted:
        return {}
    bot_id, session_id, visibility = _scope_params(scope)
    result: dict[str, int] = {}
    for start in range(0, len(wanted), 500):
        chunk = wanted[start:start + 500]
        rows = conn.execute(
            f"""SELECT id, name FROM scoped_tags
                 WHERE bot_id=? AND session_id=? AND visibility=? AND lower(trim(name)) IN ({','.join('?' * len(chunk))})
                 ORDER BY id""",
            [bot_id, session_id, visibility, *chunk],
        ).fetchall()
        for tag_id, name in rows:
            result.setdefault(_normalize_name(name), int(tag_id))
    return result


def build_recall_replay(
    *, conn: Any, trace_store: Any, scope: RuntimeScope, hours: float = 24.0,
    limit: int = 300, max_nodes: int = 400, now: float | None = None,
    all_sessions: bool = False, since: float | None = None, include_graph: bool = True,
) -> dict[str, Any]:
    _scope_params(scope)
    generated_at = float(time.time() if now is None else now)
    from_ts = generated_at - max(1.0, float(hours)) * 3600.0
    if since is not None:
        from_ts = max(from_ts, float(since))
    events = trace_store.recall_events(
        from_ts=from_ts, to_ts=generated_at, bot_id=scope.bot_id,
        session_id=None if all_sessions or scope.session is None else scope.session.id, limit=limit,
    ) if trace_store is not None else []
    if since is not None:
        events = [event for event in events if event["timestamp"] > float(since)]

    memory_ids = sorted({int(memory["id"]) for event in events for memory in event["memories"]})
    tags_by_memory = _memory_tags(conn, scope, memory_ids, all_sessions=all_sessions) if conn is not None and memory_ids else {}

    # 其它会话的标签按名字对到作用域内的同名标签；本作用域的标签直接用 id
    own_ids: set[int] = set()
    if all_sessions and conn is not None and tags_by_memory:
        names = {name for tags in tags_by_memory.values() for _tag_id, name in tags}
        by_name = _scope_tag_ids_by_name(conn, scope, names)
        own_ids = set(by_name.values())
    else:
        by_name = {}

    def scope_ids(tags: list[tuple[int, str]]) -> list[int]:
        if not all_sessions:
            return [tag_id for tag_id, _name in tags]
        mapped = [by_name.get(_normalize_name(name)) for _tag_id, name in tags]
        return [tag_id for tag_id in mapped if tag_id is not None]

    # 权重 = 在多少次注入里被想起（同一次注入里多条记忆指向同一标签只算一次）
    weights: Counter[int] = Counter()
    for event in events:
        weights.update({tag_id for memory in event["memories"] for tag_id in scope_ids(tags_by_memory.get(int(memory["id"]), []))})

    graph: dict[str, Any] | None = None
    on_graph: set[int] | None = None
    if include_graph:
        graph = build_tag_graph_projection(
            conn=conn, scope=scope, max_nodes=max_nodes, rank_by="recent",
            recent_window_hours=max(1.0, float(hours)), focus_tag_weights=dict(weights), now=generated_at,
        )
        on_graph = {int(node["locator"]) for node in graph.get("nodes", ())}

    replay_events: list[dict[str, Any]] = []
    memory_hits = tagged_hits = 0
    for event in events:
        memories = event["memories"]
        memory_hits += len(memories)
        tag_ids: list[int] = []
        tag_names: list[str] = []
        cards: list[dict[str, Any]] = []
        for memory in memories:
            memory_tags = tags_by_memory.get(int(memory["id"]), [])
            if memory_tags:
                tagged_hits += 1
            tag_names.extend(name for _tag_id, name in memory_tags if name)
            ids = scope_ids(memory_tags)
            visible = ids if on_graph is None else [tag_id for tag_id in ids if tag_id in on_graph]
            tag_ids.extend(visible)
            if len(cards) < EVENT_MEMORY_PREVIEWS:
                cards.append({
                    "id": int(memory["id"]), "channel": memory.get("channel"),
                    "score": memory.get("score"), "preview": _clip(memory.get("preview"), 120),
                    "tags": [f"tag:{tag_id}" for tag_id in visible],
                })
        replay_events.append({
            "trace_id": event["trace_id"], "timestamp": event["timestamp"],
            "sender_name": event.get("sender_name") or "",
            "message_preview": _clip(event.get("message_preview"), 160),
            "session_id": event.get("session_id"),
            "source": event.get("source") or "astrbot",
            "memory_count": len(memories),
            "tags": [f"tag:{tag_id}" for tag_id in dict.fromkeys(tag_ids)],
            "tag_names": list(dict.fromkeys(tag_names))[:EVENT_TAG_NAMES],
            "memories": cards,
        })

    stats = {
        "event_count": len(replay_events),
        "memory_hits": memory_hits,
        "tagged_memory_hits": tagged_hits,
        "recalled_tags": len(weights),
        "recalled_tags_on_graph": len(on_graph & set(weights)) if on_graph is not None else None,
        "events_with_tags": sum(1 for event in replay_events if event["tags"]),
    }
    if all_sessions:
        stats["name_matched_tags"] = len(own_ids)
    return {
        "graph": graph,
        "events": replay_events,
        "window": {"from": from_ts, "to": generated_at, "hours": float(hours)},
        "stats": stats,
        "all_sessions": bool(all_sessions),
        "scope": graph.get("scope") if graph else None,
        "read_only": True,
        "generated_at": generated_at,
    }


__all__ = ["build_recall_replay"]
