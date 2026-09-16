"""Current-group QQ identity resolution for LLM tools.

This deliberately does **not** invent a Scope from bare group_id or QQ alone.
Callers must already hold a verified group RuntimeScope.  The resolver only maps
a user-provided name/QQ onto a subject that already appears inside that Scope.
"""

from __future__ import annotations

import json
import re
from typing import Any

try:
    from ..domain.scope import RuntimeScope
except ImportError:  # pragma: no cover - direct tools imports in isolated tests
    from domain.scope import RuntimeScope

_QQ_RE = re.compile(r"^\d{5,20}$")
_BILI_RE = re.compile(r"^(?:bili:)?\d{1,20}$", re.IGNORECASE)


def is_qq_id(value: str | None) -> bool:
    text = str(value or "").strip()
    return bool(_QQ_RE.fullmatch(text))


def is_bili_id(value: str | None) -> bool:
    text = str(value or "").strip()
    return bool(_BILI_RE.fullmatch(text))


def normalize_identity_id(value: str | None) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    if text.lower().startswith("bili:"):
        return f"bili:{text[5:].strip()}"
    return text


def _table_exists(conn: Any, name: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1",
        (name,),
    ).fetchone()
    return bool(row)


def _scope_subject_user_id(scope: RuntimeScope) -> str:
    if scope.session is None:
        return ""
    prefix = f"{scope.session.platform_id}:user:"
    principal = scope.subject_principal_id or ""
    return principal[len(prefix):] if principal.startswith(prefix) else ""


def _user_present_in_scope(conn: Any, scope: RuntimeScope, user_id: str) -> bool:
    """Accept a QQ only when it already appears in the current Bot/group Scope."""
    assert scope.session is not None
    group_id = scope.session.conversation_id
    bot_id = scope.bot_id
    session_id = scope.session.id

    if _table_exists(conn, "user_profiles"):
        row = conn.execute(
            """SELECT 1 FROM user_profiles
               WHERE user_id=? AND group_id=? AND bot_id=? LIMIT 1""",
            (user_id, group_id, bot_id),
        ).fetchone()
        if row:
            return True

    if _table_exists(conn, "memories"):
        row = conn.execute(
            """SELECT 1 FROM memories
               WHERE sender_id=?
                 AND bot_id=?
                 AND group_id=?
                 AND (
                        session_id=?
                     OR session_id LIKE ?
                     OR COALESCE(session_id, '') = ''
                 )
               LIMIT 1""",
            (user_id, bot_id, group_id, session_id, f"%:group:{group_id}"),
        ).fetchone()
        if row:
            return True
    return False


def resolve_user_id(db: Any, target: str | None, scope: RuntimeScope) -> str:
    """Resolve nickname/QQ to a current-Scope user_id (QQ).

    Order:
    1. current speaker
    2. direct QQ already present in this Bot/group
    3. person_registry display_name / aliases, verified in this Scope
    4. current-group memories.sender_name → sender_id
    5. user_profiles.nickname inside this Bot/group
    """
    text = str(target or "").strip()
    if not text or scope.session is None or db is None:
        return ""
    conn = getattr(db, "conn", None)
    if conn is None:
        return ""

    current = _scope_subject_user_id(scope)
    if text == current:
        return current

    group_id = scope.session.conversation_id
    bot_id = scope.bot_id
    session_id = scope.session.id

    if is_qq_id(text) and _user_present_in_scope(conn, scope, text):
        return text

    # person_registry authoritative names, but only if that QQ appears in this group.
    if _table_exists(conn, "person_registry"):
        rows = conn.execute(
            """SELECT qq_id, display_name, aliases, COALESCE(message_count, 0)
                 FROM person_registry
                WHERE display_name = ?
                   OR display_name LIKE ?
                ORDER BY COALESCE(message_count, 0) DESC
                LIMIT 20""",
            (text, f"%{text}%"),
        ).fetchall()
        ranked: list[tuple[int, str]] = []
        needle = text.casefold()
        for qq_id, display_name, aliases_json, message_count in rows:
            user_id = str(qq_id or "").strip()
            if not user_id or not _user_present_in_scope(conn, scope, user_id):
                continue
            display = str(display_name or "")
            score = 0
            if display == text:
                score = 300
            elif display.casefold() == needle:
                score = 280
            elif needle in display.casefold():
                score = 200
            aliases: list[Any] = []
            if aliases_json:
                try:
                    loaded = json.loads(aliases_json)
                    if isinstance(loaded, list):
                        aliases = loaded
                except Exception:
                    aliases = []
            for alias in aliases:
                alias_text = str(alias or "").strip()
                if not alias_text:
                    continue
                if alias_text == text or alias_text.casefold() == needle:
                    score = max(score, 260)
                elif needle in alias_text.casefold():
                    score = max(score, 180)
            if score > 0:
                ranked.append((score * 1_000_000 + int(message_count or 0), user_id))
        if ranked:
            ranked.sort(reverse=True)
            return ranked[0][1]

        # Exact alias scan for rows whose display_name did not LIKE-match.
        alias_rows = conn.execute(
            "SELECT qq_id, aliases, COALESCE(message_count, 0) FROM person_registry"
        ).fetchall()
        alias_hits: list[tuple[int, str]] = []
        for qq_id, aliases_json, message_count in alias_rows:
            user_id = str(qq_id or "").strip()
            if not user_id or not aliases_json:
                continue
            try:
                aliases = json.loads(aliases_json)
            except Exception:
                continue
            if not isinstance(aliases, list):
                continue
            matched = False
            score = 0
            for alias in aliases:
                alias_text = str(alias or "").strip()
                if not alias_text:
                    continue
                if alias_text == text or alias_text.casefold() == needle:
                    matched = True
                    score = 260
                    break
                if len(alias_text) >= 2 and (needle in alias_text.casefold() or alias_text.casefold() in needle):
                    matched = True
                    score = max(score, 180)
            if matched and _user_present_in_scope(conn, scope, user_id):
                alias_hits.append((score * 1_000_000 + int(message_count or 0), user_id))
        if alias_hits:
            alias_hits.sort(reverse=True)
            return alias_hits[0][1]

    # Current-group chat names are the strongest live signal when profiles are empty.
    if _table_exists(conn, "memories"):
        row = conn.execute(
            """SELECT sender_id, COUNT(*) AS cnt
                 FROM memories
                WHERE bot_id=?
                  AND group_id=?
                  AND (
                        session_id=?
                     OR session_id LIKE ?
                     OR COALESCE(session_id, '') = ''
                  )
                  AND sender_name = ?
                  AND COALESCE(sender_id, '') != ''
                GROUP BY sender_id
                ORDER BY cnt DESC
                LIMIT 1""",
            (bot_id, group_id, session_id, f"%:group:{group_id}", text),
        ).fetchone()
        if row and row[0]:
            return str(row[0])

        row = conn.execute(
            """SELECT sender_id, COUNT(*) AS cnt
                 FROM memories
                WHERE bot_id=?
                  AND group_id=?
                  AND (
                        session_id=?
                     OR session_id LIKE ?
                     OR COALESCE(session_id, '') = ''
                  )
                  AND sender_name LIKE ?
                  AND COALESCE(sender_id, '') != ''
                GROUP BY sender_id
                ORDER BY cnt DESC
                LIMIT 1""",
            (bot_id, group_id, session_id, f"%:group:{group_id}", f"%{text}%"),
        ).fetchone()
        if row and row[0]:
            return str(row[0])

    if _table_exists(conn, "user_profiles"):
        row = conn.execute(
            """SELECT user_id FROM user_profiles
               WHERE group_id=? AND bot_id=? AND user_id=?
               LIMIT 1""",
            (group_id, bot_id, text),
        ).fetchone()
        if row and row[0]:
            return str(row[0])

        row = conn.execute(
            """SELECT user_id FROM user_profiles
               WHERE group_id=? AND bot_id=? AND nickname = ?
               ORDER BY COALESCE(last_seen, 0) DESC LIMIT 1""",
            (group_id, bot_id, text),
        ).fetchone()
        if row and row[0]:
            return str(row[0])

        row = conn.execute(
            """SELECT user_id FROM user_profiles
               WHERE group_id=? AND bot_id=? AND nickname LIKE ?
               ORDER BY COALESCE(last_seen, 0) DESC LIMIT 1""",
            (group_id, bot_id, f"%{text}%"),
        ).fetchone()
        if row and row[0]:
            return str(row[0])

    return ""


def display_name_for_user(db: Any, user_id: str, scope: RuntimeScope) -> str:
    """Best-effort display name inside the current Scope, never crossing Bot/group."""
    user_id = str(user_id or "").strip()
    if not user_id or scope.session is None or db is None:
        return user_id
    conn = getattr(db, "conn", None)
    if conn is None:
        return user_id

    group_id = scope.session.conversation_id
    bot_id = scope.bot_id
    session_id = scope.session.id

    if _table_exists(conn, "person_registry"):
        row = conn.execute(
            "SELECT display_name FROM person_registry WHERE qq_id=? LIMIT 1",
            (user_id,),
        ).fetchone()
        if row and str(row[0] or "").strip():
            return str(row[0]).strip()

    if _table_exists(conn, "user_profiles"):
        row = conn.execute(
            """SELECT nickname FROM user_profiles
               WHERE user_id=? AND group_id=? AND bot_id=? LIMIT 1""",
            (user_id, group_id, bot_id),
        ).fetchone()
        if row and str(row[0] or "").strip():
            return str(row[0]).strip()

    if _table_exists(conn, "memories"):
        row = conn.execute(
            """SELECT sender_name, COUNT(*) AS cnt
                 FROM memories
                WHERE sender_id=?
                  AND bot_id=?
                  AND group_id=?
                  AND (
                        session_id=?
                     OR session_id LIKE ?
                     OR COALESCE(session_id, '') = ''
                  )
                  AND COALESCE(sender_name, '') != ''
                GROUP BY sender_name
                ORDER BY cnt DESC
                LIMIT 1""",
            (user_id, bot_id, group_id, session_id, f"%:group:{group_id}"),
        ).fetchone()
        if row and str(row[0] or "").strip():
            return str(row[0]).strip()

    return user_id


__all__ = [
    "display_name_for_user",
    "is_qq_id",
    "resolve_user_id",
]
