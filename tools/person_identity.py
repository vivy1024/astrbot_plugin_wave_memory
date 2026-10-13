"""Current-group QQ identity resolution for LLM tools.

This deliberately does **not** invent a Scope from bare group_id or QQ alone.
Callers must already hold a verified group RuntimeScope.  The resolver only maps
a user-provided name/QQ onto a subject that already appears inside that Scope.
"""

from __future__ import annotations

import json
import re
import time
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


def _user_present_in_scope(conn: Any, scope: RuntimeScope, user_id: str, *, current_group_only: bool = False) -> bool:
    """Check if a QQ appears in the scope."""
    if not scope.bot_id:
        return True
    bot_id = scope.bot_id
    if scope.session and scope.visibility == "group":
        group_id = scope.session.conversation_id
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
                   WHERE sender_id=? AND bot_id=? AND group_id=? LIMIT 1""",
                (user_id, bot_id, group_id),
            ).fetchone()
            if row:
                return True
    if current_group_only and scope.session and scope.visibility == "group":
        return False

    # 回退到当前 Bot 全局检查
    if _table_exists(conn, "user_profiles"):
        row = conn.execute(
            """SELECT 1 FROM user_profiles WHERE user_id=? AND bot_id=? LIMIT 1""",
            (user_id, bot_id),
        ).fetchone()
        if row:
            return True
    if _table_exists(conn, "memories"):
        row = conn.execute(
            """SELECT 1 FROM memories WHERE sender_id=? AND bot_id=? LIMIT 1""",
            (user_id, bot_id),
        ).fetchone()
        if row:
            return True
    return True


_RESOLVE_CACHE: dict[tuple, tuple[float, str]] = {}


def _nickname_cache_ttl() -> float:
    try:
        from ..services.hot_config import HotConfig, hot_value
    except ImportError:  # pragma: no cover - isolated tools tests
        try:
            from services.hot_config import HotConfig, hot_value
        except ImportError:
            return 0.0
    if HotConfig._instance is None:  # 没有插件运行时（单测、脚本）就不缓存
        return 0.0
    return float(hot_value("people.nickname_cache_seconds", 300))


def clear_resolve_cache() -> None:
    _RESOLVE_CACHE.clear()


def resolve_user_id(db: Any, target: str | None, scope: RuntimeScope, *, current_group_only: bool = False) -> str:
    """按昵称/QQ 解析 user_id；昵称结果（含未命中）按热参数缓存。

    按昵称兜底要按 sender_name 扫这个 Bot 的全部行（线上约 360ms，sender_name 没有索引），
    而事后记账、好感/事实/关切工具会对同一个名字反复解析。QQ 号与当前发言人不走缓存。
    """
    text = str(target or "").strip()
    if not text or db is None or is_qq_id(text):
        return _resolve_user_id_uncached(db, target, scope, current_group_only=current_group_only)
    current = _scope_subject_user_id(scope) if scope.session else ""
    if text == current and current:
        return current
    ttl = _nickname_cache_ttl()
    if ttl <= 0:
        return _resolve_user_id_uncached(db, target, scope, current_group_only=current_group_only)
    key = (
        id(db), text, scope.bot_id, scope.visibility,
        scope.session.id if scope.session else "", bool(current_group_only),
    )
    now = time.monotonic()
    hit = _RESOLVE_CACHE.get(key)
    if hit is not None and now - hit[0] < ttl:
        return hit[1]
    value = _resolve_user_id_uncached(db, target, scope, current_group_only=current_group_only)
    if len(_RESOLVE_CACHE) > 4096:
        _RESOLVE_CACHE.clear()
    _RESOLVE_CACHE[key] = (now, value)
    return value


def _resolve_user_id_uncached(db: Any, target: str | None, scope: RuntimeScope, *, current_group_only: bool = False) -> str:
    """Resolve nickname/QQ to a user_id (QQ).

    Order:
    1. current speaker
    2. direct QQ number (validated against group when current_group_only=True)
    3. current-group person_registry / memories / profiles
    4. fallback to Bot-wide person_registry / memories / profiles (for private chats or cross-group mentions)
    """
    text = str(target or "").strip()
    if not text or db is None:
        return ""
    conn = getattr(db, "conn", None)
    if conn is None:
        return text if is_qq_id(text) else ""

    current = _scope_subject_user_id(scope) if scope.session else ""
    if text == current and current:
        return current

    # Direct QQ numbers can be resolved directly on read/search paths
    if is_qq_id(text):
        if current_group_only and not _user_present_in_scope(conn, scope, text, current_group_only=True):
            return ""
        return text

    group_id = scope.session.conversation_id if scope.session and scope.visibility == "group" else ""
    bot_id = scope.bot_id
    session_id = scope.session.id if scope.session else ""

    # 1. person_registry authoritative names (first in current group, then Bot-wide)
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
            if not user_id:
                continue
            if current_group_only and not _user_present_in_scope(conn, scope, user_id, current_group_only=True):
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
            if current_group_only and not _user_present_in_scope(conn, scope, user_id, current_group_only=True):
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
            if matched:
                alias_hits.append((score * 1_000_000 + int(message_count or 0), user_id))
        if alias_hits:
            alias_hits.sort(reverse=True)
            return alias_hits[0][1]

    # 2. Memories sender_name (first current group, then Bot-wide)
    if _table_exists(conn, "memories"):
        if group_id:
            row = conn.execute(
                """SELECT sender_id, COUNT(*) AS cnt
                     FROM memories
                    WHERE bot_id=? AND group_id=? AND sender_name = ?
                      AND COALESCE(sender_id, '') != ''
                    GROUP BY sender_id ORDER BY cnt DESC LIMIT 1""",
                (bot_id, group_id, text),
            ).fetchone()
            if row and row[0]:
                return str(row[0])

        if not current_group_only:
            row = conn.execute(
                """SELECT sender_id, COUNT(*) AS cnt
                     FROM memories
                    WHERE bot_id=? AND sender_name = ?
                      AND COALESCE(sender_id, '') != ''
                    GROUP BY sender_id ORDER BY cnt DESC LIMIT 1""",
                (bot_id, text),
            ).fetchone()
            if row and row[0]:
                return str(row[0])

            row = conn.execute(
                """SELECT sender_id, COUNT(*) AS cnt
                     FROM memories
                    WHERE bot_id=? AND sender_name LIKE ?
                      AND COALESCE(sender_id, '') != ''
                    GROUP BY sender_id ORDER BY cnt DESC LIMIT 1""",
                (bot_id, f"%{text}%"),
            ).fetchone()
            if row and row[0]:
                return str(row[0])

    # 3. user_profiles (first current group, then Bot-wide)
    if _table_exists(conn, "user_profiles"):
        if group_id:
            row = conn.execute(
                """SELECT user_id FROM user_profiles
                   WHERE group_id=? AND bot_id=? AND nickname = ?
                   ORDER BY COALESCE(last_seen, 0) DESC LIMIT 1""",
                (group_id, bot_id, text),
            ).fetchone()
            if row and row[0]:
                return str(row[0])

        if not current_group_only:
            row = conn.execute(
                """SELECT user_id FROM user_profiles
                   WHERE bot_id=? AND nickname = ?
                   ORDER BY COALESCE(last_seen, 0) DESC LIMIT 1""",
                (bot_id, text),
            ).fetchone()
            if row and row[0]:
                return str(row[0])

            row = conn.execute(
                """SELECT user_id FROM user_profiles
                   WHERE bot_id=? AND nickname LIKE ?
                   ORDER BY COALESCE(last_seen, 0) DESC LIMIT 1""",
                (bot_id, f"%{text}%"),
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
