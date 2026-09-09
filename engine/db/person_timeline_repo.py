"""Person timeline store: summary + detail rows keyed by bot and user."""

from __future__ import annotations

import json
import time
from collections.abc import Mapping
from typing import Any

from .connection import ConnectionManager
from .migrations.person_timeline import ensure_person_timeline_schema

_JSON_KEYS = (
    "impression",
    "impression_history",
    "impression_ledger",
    "impression_event",
    "impression_snapshot",
    "impression_updated_at",
    "impression_cleared_at",
    "impression_cleared_reason",
    "unsettled_traces",
    "unsettled_energy",
    "unsettled_interaction_count",
)


def _json_dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


class PersonTimelineRepo:
    def __init__(self, cm: ConnectionManager):
        self.cm = cm
        ensure_person_timeline_schema(cm)

    def add_event(
        self,
        *,
        bot_id: str,
        user_id: str,
        group_id: str = "",
        kind: str,
        summary: str,
        detail: str = "",
        subject: str = "",
        predicate: str = "",
        object: str = "",
        confidence: float | None = None,
        occurred_at: float | None = None,
        provenance: Mapping[str, Any] | None = None,
        legacy_fact_id: int | None = None,
        connection=None,
    ) -> int:
        stamp = float(occurred_at if occurred_at is not None else time.time())
        payload = (
            str(bot_id or ""),
            str(user_id or ""),
            str(group_id or ""),
            str(kind or "impression"),
            str(summary or "").strip()[:240],
            str(detail or "").strip(),
            str(subject or ""),
            str(predicate or ""),
            str(object or ""),
            confidence,
            stamp,
            legacy_fact_id,
            _json_dumps(dict(provenance or {})),
            time.time(),
        )
        sql = """INSERT INTO person_timeline_events (
                    bot_id, user_id, group_id, kind, summary, detail, subject, predicate, object,
                    confidence, occurred_at, legacy_fact_id, provenance, created_at
                 ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"""
        if connection is not None:
            cur = connection.execute(sql, payload)
            commit = getattr(connection, "commit", None)
            if callable(commit) and getattr(connection, "in_transaction", False) is False:
                try:
                    commit()
                except Exception:
                    pass
            return int(getattr(cur, "lastrowid", 0) or 0)
        cur = self.cm.execute_write(sql, payload)
        self.cm.commit()
        return int(getattr(cur, "lastrowid", 0) or 0)

    def list_events(
        self,
        *,
        bot_id: str,
        user_id: str,
        group_id: str | None = None,
        query: str = "",
        limit: int = 50,
        connection=None,
    ) -> list[dict[str, Any]]:
        clauses = ["bot_id=?", "user_id=?"]
        params: list[Any] = [str(bot_id or ""), str(user_id or "")]
        if group_id:
            clauses.append("group_id=?")
            params.append(group_id)
        token = str(query or "").strip()
        if token:
            like = f"%{token}%"
            clauses.append("(summary LIKE ? OR detail LIKE ? OR subject LIKE ? OR object LIKE ?)")
            params.extend((like, like, like, like))
        params.append(max(1, int(limit or 50)))
        sql = f"""SELECT id, bot_id, user_id, group_id, kind, summary, detail, subject, predicate, object,
                         confidence, occurred_at, provenance, created_at
                    FROM person_timeline_events
                   WHERE {' AND '.join(clauses)}
                   ORDER BY occurred_at DESC, id DESC
                   LIMIT ?"""
        reader = connection.execute if connection is not None else self.cm.execute_read
        try:
            rows = reader(sql, tuple(params)).fetchall()
        except Exception:
            return []
        items: list[dict[str, Any]] = []
        for row in rows:
            provenance: dict[str, Any] = {}
            raw = row[12]
            if isinstance(raw, str) and raw.strip():
                try:
                    loaded = json.loads(raw)
                    if isinstance(loaded, dict):
                        provenance = loaded
                except Exception:
                    provenance = {}
            items.append({
                "id": row[0],
                "bot_id": row[1],
                "user_id": row[2],
                "group_id": row[3],
                "kind": row[4],
                "summary": row[5],
                "detail": row[6],
                "subject": row[7],
                "predicate": row[8],
                "object": row[9],
                "confidence": row[10],
                "occurred_at": row[11],
                "provenance": provenance,
                "created_at": row[13],
                "text": row[5],
                "at": row[11],
            })
        return items

    def latest_impression(self, *, bot_id: str, user_id: str, group_id: str | None = None, connection=None) -> str:
        clauses = ["bot_id=?", "user_id=?", "kind IN ('impression', 'affinity')"]
        params: list[Any] = [str(bot_id or ""), str(user_id or "")]
        if group_id:
            clauses.append("group_id=?")
            params.append(group_id)
        sql = f"""SELECT COALESCE(NULLIF(detail, ''), summary)
                    FROM person_timeline_events
                   WHERE {' AND '.join(clauses)}
                   ORDER BY occurred_at DESC, id DESC
                   LIMIT 1"""
        reader = connection.execute if connection is not None else self.cm.execute_read
        try:
            row = reader(sql, tuple(params)).fetchone()
        except Exception:
            return ""
        return str(row[0] or "").strip() if row else ""

    def migrate_profile_metadata(
        self,
        *,
        bot_id: str,
        user_id: str,
        group_id: str,
        metadata: Mapping[str, Any] | None,
        connection=None,
    ) -> dict[str, Any]:
        payload = dict(metadata) if isinstance(metadata, Mapping) else {}
        if not any(key in payload for key in _JSON_KEYS):
            return payload
        current = str(payload.get("impression") or "").strip()
        history = payload.get("impression_history")
        if isinstance(history, list):
            for item in history:
                if not isinstance(item, Mapping):
                    continue
                text = str(item.get("text") or item.get("summary") or "").strip()
                if not text:
                    continue
                event = item.get("event") if isinstance(item.get("event"), Mapping) else {}
                kind = "affinity" if event.get("before_affinity") not in {None, ""} else "impression"
                self.add_event(
                    bot_id=bot_id,
                    user_id=user_id,
                    group_id=group_id,
                    kind=kind,
                    summary=str(item.get("summary") or text)[:240],
                    detail=str(item.get("detail") or text),
                    occurred_at=item.get("updated_at") or item.get("superseded_at") or item.get("at") or item.get("cleared_at"),
                    provenance={"source": "metadata.impression_history", "event": dict(event), "actor": item.get("actor")},
                    connection=connection,
                )
        if current:
            self.add_event(
                bot_id=bot_id,
                user_id=user_id,
                group_id=group_id,
                kind="impression",
                summary=current[:240],
                detail=current,
                occurred_at=payload.get("impression_updated_at"),
                provenance={"source": "metadata.impression", "event": payload.get("impression_event")},
                connection=connection,
            )
        traces = payload.get("unsettled_traces")
        energy = payload.get("unsettled_energy")
        if traces or energy not in {None, ""}:
            self.set_unsettled_state(
                bot_id=bot_id,
                user_id=user_id,
                group_id=group_id,
                energy=float(energy or 0.0),
                interaction_count=int(payload.get("unsettled_interaction_count") or 0),
                traces=list(traces or []),
                connection=connection,
            )
        ledger = payload.get("impression_ledger")
        if isinstance(ledger, list):
            for item in ledger:
                if not isinstance(item, Mapping):
                    continue
                event_type = str(item.get("event_type") or "").strip()
                if event_type in {"", "message_seen"}:
                    continue
                reason = str(item.get("reason") or "").strip()
                dimension = str(item.get("dimension") or "").strip()
                delta = item.get("delta")
                summary = f"{event_type} {dimension}{delta:+g}" if isinstance(delta, (int, float)) else event_type
                if reason:
                    summary = f"{summary}：{reason}"[:240]
                self.add_event(
                    bot_id=bot_id,
                    user_id=user_id,
                    group_id=group_id,
                    kind="affinity",
                    summary=summary,
                    detail=reason or summary,
                    occurred_at=item.get("at"),
                    provenance={"source": "metadata.impression_ledger", "ledger": dict(item)},
                    connection=connection,
                )
        for key in _JSON_KEYS:
            payload.pop(key, None)
        return payload

    def get_unsettled_state(
        self,
        *,
        bot_id: str,
        user_id: str,
        group_id: str = "",
        connection=None,
    ) -> dict[str, Any]:
        sql = """SELECT energy, interaction_count, traces, updated_at
                   FROM person_unsettled_state
                  WHERE bot_id=? AND user_id=? AND group_id=?"""
        reader = connection.execute if connection is not None else self.cm.execute_read
        try:
            row = reader(sql, (str(bot_id or ""), str(user_id or ""), str(group_id or ""))).fetchone()
        except Exception:
            return {"energy": 0.0, "interaction_count": 0, "traces": [], "updated_at": 0.0}
        if not row:
            return {"energy": 0.0, "interaction_count": 0, "traces": [], "updated_at": 0.0}
        traces: list[Any] = []
        raw = row[2]
        if isinstance(raw, str) and raw.strip():
            try:
                loaded = json.loads(raw)
                if isinstance(loaded, list):
                    traces = loaded
            except Exception:
                traces = []
        return {
            "energy": float(row[0] or 0.0),
            "interaction_count": int(row[1] or 0),
            "traces": traces,
            "updated_at": float(row[3] or 0.0),
        }

    def set_unsettled_state(
        self,
        *,
        bot_id: str,
        user_id: str,
        group_id: str = "",
        energy: float = 0.0,
        interaction_count: int = 0,
        traces: list[Any] | None = None,
        connection=None,
    ) -> None:
        sql = """INSERT INTO person_unsettled_state (
                    bot_id, user_id, group_id, energy, interaction_count, traces, updated_at
                 ) VALUES (?, ?, ?, ?, ?, ?, ?)
                 ON CONFLICT(bot_id, user_id, group_id) DO UPDATE SET
                    energy=excluded.energy,
                    interaction_count=excluded.interaction_count,
                    traces=excluded.traces,
                    updated_at=excluded.updated_at"""
        payload = (
            str(bot_id or ""),
            str(user_id or ""),
            str(group_id or ""),
            float(energy or 0.0),
            int(interaction_count or 0),
            _json_dumps(list(traces or [])),
            time.time(),
        )
        if connection is not None:
            connection.execute(sql, payload)
            commit = getattr(connection, "commit", None)
            if callable(commit):
                try:
                    commit()
                except Exception:
                    pass
            return
        self.cm.execute_write(sql, payload)
        self.cm.commit()

    def add_unsettled_trace(
        self,
        *,
        bot_id: str,
        user_id: str,
        group_id: str,
        text: str,
        impact: float,
        now: float | None = None,
        connection=None,
    ) -> dict[str, Any]:
        state = self.get_unsettled_state(bot_id=bot_id, user_id=user_id, group_id=group_id, connection=connection)
        stamp = float(now if now is not None else time.time())
        traces = list(state.get("traces") or [])
        traces.append({"text": text, "summary": text, "detail": text, "impact": impact, "ts": stamp})
        energy = round(float(state.get("energy") or 0.0) + float(impact or 0.0), 2)
        count = int(state.get("interaction_count") or 0) + 1
        self.set_unsettled_state(
            bot_id=bot_id,
            user_id=user_id,
            group_id=group_id,
            energy=energy,
            interaction_count=count,
            traces=traces,
            connection=connection,
        )
        return {"energy": energy, "interaction_count": count, "traces": traces}

    def clear_unsettled_state(
        self,
        *,
        bot_id: str,
        user_id: str,
        group_id: str,
        connection=None,
    ) -> None:
        self.set_unsettled_state(
            bot_id=bot_id,
            user_id=user_id,
            group_id=group_id,
            energy=0.0,
            interaction_count=0,
            traces=[],
            connection=connection,
        )


__all__ = ["PersonTimelineRepo"]
