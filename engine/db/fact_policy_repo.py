"""Fact-only access to existing scoped tables. No schema changes or backfills."""
from __future__ import annotations

import json
import time
import uuid
from collections.abc import Mapping
from typing import Any

try:
    from ...domain.scope import RuntimeScope, validate_formal_command_scope
    from ...services.fact_policy import normalized_text, valid_subject, get_fact_policy
    from ...services.facts_conflict import FactConflictClassifier
except ImportError:  # pragma: no cover
    from domain.scope import RuntimeScope, validate_formal_command_scope
    from services.fact_policy import normalized_text, valid_subject, get_fact_policy
    from services.facts_conflict import FactConflictClassifier


def scope_params(scope: RuntimeScope) -> tuple[str, str, str]:
    decision = validate_formal_command_scope("fact.read", scope)
    if not decision.allowed:
        raise ValueError(decision.reason_code or "scope_required")
    return scope.bot_id, scope.session.id, scope.visibility


def json_object(value: Any) -> dict:
    if isinstance(value, Mapping):
        return dict(value)
    try:
        parsed = json.loads(value or "{}")
        return parsed if isinstance(parsed, dict) else {}
    except (TypeError, ValueError):
        return {}


def _one(cursor) -> dict | None:
    row = cursor.fetchone()
    return dict(zip((col[0] for col in cursor.description), row)) if row is not None else None


def fact_row(connection, scope, fact_id: int) -> dict | None:
    row = _one(connection.execute(
        "SELECT * FROM scoped_facts WHERE id=? AND bot_id=? AND session_id=? AND visibility=?",
        (fact_id, *scope_params(scope)),
    ))
    if row:
        row["provenance"] = json_object(row.get("provenance"))
    return row


def source_memory(connection, scope, memory_id: Any, quote: str) -> dict | None:
    """A linked ID is not proof: recheck scope, source health and the actual quote."""
    if isinstance(memory_id, bool) or not isinstance(memory_id, int) or memory_id <= 0 or not quote.strip():
        return None
    try:
        row = _one(connection.execute(
            """SELECT * FROM memories WHERE id=? AND bot_id=? AND session_id=? AND visibility=?
               AND resolution_state='resolved' AND COALESCE(quarantine,0)=0""",
            (memory_id, *scope_params(scope)),
        ))
    except Exception:
        return None
    if not row or normalized_text(quote) not in normalized_text(row.get("content")):
        return None
    if row.get("memory_type", "message") != "message" or row.get("source", "live") not in {"live", "message", "chat"}:
        return None
    if not str(row.get("sender_id") or "").strip() or row.get("sender_id") == "bot":
        return None
    return row


def resolve_subject(connection, scope, subject: str, source: Mapping | None, quote: str) -> dict | None:
    """Resolve accounts locally; named third parties may be explicitly quoted."""
    if not valid_subject(subject) or source is None:
        return None
    text = normalized_text(subject)
    prefix = f"{scope.session.platform_id}:user:"
    account = subject[len(prefix):] if subject.startswith(prefix) else subject
    sender_id = str(source.get("sender_id") or "")
    sender_name = str(source.get("sender_name") or "")
    if text in {normalized_text(sender_id), normalized_text(sender_name)} or account == sender_id:
        return {"subject": subject, "account_id": sender_id, "display_name": sender_name,
                "principal_id": f"{scope.session.platform_id}:user:{sender_id}"}
    # Do not reject real numeric IDs: resolve in exactly this bot and session.
    try:
        person = _one(connection.execute(
            """SELECT sender_id, sender_name FROM memories
               WHERE bot_id=? AND session_id=? AND visibility=? AND sender_id=?
                 AND resolution_state='resolved' AND COALESCE(quarantine,0)=0
               ORDER BY timestamp DESC LIMIT 1""", (*scope_params(scope), account),
        ))
    except Exception:
        person = None
    if person and (subject in quote or (person.get("sender_name") and person["sender_name"] in quote)):
        return {"subject": subject, "account_id": str(person["sender_id"]),
                "display_name": str(person.get("sender_name") or ""),
                "principal_id": f"{scope.session.platform_id}:user:{person['sender_id']}"}
    if not account.isdecimal() and not subject.startswith(prefix) and text in normalized_text(quote):
        return {"subject": subject, "display_name": subject}
    return None


def auto_review_reason(connection, scope, row: Mapping, policy=None) -> str | None:
    """Return why evidence is insufficient; run again inside the review transaction."""
    policy = policy or get_fact_policy()
    prov = json_object(row.get("provenance"))
    if not valid_subject(row.get("subject")):
        return "invalid_subject"
    if prov.get("needs_jargon_review"):
        return "jargon"
    quote = str(prov.get("source_quote") or "").strip()
    source = source_memory(connection, scope, row.get("source_memory_id"), quote)
    if not source:
        return "no_evidence"
    if not resolve_subject(connection, scope, str(row.get("subject") or ""), source, quote):
        return "unresolved_subject"
    # Quoted speculation and second-hand uncertainty remain human decisions.
    import re
    if re.search(r"听说|据说|可能|也许|猜测|好像|似乎|开玩笑|假如|假设|如果|\bmaybe\b|\brumou?r\b|\bperhaps\b|[?？]", quote, re.I):
        return "uncertain_evidence"
    if not normalized_text(row.get("object")) or normalized_text(row.get("object")) not in normalized_text(quote):
        return "unsupported_object"
    try:
        from ...services.identity_safety import is_identity_contamination
    except ImportError:  # pragma: no cover
        from services.identity_safety import is_identity_contamination
    if is_identity_contamination(" ".join(str(row.get(k) or "") for k in ("subject", "predicate", "object"))) or is_identity_contamination(quote):
        return "identity_contamination"
    temporal = policy.validity(predicate=str(row.get("predicate") or ""), object=str(row.get("object") or ""),
                               source_quote=quote, source_timestamp=source.get("timestamp"),
                               valid_from=row.get("valid_from"), valid_until=row.get("valid_until"))
    if temporal.needs_review or json_object(prov.get("temporal_policy")).get("needs_review"):
        return "unclear_time"
    return None


class FactPolicyRepo:
    def __init__(self, cm):
        self.cm = cm

    def get(self, scope, fact_id: int):
        return fact_row(self.cm.conn, scope, fact_id)

    def propose(self, scope, *, subject: str, predicate: str, object: str,
                source_memory_id=None, provenance=None, confidence=0.85,
                valid_from=None, valid_until=None, policy=None) -> int:
        """Legacy synchronous entry point for callers without a production writer.

        Production tools use the mutation gateway, which supplies its own writer
        transaction and outbox. Both paths preserve duplicate observations.
        """
        with self.cm.write_transaction() as conn:
            record = self.propose_in_transaction(
                conn, scope, subject=subject, predicate=predicate, object=object,
                source_memory_id=source_memory_id, provenance=provenance,
                confidence=confidence, valid_from=valid_from, valid_until=valid_until,
                policy=policy, now=time.time(),
            )
            return record["locator"]

    @staticmethod
    def propose_in_transaction(conn, scope, *, subject: str, predicate: str, object: str,
                               source_memory_id=None, provenance=None, confidence=0.85,
                               valid_from=None, valid_until=None, policy=None, now=None) -> dict:
        """Append a candidate/observation using the caller's existing transaction."""
        policy = policy or get_fact_policy()
        params = scope_params(scope)
        now = time.time() if now is None else now
        for name, value in (("subject", subject), ("predicate", predicate), ("object", object)):
            if not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"invalid_fact_{name}")
        if not valid_subject(subject):
            raise ValueError("invalid_fact_subject")
        provenance = dict(provenance or {})
        quote = str(provenance.get("source_quote") or "")
        source = source_memory(conn, scope, source_memory_id, quote)
        if source is None:
            source_memory_id = None
        validity = policy.validity(predicate=predicate, object=object, source_quote=quote,
                                   source_timestamp=(source or {}).get("timestamp"),
                                   valid_from=valid_from, valid_until=valid_until)
        valid_from, valid_until = validity.valid_from, validity.valid_until
        provenance.update({
            "source_timestamp": (source or {}).get("timestamp"),
            "resolved_subject": resolve_subject(conn, scope, subject, source, quote),
            "temporal_policy": {**validity.metadata(), "timezone": policy.timezone},
        })
        dump = lambda value: json.dumps(value, ensure_ascii=False, sort_keys=True)
        cursor = conn.execute(
            "SELECT * FROM scoped_facts WHERE bot_id=? AND session_id=? AND visibility=? AND subject=? AND predicate=?",
            (*params, subject, predicate),
        )
        columns = [col[0] for col in cursor.description]
        others = [dict(zip(columns, row)) for row in cursor.fetchall()]
        previous = next((row for row in others if row["object"] == object), None)
        candidate = {"subject": subject, "predicate": predicate, "object": object,
                     "valid_from": valid_from, "valid_until": valid_until, "provenance": provenance,
                     "source_memory_id": source_memory_id, "confidence": confidence}
        current = [row for row in others if row.get("valid_until") is None or row["valid_until"] > now]
        decision = FactConflictClassifier(policy=policy).classify(candidate, current)
        status = "conflict" if decision.relation == "conflicts" else "pending"
        if previous is None:
            cursor = conn.execute(
                """INSERT INTO scoped_facts (bot_id,session_id,visibility,subject,predicate,object,
                   confidence,status,source_memory_id,provenance,valid_from,valid_until,created_at,updated_at,revision)
                   VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,1)""",
                (*params, subject, predicate, object, confidence, status, source_memory_id,
                 dump(provenance), valid_from, valid_until, now, now),
            )
            fact_id = int(cursor.lastrowid)
        else:
            fact_id = int(previous["id"])
            # A repeated observation never changes a reviewed or pending row.
        existing = previous or next((row for row in others if row["id"] == decision.existing_id), {})
        evidence = {"source_quote": quote,
                    "context_evidence": provenance.get("context_evidence") or provenance.get("evidence", ""),
                    "source_memory_id": source_memory_id}
        cursor = conn.execute(
            """INSERT INTO scoped_fact_history
               (bot_id,session_id,visibility,candidate_fact_id,existing_fact_id,subject,predicate,object,
                relation,review_status,confidence,candidate_snapshot,existing_snapshot,evidence,source_tags,
                query_trace_id,source_memory_id,provenance,valid_from,valid_until,idempotency_key,observed_at)
               VALUES (?,?,?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?)""",
            (*params, fact_id, existing.get("id"), subject, predicate, object, decision.relation,
             confidence, dump(candidate), dump(existing), dump(evidence), dump(provenance.get("source_tags") or []),
             str(provenance.get("query_trace_id") or ""), source_memory_id, dump(provenance),
             valid_from, valid_until, uuid.uuid4().hex, now),
        )
        return {"kind": "fact", "locator": fact_id,
                "revision": int(previous["revision"]) if previous else 1,
                "status": previous["status"] if previous else status,
                "event_type": "scoped_fact.observed" if previous else "scoped_fact.proposed",
                "event_payload": {"observation_id": int(cursor.lastrowid), "evidence": evidence}}
