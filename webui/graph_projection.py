"""RuntimeScope 严格隔离的 3D 神经云图多图层只读投影。"""

from __future__ import annotations

import json
import math
import time
from collections import defaultdict, deque
from dataclasses import asdict, is_dataclass
from typing import Any, Iterable

import numpy as np

try:
    from ..domain.scope import RuntimeScope, ScopeCodec
    from ..engine.db.scoped_tag_projection import effective_tag_rows
except ImportError:  # pragma: no cover - plugin root may be imported directly
    from domain.scope import RuntimeScope, ScopeCodec
    from engine.db.scoped_tag_projection import effective_tag_rows


SUPPORTED_LAYERS = (
    "facts", "memories", "beliefs", "jargon", "concerns", "mood",
    "timeline", "affinity", "few_shot", "book_lore", "communities",
)


def _columns(conn: Any, table: str) -> set[str]:
    if conn is None:
        return set()
    try:
        return {str(row[1]) for row in conn.execute(f'PRAGMA table_info("{table}")').fetchall()}
    except Exception:
        return set()


def _rows(conn: Any, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
    cursor = conn.execute(sql, tuple(params))
    names = [str(item[0]) for item in cursor.description or ()]
    return [dict(zip(names, tuple(row))) for row in cursor.fetchall()]


def _json(value: Any, fallback: Any) -> Any:
    if isinstance(value, (dict, list)):
        return value
    try:
        return json.loads(value) if value not in (None, "") else fallback
    except (TypeError, ValueError, json.JSONDecodeError):
        return fallback


def _safe(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if hasattr(value, "to_dict"):
        return _safe(value.to_dict())
    if is_dataclass(value):
        return _safe(asdict(value))
    if isinstance(value, dict):
        return {str(key): _safe(item) for key, item in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe(item) for item in value]
    return str(value)


def _clip(value: Any, limit: int = 500) -> str:
    text = str(value or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _scope_params(scope: RuntimeScope) -> tuple[str, str, str]:
    if not isinstance(scope, RuntimeScope) or scope.session is None or scope.visibility != "group":
        raise ValueError("scope_required")
    return scope.bot_id, scope.session.id, scope.visibility


class GraphProjection:
    def __init__(self, scope: RuntimeScope, layers: Iterable[str]):
        self.scope = scope
        self.layers = tuple(dict.fromkeys(str(layer) for layer in layers))
        self.nodes: dict[str, dict[str, Any]] = {}
        self.edges: dict[str, dict[str, Any]] = {}
        self.counts = {layer: {"nodes": 0, "edges": 0} for layer in self.layers}
        self.warnings: list[dict[str, str]] = []

    def warn(self, layer: str, reason: str) -> None:
        item = {"layer": layer, "reason": reason}
        if item not in self.warnings:
            self.warnings.append(item)

    def node(self, node_id: str, name: str, node_type: str, layer: str, **metadata: Any) -> str:
        node_id = str(node_id)
        existing = self.nodes.get(node_id)
        payload = {
            "id": node_id, "name": _clip(name, 160) or node_id, "type": node_type,
            "layer": layer, "read_only": True, **_safe(metadata),
        }
        if existing is None:
            self.nodes[node_id] = payload
            if layer in self.counts:
                self.counts[layer]["nodes"] += 1
        else:
            existing.update({key: value for key, value in payload.items() if value not in (None, "", [], {})})
        return node_id

    def edge(
        self, edge_id: str, source: str, target: str, label: str, layer: str, kind: str,
        *, weight: float = 1.0, confidence: float | None = None, ts: float | None = None,
        source_type: str = "entity", target_type: str = "entity", **metadata: Any,
    ) -> None:
        source, target = str(source), str(target)
        if not source or not target or source == target or source not in self.nodes or target not in self.nodes:
            return
        edge_id = str(edge_id)
        if edge_id in self.edges:
            return
        weight_value = float(weight or 0.0)
        confidence_value = weight_value if confidence is None else float(confidence or 0.0)
        self.edges[edge_id] = {
            "id": edge_id, "s": source, "t": target, "l": str(label or "relates"),
            "w": weight_value, "weight": weight_value, "confidence": confidence_value,
            "ts": float(ts or 0.0), "st": source_type, "tt": target_type,
            "layer": layer, "kind": kind, "editable": False, "read_only": True,
            **_safe(metadata),
        }
        if layer in self.counts:
            self.counts[layer]["edges"] += 1

    def payload(self) -> dict[str, Any]:
        degree = {node_id: 0 for node_id in self.nodes}
        for edge in self.edges.values():
            degree[edge["s"]] = degree.get(edge["s"], 0) + 1
            degree[edge["t"]] = degree.get(edge["t"], 0) + 1
        nodes = [dict(node, degree=degree.get(node_id, 0)) for node_id, node in self.nodes.items()]
        return {
            "nodes": nodes, "edges": list(self.edges.values()), "total": len(self.edges),
            "node_total": len(nodes), "layers": list(self.layers),
            "layer_counts": self.counts, "warnings": self.warnings,
            "scope": ScopeCodec.to_dict(self.scope), "read_only": True,
            "generated_at": time.time(),
        }


def _bot_node(graph: GraphProjection, layer: str) -> str:
    return graph.node(f"bot:{graph.scope.bot_id}", graph.scope.bot_id, "bot", layer, bot_id=graph.scope.bot_id)


def _memory_node(graph: GraphProjection, row: dict[str, Any], layer: str = "memories") -> str:
    memory_id = int(row["id"])
    content = _clip(row.get("content"), 500)
    sender_name = str(row.get("sender_name") or "").strip()
    # 标签优化：去掉 @昵称(QQ号) 前缀噪声，用发送者昵称 + 摘要
    import re
    clean = re.sub(r"@[^\s(]*\(\d+\)\s*", "", content).strip()
    if sender_name and clean:
        label = f"{sender_name}: {clean[:30]}"
    elif sender_name:
        label = sender_name
    elif clean:
        label = clean[:40]
    else:
        label = f"记忆 #{memory_id}"
    return graph.node(
        f"memory:{memory_id}", label, "memory", layer, memory_id=memory_id,
        content=content, sender_id=row.get("sender_id"), sender_name=sender_name,
        importance=float(row.get("importance") or 0), source=row.get("source"),
        ts=float(row.get("timestamp") or 0), timestamp=float(row.get("timestamp") or 0),
    )


def _entity_node(graph: GraphProjection, name: Any, node_type: str = "entity", layer: str = "facts", **meta: Any) -> str:
    text = str(name or "").strip()
    return graph.node(f"entity:{text}", text, node_type or "entity", layer, **meta)


def _project_facts(graph: GraphProjection, conn: Any, min_confidence: float) -> None:
    layer = "facts"
    params = _scope_params(graph.scope)
    fact_columns = _columns(conn, "scoped_facts")
    if {"bot_id", "session_id", "visibility", "subject", "predicate", "object"} <= fact_columns:
        revision_sql = "revision" if "revision" in fact_columns else "1 AS revision"
        status_filter = "AND status NOT IN ('deleted','superseded')" if "status" in fact_columns else ""
        for row in _rows(conn, f"""SELECT id, subject, predicate, object, confidence, status,
                                      source_memory_id, provenance, created_at, updated_at,
                                      {revision_sql}
                                 FROM scoped_facts
                                WHERE bot_id=? AND session_id=? AND visibility=? AND confidence>=?
                                  {status_filter}
                                ORDER BY updated_at DESC, id DESC LIMIT 2000""", (*params, min_confidence)):
            source_name, target_name = str(row["subject"] or "").strip(), str(row["object"] or "").strip()
            if not source_name or not target_name:
                continue
            source = _entity_node(graph, source_name, "entity", layer)
            target = _entity_node(graph, target_name, "entity", layer)
            graph.edge(
                f"fact:{row['id']}", source, target, row.get("predicate") or "relates", layer, "fact",
                weight=float(row.get("confidence") or 0), confidence=float(row.get("confidence") or 0),
                ts=float(row.get("updated_at") or row.get("created_at") or 0),
                source_type="entity", target_type="entity", fact_id=int(row["id"]),
                source_memory_id=row.get("source_memory_id"), status=row.get("status"),
                revision=int(row.get("revision") or 1),
                provenance=_json(row.get("provenance"), {}),
            )
    else:
        graph.warn(layer, "scoped_facts_unavailable")

    tag_columns = _columns(conn, "scoped_tags")
    relation_columns = _columns(conn, "scoped_tag_relations")
    if {"id", "bot_id", "session_id", "visibility", "name"} <= tag_columns and {
        "id", "bot_id", "session_id", "visibility", "source_tag_id", "target_tag_id"
    } <= relation_columns:
        relation_revision_sql = "r.revision" if "revision" in relation_columns else "1 AS revision"
        relation_status_sql = "r.status" if "status" in relation_columns else "'active' AS status"
        relation_valid_until_sql = "r.valid_until" if "valid_until" in relation_columns else "NULL AS valid_until"
        relation_status_filter = "AND r.status NOT IN ('deleted','superseded')" if "status" in relation_columns else ""
        for row in _rows(conn, f"""SELECT r.id, r.relation_type, r.weight, r.confidence, r.metadata,
                                      r.created_at, r.updated_at, source.name AS source_name,
                                      target.name AS target_name, source.tag_type AS source_type,
                                      target.tag_type AS target_type, {relation_status_sql},
                                      {relation_valid_until_sql}, {relation_revision_sql}
                                 FROM scoped_tag_relations r
                                 JOIN scoped_tags source ON source.id=r.source_tag_id
                                  AND source.bot_id=r.bot_id AND source.session_id=r.session_id AND source.visibility=r.visibility
                                 JOIN scoped_tags target ON target.id=r.target_tag_id
                                  AND target.bot_id=r.bot_id AND target.session_id=r.session_id AND target.visibility=r.visibility
                                WHERE r.bot_id=? AND r.session_id=? AND r.visibility=? AND r.confidence>=?
                                  {relation_status_filter}
                                ORDER BY r.updated_at DESC, r.id DESC LIMIT 2000""", (*params, min_confidence)):
            source = _entity_node(graph, row["source_name"], row.get("source_type") or "topic", layer)
            target = _entity_node(graph, row["target_name"], row.get("target_type") or "topic", layer)
            graph.edge(
                f"tagrel:{row['id']}", source, target, row.get("relation_type") or "relates", layer,
                "tag_relation", weight=float(row.get("weight") or 0),
                confidence=float(row.get("confidence") or 0),
                ts=float(row.get("updated_at") or row.get("created_at") or 0),
                source_type=row.get("source_type") or "topic", target_type=row.get("target_type") or "topic",
                relation_id=int(row["id"]), metadata=_json(row.get("metadata"), {}),
                status=row.get("status"), valid_until=row.get("valid_until"),
                revision=int(row.get("revision") or 1),
            )


def _scoped_memory_rows(conn: Any, scope: RuntimeScope, limit: int) -> list[dict[str, Any]]:
    required = {"id", "bot_id", "session_id", "visibility", "content", "resolution_state"}
    columns = _columns(conn, "memories")
    if not required <= columns:
        return []
    selected = [name for name in (
        "id", "content", "vector", "timestamp", "importance", "source", "sender_id", "sender_name"
    ) if name in columns]
    quarantine = "AND COALESCE(quarantine,0)=0" if "quarantine" in columns else ""
    return _rows(conn, f"""SELECT {', '.join(selected)} FROM memories
                             WHERE bot_id=? AND session_id=? AND visibility=?
                               AND resolution_state='resolved' {quarantine}
                             ORDER BY COALESCE(importance,0) DESC, COALESCE(timestamp,0) DESC, id DESC
                             LIMIT ?""", (*_scope_params(scope), limit))


def _project_memories(
    graph: GraphProjection, conn: Any, memory_index: Any, *, memory_limit: int,
    similarity_k: int, similarity_threshold: float,
) -> None:
    layer = "memories"
    rows = _scoped_memory_rows(conn, graph.scope, memory_limit)
    if not rows:
        graph.warn(layer, "scoped_memories_empty_or_unavailable")
        return
    by_id = {int(row["id"]): row for row in rows}
    for row in rows:
        _memory_node(graph, row)

    if {"bot_id", "session_id", "visibility", "memory_id", "tag_id"} <= _columns(conn, "scoped_memory_tags") and {
        "id", "bot_id", "session_id", "visibility", "name"
    } <= _columns(conn, "scoped_tags"):
        effective = [
            row for row in effective_tag_rows(conn, scope=graph.scope)
            if int(row["memory_id"]) in by_id and row.get("tag_id") is not None
        ]
        tag_ids = sorted({int(row["tag_id"]) for row in effective})
        tag_by_id: dict[int, dict[str, Any]] = {}
        if tag_ids:
            placeholders = ",".join("?" for _ in tag_ids)
            tag_rows = _rows(
                conn,
                f"""SELECT id, name, tag_type, confidence, description
                       FROM scoped_tags
                      WHERE bot_id=? AND session_id=? AND visibility=?
                        AND id IN ({placeholders})""",
                (*_scope_params(graph.scope), *tag_ids),
            )
            tag_by_id = {int(row["id"]): row for row in tag_rows}
        for link in effective:
            tag_id = int(link["tag_id"])
            tag = tag_by_id.get(tag_id)
            if tag is None:
                continue
            memory_id = int(link["memory_id"])
            memory_node = f"memory:{memory_id}"
            tag_node = _entity_node(
                graph, tag.get("name"), tag.get("tag_type") or "keyword", layer,
                tag_id=tag_id, description=_clip(tag.get("description"), 240),
            )
            relevance = float(link.get("relevance") or 0)
            graph.edge(
                f"memory-tag:{memory_id}:{tag_id}", memory_node, tag_node, "标注", layer,
                "memory_tag", weight=relevance, confidence=float(tag.get("confidence") or relevance),
                ts=0.0, source_type="memory", target_type=tag.get("tag_type") or "keyword",
                memory_id=memory_id, tag_id=tag_id,
            )
    else:
        graph.warn(layer, "scoped_memory_tags_unavailable")

    vector_rows = {memory_id: row for memory_id, row in by_id.items() if row.get("vector")}
    if not vector_rows:
        graph.warn(layer, "scoped_memory_vectors_empty")
        return
    if memory_index is None or not callable(getattr(memory_index, "search", None)):
        graph.warn(layer, "hnsw_index_unavailable")
        return
    allowed = set(vector_rows)
    seen: set[tuple[int, int]] = set()
    search_k = max(16, min(100, similarity_k * 8))
    try:
        for memory_id, row in vector_rows.items():
            vector = np.frombuffer(row["vector"], dtype=np.float32)
            if not vector.size:
                continue
            for neighbor_id, distance in memory_index.search(vector, k=search_k):
                neighbor_id = int(neighbor_id)
                if neighbor_id == memory_id or neighbor_id not in allowed:
                    continue
                pair = tuple(sorted((memory_id, neighbor_id)))
                if pair in seen:
                    continue
                similarity = 1.0 - float(distance)
                if similarity < similarity_threshold:
                    continue
                seen.add(pair)
                graph.edge(
                    f"hnsw:{pair[0]}:{pair[1]}", f"memory:{pair[0]}", f"memory:{pair[1]}",
                    "HNSW 近邻", layer, "hnsw_neighbor", weight=similarity, confidence=similarity,
                    source_type="memory", target_type="memory", similarity=similarity,
                )
                if sum(1 for item in seen if memory_id in item) >= similarity_k:
                    break
    except Exception as exc:
        graph.warn(layer, f"hnsw_query_failed:{type(exc).__name__}")


def _project_simple_scoped_layers(graph: GraphProjection, conn: Any) -> None:
    params = _scope_params(graph.scope)
    bot = None
    if "beliefs" in graph.layers:
        bot = bot or _bot_node(graph, "beliefs")
        if {"id", "bot_id", "session_id", "visibility", "content"} <= _columns(conn, "scoped_beliefs"):
            for row in _rows(conn, """SELECT id, belief_key, content, belief_type, strength, status,
                                          source_memory_id, provenance, created_at, updated_at
                                     FROM scoped_beliefs WHERE bot_id=? AND session_id=? AND visibility=?
                                     ORDER BY strength DESC, updated_at DESC LIMIT 500""", params):
                node = graph.node(
                    f"belief:{row['id']}", row.get("content") or row.get("belief_key"), "belief", "beliefs",
                    belief_id=row["id"], belief_key=row.get("belief_key"), content=_clip(row.get("content")),
                    belief_type=row.get("belief_type"), strength=float(row.get("strength") or 0),
                    status=row.get("status"), ts=float(row.get("updated_at") or row.get("created_at") or 0),
                    provenance=_json(row.get("provenance"), {}),
                )
                graph.edge(f"bot-belief:{row['id']}", bot, node, "持有信念", "beliefs", "belief",
                           weight=float(row.get("strength") or 0), source_type="bot", target_type="belief")
                if row.get("source_memory_id"):
                    source = graph.node(f"memory:{row['source_memory_id']}", f"记忆 #{row['source_memory_id']}", "memory", "beliefs", memory_id=row["source_memory_id"])
                    graph.edge(f"belief-evidence:{row['id']}", source, node, "证据", "beliefs", "evidence", source_type="memory", target_type="belief")
        else:
            graph.warn("beliefs", "scoped_beliefs_unavailable")

    if "jargon" in graph.layers:
        bot = bot or _bot_node(graph, "jargon")
        if {"id", "bot_id", "session_id", "visibility", "word"} <= _columns(conn, "scoped_jargon"):
            for row in _rows(conn, """SELECT id, word, meaning, status, frequency, confidence, contexts,
                                          source_memory_id, source_context, provenance, created_at, updated_at
                                     FROM scoped_jargon WHERE bot_id=? AND session_id=? AND visibility=?
                                     ORDER BY confidence DESC, frequency DESC, updated_at DESC LIMIT 500""", params):
                node = graph.node(
                    f"jargon:{row['id']}", row.get("word"), "jargon", "jargon", jargon_id=row["id"],
                    meaning=_clip(row.get("meaning")), status=row.get("status"), frequency=int(row.get("frequency") or 0),
                    confidence=float(row.get("confidence") or 0), contexts=_json(row.get("contexts"), []),
                    source_context=_clip(row.get("source_context")), provenance=_json(row.get("provenance"), {}),
                    ts=float(row.get("updated_at") or row.get("created_at") or 0),
                )
                graph.edge(f"bot-jargon:{row['id']}", bot, node, "群内表达", "jargon", "jargon",
                           weight=float(row.get("confidence") or 0), source_type="bot", target_type="jargon")
                if row.get("source_memory_id"):
                    source = graph.node(f"memory:{row['source_memory_id']}", f"记忆 #{row['source_memory_id']}", "memory", "jargon", memory_id=row["source_memory_id"])
                    graph.edge(f"jargon-evidence:{row['id']}", source, node, "来源", "jargon", "evidence", source_type="memory", target_type="jargon")
        else:
            graph.warn("jargon", "scoped_jargon_unavailable")

    if "concerns" in graph.layers:
        bot = bot or _bot_node(graph, "concerns")
        if {"id", "bot_id", "session_id", "visibility", "topic"} <= _columns(conn, "scoped_soul_concerns"):
            for row in _rows(conn, """SELECT id, topic, intensity, origin_memory_id, revision, evidence,
                                          created_at, last_triggered FROM scoped_soul_concerns
                                     WHERE bot_id=? AND session_id=? AND visibility=?
                                     ORDER BY intensity DESC, last_triggered DESC LIMIT 500""", params):
                node = graph.node(f"concern:{row['id']}", row.get("topic"), "concern", "concerns",
                                  concern_id=row["id"], intensity=float(row.get("intensity") or 0),
                                  revision=row.get("revision"), evidence=_json(row.get("evidence"), []),
                                  ts=float(row.get("last_triggered") or row.get("created_at") or 0))
                graph.edge(f"bot-concern:{row['id']}", bot, node, "当前关切", "concerns", "concern",
                           weight=float(row.get("intensity") or 0), source_type="bot", target_type="concern")
                if row.get("origin_memory_id"):
                    source = graph.node(f"memory:{row['origin_memory_id']}", f"记忆 #{row['origin_memory_id']}", "memory", "concerns", memory_id=row["origin_memory_id"])
                    graph.edge(f"concern-origin:{row['id']}", source, node, "触发", "concerns", "evidence", source_type="memory", target_type="concern")
        else:
            graph.warn("concerns", "scoped_soul_concerns_unavailable")

    if "mood" in graph.layers:
        bot = bot or _bot_node(graph, "mood")
        if {"bot_id", "session_id", "visibility", "valence", "arousal"} <= _columns(conn, "scoped_soul_mood"):
            rows = _rows(conn, """SELECT valence, arousal, cause, policy_version, revision, evidence,
                                         observed_at, updated_at FROM scoped_soul_mood
                                    WHERE bot_id=? AND session_id=? AND visibility=? LIMIT 1""", params)
            for row in rows:
                valence, arousal = float(row.get("valence") or 0), float(row.get("arousal") or 0)
                node = graph.node(f"mood:{params[0]}:{params[1]}", f"情绪 V{valence:.2f} / A{arousal:.2f}", "mood", "mood",
                                  valence=valence, arousal=arousal, cause=_clip(row.get("cause")),
                                  policy_version=row.get("policy_version"), revision=row.get("revision"),
                                  evidence=_json(row.get("evidence"), []), ts=float(row.get("updated_at") or row.get("observed_at") or 0))
                graph.edge("bot-mood", bot, node, "当前情绪", "mood", "mood", weight=max(abs(valence), arousal), source_type="bot", target_type="mood")
        else:
            graph.warn("mood", "scoped_soul_mood_unavailable")

    if "timeline" in graph.layers:
        bot = bot or _bot_node(graph, "timeline")
        if {"id", "bot_id", "session_id", "visibility", "event_summary"} <= _columns(conn, "scoped_soul_timeline"):
            for row in _rows(conn, """SELECT id, subject_principal_id, event_summary, event_type, emotional_weight,
                                          occurred_at, revision, evidence, created_at FROM scoped_soul_timeline
                                     WHERE bot_id=? AND session_id=? AND visibility=?
                                     ORDER BY occurred_at DESC, id DESC LIMIT 500""", params):
                node = graph.node(f"timeline:{row['id']}", row.get("event_summary"), "timeline", "timeline",
                                  timeline_id=row["id"], event_type=row.get("event_type"),
                                  emotional_weight=float(row.get("emotional_weight") or 0), revision=row.get("revision"),
                                  evidence=_json(row.get("evidence"), []), ts=float(row.get("occurred_at") or row.get("created_at") or 0))
                subject = row.get("subject_principal_id")
                source = graph.node(f"person:{subject}", subject, "person", "timeline", subject_principal_id=subject) if subject else bot
                graph.edge(f"timeline-link:{row['id']}", source, node, row.get("event_type") or "时间锚点", "timeline", "timeline_event",
                           weight=abs(float(row.get("emotional_weight") or 0)), source_type="person" if subject else "bot", target_type="timeline")
        else:
            graph.warn("timeline", "scoped_soul_timeline_unavailable")

    if "affinity" in graph.layers:
        bot = bot or _bot_node(graph, "affinity")
        if {"bot_id", "session_id", "visibility", "subject_principal_id"} <= _columns(conn, "scoped_soul_relationships"):
            for row in _rows(conn, """SELECT subject_principal_id, affinity, state, dimensions, revision, evidence, updated_at
                                     FROM scoped_soul_relationships WHERE bot_id=? AND session_id=? AND visibility=?
                                     ORDER BY ABS(affinity) DESC, updated_at DESC LIMIT 500""", params):
                subject = str(row.get("subject_principal_id") or "").strip()
                if not subject:
                    continue
                person = graph.node(f"person:{subject}", subject, "person", "affinity", subject_principal_id=subject)
                affinity = float(row.get("affinity") or 0)
                graph.edge(f"affinity:{subject}", bot, person, row.get("state") or "关系", "affinity", "affinity",
                           weight=min(1.0, abs(affinity) / 100.0), confidence=1.0, ts=float(row.get("updated_at") or 0),
                           source_type="bot", target_type="person", affinity=affinity,
                           dimensions=_json(row.get("dimensions"), {}), revision=row.get("revision"), evidence=_json(row.get("evidence"), []))
        else:
            graph.warn("affinity", "scoped_soul_relationships_unavailable")
        if {"id", "bot_id", "session_id", "visibility", "subject_principal_id"} <= _columns(conn, "scoped_soul_relationship_events"):
            for row in _rows(conn, """SELECT id, subject_principal_id, event_type, dimension, delta, reason,
                                          source_episode_id, source_memory_id, revision, created_at
                                     FROM scoped_soul_relationship_events
                                    WHERE bot_id=? AND session_id=? AND visibility=?
                                    ORDER BY created_at DESC, id DESC LIMIT 500""", params):
                subject = str(row.get("subject_principal_id") or "").strip()
                if not subject:
                    continue
                person = graph.node(f"person:{subject}", subject, "person", "affinity", subject_principal_id=subject)
                event = graph.node(f"relationship-event:{row['id']}", row.get("reason") or row.get("event_type"), "relationship_event", "affinity",
                                   event_id=row["id"], event_type=row.get("event_type"), dimension=row.get("dimension"),
                                   delta=float(row.get("delta") or 0), revision=row.get("revision"), ts=float(row.get("created_at") or 0))
                graph.edge(f"relationship-event-link:{row['id']}", person, event, row.get("event_type") or "关系事件", "affinity", "relationship_event",
                           weight=min(1.0, abs(float(row.get("delta") or 0)) / 10.0), source_type="person", target_type="relationship_event")
                if row.get("source_memory_id"):
                    memory = graph.node(f"memory:{row['source_memory_id']}", f"记忆 #{row['source_memory_id']}", "memory", "affinity", memory_id=row["source_memory_id"])
                    graph.edge(f"relationship-event-evidence:{row['id']}", memory, event, "证据", "affinity", "evidence", source_type="memory", target_type="relationship_event")


def _evidence_memory_ids(items: Any) -> list[int]:
    result: list[int] = []
    for evidence in items or ():
        data = _safe(evidence)
        if not isinstance(data, dict) or str(data.get("kind") or "") not in {"memory", "episode"}:
            continue
        try:
            result.append(int(data.get("id")))
        except (TypeError, ValueError):
            pass
    return result


def _project_repository_layers(graph: GraphProjection, fewshot_repository: Any) -> None:
    if "few_shot" in graph.layers:
        bot = _bot_node(graph, "few_shot")
        if fewshot_repository is None or not callable(getattr(fewshot_repository, "list_approved", None)):
            graph.warn("few_shot", "scoped_fewshot_repository_unavailable")
        else:
            try:
                for item in fewshot_repository.list_approved(scope=graph.scope, limit=500, offset=0):
                    node = graph.node(f"few-shot:{item['id']}", _clip(item.get("content"), 120), "few_shot", "few_shot",
                                      example_id=item["id"], content=_clip(item.get("content")), score=float(item.get("score") or 0),
                                      traits=_safe(item.get("traits") or ()), revision=item.get("revision"), ts=float(item.get("updated_at") or 0))
                    graph.edge(f"bot-few-shot:{item['id']}", bot, node, "示例", "few_shot", "few_shot",
                               weight=float(item.get("score") or 0), source_type="bot", target_type="few_shot")
                    for trait in item.get("traits") or ():
                        trait_node = graph.node(f"trait:{trait}", trait, "trait", "few_shot")
                        graph.edge(f"few-shot-trait:{item['id']}:{trait}", node, trait_node, "风格特征", "few_shot", "trait", source_type="few_shot", target_type="trait")
                    for memory_id in _evidence_memory_ids(item.get("evidence_refs")):
                        memory = graph.node(f"memory:{memory_id}", f"记忆 #{memory_id}", "memory", "few_shot", memory_id=memory_id)
                        graph.edge(f"few-shot-evidence:{item['id']}:{memory_id}", memory, node, "证据", "few_shot", "evidence", source_type="memory", target_type="few_shot")
            except Exception as exc:
                graph.warn("few_shot", f"scoped_fewshot_projection_failed:{type(exc).__name__}")
    if "book_lore" in graph.layers:
        # 书设是独立 Catalog 直读源，不再投影 reviewed_book_lore_projections。
        graph.warn("book_lore", "book_lore_is_catalog_readonly_not_projection")


def _project_communities(graph: GraphProjection, conn: Any) -> None:
    layer = "communities"
    params = _scope_params(graph.scope)
    required_tags = {"id", "bot_id", "session_id", "visibility", "name"}
    required_relations = {"source_tag_id", "target_tag_id", "bot_id", "session_id", "visibility"}
    if not required_tags <= _columns(conn, "scoped_tags") or not required_relations <= _columns(conn, "scoped_tag_relations"):
        graph.warn(layer, "scoped_tag_graph_unavailable")
        return
    tags = {int(row["id"]): row for row in _rows(conn, """SELECT id, name, tag_type, confidence FROM scoped_tags
                                                               WHERE bot_id=? AND session_id=? AND visibility=?""", params)}
    relation_filter = "AND status NOT IN ('deleted','superseded')" if "status" in _columns(conn, "scoped_tag_relations") else ""
    relations = _rows(conn, f"""SELECT source_tag_id, target_tag_id, weight FROM scoped_tag_relations
                                WHERE bot_id=? AND session_id=? AND visibility=? {relation_filter}""", params)
    parent = {tag_id: tag_id for tag_id in tags}

    def root(value: int) -> int:
        while parent[value] != value:
            parent[value] = parent[parent[value]]
            value = parent[value]
        return value

    def union(left: int, right: int) -> None:
        if left in parent and right in parent:
            a, b = root(left), root(right)
            if a != b:
                parent[b] = a

    for relation in relations:
        union(int(relation["source_tag_id"]), int(relation["target_tag_id"]))
    groups: dict[int, list[int]] = {}
    for tag_id in tags:
        groups.setdefault(root(tag_id), []).append(tag_id)
    ranked = sorted((members for members in groups.values() if len(members) >= 2), key=len, reverse=True)[:100]
    for index, members in enumerate(ranked, 1):
        names = [str(tags[tag_id].get("name") or "") for tag_id in members]
        community = graph.node(f"tag-community:{index}", f"标签簇 {index} · {names[0]}", "community", layer,
                               community=index, size=len(members), members=names[:50])
        for tag_id in members[:100]:
            row = tags[tag_id]
            tag = _entity_node(graph, row.get("name"), row.get("tag_type") or "keyword", layer, community=index, tag_id=tag_id)
            graph.edge(f"community-member:{index}:{tag_id}", community, tag, "成员", layer, "community_member",
                       weight=float(row.get("confidence") or 0.5), source_type="community", target_type=row.get("tag_type") or "keyword", community=index)


def build_graph_projection(
    *, conn: Any, scope: RuntimeScope, layers: Iterable[str], memory_index: Any = None,
    fewshot_repository: Any = None, book_lore_repository: Any = None,
    min_confidence: float = 0.0, memory_limit: int = 150,
    similarity_k: int = 3, similarity_threshold: float = 0.65,
) -> dict[str, Any]:
    # book_lore_repository 参数仅兼容旧调用方，不再读取 reviewed projection。
    requested = tuple(dict.fromkeys(str(layer).strip() for layer in layers if str(layer).strip()))
    invalid = sorted(set(requested) - set(SUPPORTED_LAYERS))
    if invalid:
        raise ValueError("unsupported_layers:" + ",".join(invalid))
    if not requested:
        requested = ("facts",)
    _scope_params(scope)
    graph = GraphProjection(scope, requested)
    if conn is None:
        graph.warn("all", "database_unavailable")
        return graph.payload()
    if "facts" in requested:
        _project_facts(graph, conn, min_confidence)
    if "memories" in requested:
        _project_memories(
            graph, conn, memory_index, memory_limit=memory_limit,
            similarity_k=similarity_k, similarity_threshold=similarity_threshold,
        )
    _project_simple_scoped_layers(graph, conn)
    _project_repository_layers(graph, fewshot_repository)
    if "communities" in requested:
        _project_communities(graph, conn)
    return graph.payload()


def scoped_layer_counts(conn: Any, scope: RuntimeScope) -> dict[str, int | None]:
    params = _scope_params(scope)
    table_map = {
        "facts": "scoped_facts", "memories": "memories", "beliefs": "scoped_beliefs",
        "jargon": "scoped_jargon", "concerns": "scoped_soul_concerns", "mood": "scoped_soul_mood",
        "timeline": "scoped_soul_timeline", "affinity": "scoped_soul_relationships",
        "few_shot": "scoped_few_shot_examples",
        "communities": "scoped_tag_relations",
    }
    result: dict[str, int | None] = {"book_lore": None}
    for layer, table in table_map.items():
        columns = _columns(conn, table)
        try:
            if {"bot_id", "session_id", "visibility"} <= columns:
                extra = " AND resolution_state='resolved' AND COALESCE(quarantine,0)=0" if table == "memories" and "quarantine" in columns else ""
                if table in {"scoped_facts", "scoped_tag_relations"} and "status" in columns:
                    extra += " AND status NOT IN ('deleted','superseded')"
                result[layer] = int(conn.execute(
                    f"SELECT COUNT(*) FROM {table} WHERE bot_id=? AND session_id=? AND visibility=?{extra}", params
                ).fetchone()[0])
            elif table == "scoped_few_shot_examples" and "runtime_scope_key" in columns:
                result[layer] = None
            else:
                result[layer] = None
        except Exception:
            result[layer] = None
    return result


TAG_GRAPH_LAYERS = ("cooccurrence", "relations")


def _ordinal_potential(position: int, max_position: int) -> float:
    """与 engine.directed_cooccurrence.ordinal_potential 保持同一序位势能公式。"""
    if position <= 0 or max_position <= 1:
        return 0.7
    return 0.9 - 0.4 * (position - 1) / (max_position - 1)


def _active_tag_rows(conn: Any, scope: RuntimeScope) -> list[dict[str, Any]]:
    columns = _columns(conn, "scoped_tags")
    required = {"id", "bot_id", "session_id", "visibility", "name"}
    if not required <= columns:
        return []
    selected = [
        name for name in (
            "id", "name", "tag_type", "description", "confidence", "metadata",
            "status", "revision", "created_at", "updated_at",
        ) if name in columns
    ]
    status_filter = " AND status='active'" if "status" in columns else ""
    return _rows(
        conn,
        f"""SELECT {', '.join(selected)} FROM scoped_tags
              WHERE bot_id=? AND session_id=? AND visibility=?{status_filter}
              ORDER BY updated_at DESC, id DESC""",
        _scope_params(scope),
    )


def _live_memory_rows(conn: Any, scope: RuntimeScope, memory_ids: Iterable[int]) -> dict[int, dict[str, Any]]:
    ids = sorted({int(value) for value in memory_ids})
    columns = _columns(conn, "memories")
    required = {"id", "bot_id", "session_id", "visibility", "content", "resolution_state"}
    if not ids or not required <= columns:
        return {}
    selected = [
        name for name in (
            "id", "content", "sender_id", "sender_name", "timestamp", "importance",
            "source", "version", "quarantine", "memory_type",
        ) if name in columns
    ]
    placeholders = ",".join("?" for _ in ids)
    quarantine = " AND COALESCE(quarantine,0)=0" if "quarantine" in columns else ""
    lifecycle = (
        " AND COALESCE(memory_type,'message') NOT IN ('archived','evicted','deleted')"
        if "memory_type" in columns else ""
    )
    rows = _rows(
        conn,
        f"""SELECT {', '.join(selected)} FROM memories
              WHERE id IN ({placeholders})
                AND bot_id=? AND session_id=? AND visibility=?
                AND resolution_state='resolved'{quarantine}{lifecycle}""",
        (*ids, *_scope_params(scope)),
    )
    return {int(row["id"]): row for row in rows}


def _effective_links(conn: Any, scope: RuntimeScope, tag_ids: Iterable[Any] | None = None) -> list[dict[str, Any]]:
    try:
        return effective_tag_rows(conn, scope=scope, tag_ids=tag_ids)
    except Exception:
        columns = _columns(conn, "scoped_memory_tags")
        required = {"bot_id", "session_id", "visibility", "memory_id", "tag_id"}
        if not required <= columns:
            return []
        position = "position" if "position" in columns else "0 AS position"
        relevance = "relevance" if "relevance" in columns else "1.0 AS relevance"
        where_tag = ""
        params = list(_scope_params(scope))
        if tag_ids is not None:
            clean_tags = [int(x) for x in tag_ids if int(x) > 0]
            if not clean_tags:
                return []
            placeholders = ",".join("?" for _ in clean_tags)
            where_tag = f" AND tag_id IN ({placeholders})"
            params.extend(clean_tags)
        return _rows(
            conn,
            f"""SELECT bot_id, session_id, visibility, memory_id, tag_id,
                       {position}, {relevance}, 'automatic' AS source, NULL AS correction_id
                  FROM scoped_memory_tags
                 WHERE bot_id=? AND session_id=? AND visibility=?{where_tag}
                 ORDER BY memory_id, position, tag_id""",
            params,
        )


def _pulse_fields(weight: float, latest_ts: float, *, now: float, half_life_hours: float) -> dict[str, float]:
    if latest_ts <= 0:
        return {"pulse_energy": 0.0, "pulse_decay": 0.0}
    half_life_seconds = max(1.0, float(half_life_hours) * 3600.0)
    age = max(0.0, now - latest_ts)
    decay = math.exp(-math.log(2.0) * age / half_life_seconds)
    return {"pulse_energy": round(max(0.0, weight) * decay, 6), "pulse_decay": round(decay, 6)}


def build_tag_graph_projection(
    *, conn: Any, scope: RuntimeScope, layers: Iterable[str] = TAG_GRAPH_LAYERS,
    min_confidence: float = 0.0, max_nodes: int = 300, include_pulse: bool = False,
    pulse_half_life_hours: float = 72.0, now: float | None = None,
) -> dict[str, Any]:
    """构造严格 Scope 隔离的正式 Tag 神经云图只读投影。"""
    requested = tuple(dict.fromkeys(str(layer).strip() for layer in layers if str(layer).strip()))
    invalid = sorted(set(requested) - set(TAG_GRAPH_LAYERS))
    if invalid:
        raise ValueError("unsupported_tag_graph_layers:" + ",".join(invalid))
    _scope_params(scope)
    generated_at = float(time.time() if now is None else now)
    if conn is None:
        return {
            "nodes": [], "edges": [], "layers": list(requested),
            "available_layers": list(TAG_GRAPH_LAYERS),
            "layer_counts": {layer: {"nodes": 0, "edges": 0} for layer in requested},
            "tag_total": 0,
            "scope": ScopeCodec.to_dict(scope), "read_only": True, "generated_at": generated_at,
            "warnings": [{"layer": "all", "reason": "database_unavailable"}],
            "pulse": {"enabled": bool(include_pulse), "half_life_hours": float(pulse_half_life_hours)},
        }

    tag_rows = _active_tag_rows(conn, scope)
    tags = {int(row["id"]): row for row in tag_rows}
    # 1. 节点前置收敛：避免对几千个全量标签做全矩阵扫描
    candidate_cap = max(80, min(len(tag_rows), int(max_nodes * 1.5)))
    candidate_tags = sorted(tag_rows, key=lambda r: float(r.get("confidence") or 0.0), reverse=True)[:candidate_cap]
    candidate_ids = [int(r["id"]) for r in candidate_tags]

    # 2. 链接查询下推 candidate_ids，大幅缩减 link 数量
    links = [row for row in _effective_links(conn, scope, tag_ids=candidate_ids) if row.get("tag_id") is not None]
    links = [row for row in links if int(row["tag_id"]) in tags]

    # 3. 记忆查询前置收敛为最新活跃记忆，避免数万参数 SQL 溢出
    active_mids = sorted({int(row["memory_id"]) for row in links}, reverse=True)[:1500]
    memories = _live_memory_rows(conn, scope, active_mids)
    links = [row for row in links if int(row["memory_id"]) in memories]

    links_by_memory: dict[int, list[dict[str, Any]]] = defaultdict(list)
    links_by_tag: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for link in links:
        memory_id = int(link["memory_id"])
        tag_id = int(link["tag_id"])
        links_by_memory[memory_id].append(link)
        links_by_tag[tag_id].append(link)
    for values in links_by_memory.values():
        values.sort(key=lambda item: (int(item.get("position") or 0), int(item["tag_id"])))

    edges: list[dict[str, Any]] = []
    layer_counts = {layer: {"nodes": 0, "edges": 0} for layer in requested}
    if "cooccurrence" in requested:
        aggregates: dict[tuple[int, int], dict[str, Any]] = {}
        for memory_id, memory_links in links_by_memory.items():
            if len(memory_links) < 2:
                continue
            max_position = max(int(item.get("position") or 0) for item in memory_links) or len(memory_links)
            memory = memories[memory_id]
            memory_ts = float(memory.get("timestamp") or 0.0)
            for source_link in memory_links:
                source_id = int(source_link["tag_id"])
                source_position = int(source_link.get("position") or 0)
                source_relevance = max(0.0, float(source_link.get("relevance") or 0.0))
                for target_link in memory_links:
                    target_id = int(target_link["tag_id"])
                    if source_id == target_id:
                        continue
                    target_position = int(target_link.get("position") or 0)
                    target_relevance = max(0.0, float(target_link.get("relevance") or 0.0))
                    key = (source_id, target_id)
                    item = aggregates.setdefault(key, {
                        "raw_weight": 0.0, "frequency": 0, "confidence_sum": 0.0,
                        "latest_ts": 0.0, "memory_ids": [], "source_counts": defaultdict(int),
                    })
                    relevance_gain = math.sqrt(source_relevance * target_relevance)
                    item["raw_weight"] += _ordinal_potential(source_position, max_position) * _ordinal_potential(target_position, max_position) * relevance_gain
                    item["frequency"] += 1
                    item["confidence_sum"] += min(source_relevance, target_relevance)
                    item["latest_ts"] = max(float(item["latest_ts"]), memory_ts)
                    if len(item["memory_ids"]) < 12:
                        item["memory_ids"].append(memory_id)
                    source_kind = "manual" if "manual" in {str(source_link.get("source")), str(target_link.get("source"))} else "automatic"
                    item["source_counts"][source_kind] += 1
        max_raw = max((float(item["raw_weight"]) for item in aggregates.values()), default=0.0)
        max_edges_limit = min(600, int(max_nodes * 2.5))
        # 按共现权重排序，只保留前 max_edges_limit 条强关联骨干突触
        sorted_aggregates = sorted(
            aggregates.items(),
            key=lambda pair: float(pair[1]["raw_weight"]),
            reverse=True,
        )[:max_edges_limit]

        for (source_id, target_id), item in sorted_aggregates:
            weight = float(item["raw_weight"]) / max_raw if max_raw > 0 else 0.0
            tag_confidence = min(float(tags[source_id].get("confidence") or 0.0), float(tags[target_id].get("confidence") or 0.0))
            evidence_confidence = float(item["confidence_sum"]) / max(1, int(item["frequency"]))
            confidence = min(tag_confidence, evidence_confidence)
            if confidence < min_confidence:
                continue
            edge = {
                "id": f"cooccurrence:{source_id}:{target_id}",
                "source": f"tag:{source_id}", "target": f"tag:{target_id}",
                "layer": "cooccurrence", "kind": "directed_cooccurrence",
                "type": "ordinal_cooccurrence", "label": "序位共现",
                "weight": round(weight, 6), "frequency": int(item["frequency"]),
                "confidence": round(confidence, 6), "latest_ts": float(item["latest_ts"]),
                "source_kind": "effective_memory_tags",
                "source_counts": dict(item["source_counts"]),
                "memory_ids": list(dict.fromkeys(item["memory_ids"])),
                "read_only": True,
            }
            if include_pulse:
                edge.update(_pulse_fields(weight, float(item["latest_ts"]), now=generated_at, half_life_hours=pulse_half_life_hours))
            edges.append(edge)

    if "relations" in requested:
        relation_columns = _columns(conn, "scoped_tag_relations")
        required = {"id", "bot_id", "session_id", "visibility", "source_tag_id", "target_tag_id", "relation_type"}
        if required <= relation_columns:
            selected = [name for name in (
                "id", "source_tag_id", "target_tag_id", "relation_type", "weight",
                "confidence", "metadata", "status", "valid_until", "revision", "created_at", "updated_at",
            ) if name in relation_columns]
            status_filter = " AND status='active'" if "status" in relation_columns else ""
            valid_filter = " AND (valid_until IS NULL OR valid_until>?)" if "valid_until" in relation_columns else ""
            params: tuple[Any, ...] = (*_scope_params(scope), *((generated_at,) if valid_filter else ()))
            for row in _rows(
                conn,
                f"""SELECT {', '.join(selected)} FROM scoped_tag_relations
                      WHERE bot_id=? AND session_id=? AND visibility=?{status_filter}{valid_filter}
                      ORDER BY updated_at DESC, id DESC""",
                params,
            ):
                source_id, target_id = int(row["source_tag_id"]), int(row["target_tag_id"])
                confidence = float(row.get("confidence") or 0.0)
                if source_id not in tags or target_id not in tags or source_id == target_id or confidence < min_confidence:
                    continue
                weight = float(row.get("weight") or 0.0)
                latest_ts = float(row.get("updated_at") or row.get("created_at") or 0.0)
                edge = {
                    "id": f"relation:{row['id']}",
                    "source": f"tag:{source_id}", "target": f"tag:{target_id}",
                    "layer": "relations", "kind": "tag_relation",
                    "type": str(row.get("relation_type") or "relates"),
                    "label": str(row.get("relation_type") or "关联"),
                    "weight": round(weight, 6), "frequency": 1,
                    "confidence": round(confidence, 6), "latest_ts": latest_ts,
                    "source_kind": "scoped_tag_relations",
                    "metadata": _json(row.get("metadata"), {}),
                    "revision": int(row.get("revision") or 1), "read_only": True,
                }
                if include_pulse:
                    edge.update(_pulse_fields(weight, latest_ts, now=generated_at, half_life_hours=pulse_half_life_hours))
                edges.append(edge)

    degree_score: dict[int, float] = defaultdict(float)
    for edge in edges:
        source_id = int(str(edge["source"]).split(":", 1)[1])
        target_id = int(str(edge["target"]).split(":", 1)[1])
        degree_score[source_id] += 1.0 + float(edge["weight"])
        degree_score[target_id] += 1.0 + float(edge["weight"])
    ranked_ids = sorted(tags, key=lambda tag_id: (len(links_by_tag.get(tag_id, ())), degree_score[tag_id], float(tags[tag_id].get("confidence") or 0.0), tag_id), reverse=True)
    selected_ids = set(ranked_ids[:max(1, min(10_000, int(max_nodes)))])
    edges = [edge for edge in edges if int(str(edge["source"]).split(":", 1)[1]) in selected_ids and int(str(edge["target"]).split(":", 1)[1]) in selected_ids]

    incoming: dict[int, list[dict[str, Any]]] = defaultdict(list)
    outgoing: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for edge in edges:
        source_id = int(str(edge["source"]).split(":", 1)[1])
        target_id = int(str(edge["target"]).split(":", 1)[1])
        outgoing[source_id].append(edge)
        incoming[target_id].append(edge)
        layer_counts[edge["layer"]]["edges"] += 1

    nodes: list[dict[str, Any]] = []
    for tag_id in ranked_ids:
        if tag_id not in selected_ids:
            continue
        row = tags[tag_id]
        tag_links = links_by_tag.get(tag_id, [])
        source_counts: dict[str, int] = defaultdict(int)
        associated: list[dict[str, Any]] = []
        for link in sorted(tag_links, key=lambda item: float(memories[int(item["memory_id"])].get("timestamp") or 0), reverse=True):
            source_counts[str(link.get("source") or "automatic")] += 1
            if len(associated) >= 5:
                continue
            memory = memories[int(link["memory_id"])]
            associated.append({
                "id": int(memory["id"]), "content": _clip(memory.get("content"), 240),
                "sender": memory.get("sender_name") or memory.get("sender_id") or "",
                "timestamp": float(memory.get("timestamp") or 0.0),
                "importance": float(memory.get("importance") or 0.0),
                "source": memory.get("source"), "version": int(memory.get("version") or 1),
                "tag_source": str(link.get("source") or "automatic"),
                "relevance": float(link.get("relevance") or 0.0),
            })
        node = {
            "id": f"tag:{tag_id}", "locator": tag_id,
            "name": str(row.get("name") or f"Tag {tag_id}"),
            "type": str(row.get("tag_type") or "keyword"),
            "description": _clip(row.get("description"), 500),
            "confidence": float(row.get("confidence") or 0.0),
            "metadata": _json(row.get("metadata"), {}),
            "status": str(row.get("status") or "active"),
            "revision": int(row.get("revision") or 1),
            "memory_count": len({int(link["memory_id"]) for link in tag_links}),
            "frequency": len(tag_links), "source_counts": dict(source_counts),
            "sources": sorted(source_counts), "associated_memories": associated,
            "in_degree": len(incoming[tag_id]), "out_degree": len(outgoing[tag_id]),
            "in_weight": round(sum(float(edge["weight"]) for edge in incoming[tag_id]), 6),
            "out_weight": round(sum(float(edge["weight"]) for edge in outgoing[tag_id]), 6),
            "read_only": True,
        }
        nodes.append(node)
        for layer in requested:
            if any(edge["layer"] == layer for edge in incoming[tag_id] + outgoing[tag_id]):
                layer_counts[layer]["nodes"] += 1

    warnings: list[dict[str, str]] = []
    if not tag_rows:
        warnings.append({"layer": "all", "reason": "scoped_tags_empty_or_unavailable"})
    return {
        "nodes": nodes, "edges": edges, "layers": list(requested), "available_layers": list(TAG_GRAPH_LAYERS),
        "layer_counts": layer_counts, "tag_total": len(tag_rows),
        "scope": ScopeCodec.to_dict(scope), "read_only": True,
        "generated_at": generated_at, "warnings": warnings,
        "pulse": {"enabled": bool(include_pulse), "half_life_hours": float(pulse_half_life_hours)},
    }


def find_tag_graph_path(
    graph: dict[str, Any], *, source_id: str, target_id: str,
    layers: Iterable[str] = TAG_GRAPH_LAYERS, max_depth: int = 6,
) -> dict[str, Any]:
    """在当前可见图层上执行严格有向 Tag→Tag BFS。"""
    visible = tuple(dict.fromkeys(str(layer) for layer in layers if str(layer)))
    invalid = sorted(set(visible) - set(TAG_GRAPH_LAYERS))
    if invalid:
        raise ValueError("unsupported_tag_graph_layers:" + ",".join(invalid))
    node_by_id = {str(node["id"]): node for node in graph.get("nodes", ())}
    if source_id not in node_by_id or target_id not in node_by_id:
        return {"found": False, "path": [], "nodes": [], "edges": [], "layers": list(visible), "read_only": True}
    adjacency: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in graph.get("edges", ()):
        if str(edge.get("layer")) in visible:
            adjacency[str(edge["source"])].append(edge)
    visited: dict[str, tuple[str | None, dict[str, Any] | None]] = {source_id: (None, None)}
    queue = deque([(source_id, 0)])
    depth_limit = max(1, min(12, int(max_depth)))
    while queue and target_id not in visited:
        current, depth = queue.popleft()
        if depth >= depth_limit:
            continue
        for edge in sorted(adjacency.get(current, ()), key=lambda item: (float(item.get("weight") or 0), float(item.get("confidence") or 0)), reverse=True):
            neighbor = str(edge["target"])
            if neighbor not in visited:
                visited[neighbor] = (current, edge)
                queue.append((neighbor, depth + 1))
    if target_id not in visited:
        return {"found": False, "path": [], "nodes": [], "edges": [], "layers": list(visible), "read_only": True}
    node_ids: list[str] = []
    path_edges: list[dict[str, Any]] = []
    current: str | None = target_id
    while current is not None:
        node_ids.append(current)
        parent, edge = visited[current]
        if edge is not None:
            path_edges.append(edge)
        current = parent
    node_ids.reverse()
    path_edges.reverse()
    return {
        "found": True, "path": node_ids, "nodes": [node_by_id[node_id] for node_id in node_ids],
        "edges": path_edges, "layers": list(visible), "read_only": True,
    }


__all__ = [
    "SUPPORTED_LAYERS", "TAG_GRAPH_LAYERS", "build_graph_projection",
    "build_tag_graph_projection", "find_tag_graph_path", "scoped_layer_counts",
]
