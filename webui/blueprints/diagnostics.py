"""Read-only Diagnostics API."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any, Mapping

try:
    from quart import Blueprint, jsonify
except Exception:  # pragma: no cover - helper tests without Quart
    class Blueprint:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs): pass
        def route(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

    def jsonify(value=None, **kwargs):  # type: ignore[no-redef]
        return value if value is not None else kwargs

try:
    from services.diagnostics import DiagnosticsService, IndexSource
    from services.memory_index_policy import MemoryIndexPolicy, memory_index_policy_from_settings
except ImportError:  # pragma: no cover - AstrBot package import path
    from ...services.diagnostics import DiagnosticsService, IndexSource
    from ...services.memory_index_policy import MemoryIndexPolicy, memory_index_policy_from_settings

try:
    from ..container import get_container
    from ..middleware.auth import require_auth
except Exception:  # pragma: no cover
    def get_container():  # type: ignore[no-redef]
        return None

    def require_auth(func):  # type: ignore[no-redef]
        return func


diagnostics_bp = Blueprint("diagnostics", __name__, url_prefix="/api/diagnostics")


def build_diagnostics_service(container: Any) -> DiagnosticsService:
    """Resolve only explicitly configured/live sources; never invent a database."""
    database_path = _path_value(getattr(getattr(container, "db", None), "db_path", None))
    memory_index = _index_source(
        getattr(container, "memory_index", None),
        "memory",
        memory_policy=_memory_index_policy(container),
    )
    tag_index = _index_source(getattr(container, "tag_index", None), "tags")
    book_lore_path = _book_lore_path(container, database_path)
    return DiagnosticsService(
        database_path=database_path,
        memory_index=memory_index,
        tag_index=tag_index,
        book_lore_path=book_lore_path,
    )


def _index_source(
    index: Any,
    fallback_kind: str,
    *,
    memory_policy: MemoryIndexPolicy | None = None,
) -> IndexSource:
    if index is None:
        return IndexSource(None, _canonical_index_kind(fallback_kind), memory_policy=memory_policy)
    path = _path_value(getattr(index, "index_path", None))
    kind = _canonical_index_kind(getattr(index, "kind", None) or fallback_kind)
    dimension = _optional_int(getattr(index, "dimension", None))
    try:
        runtime_count = _optional_int(getattr(index, "count", None))
    except Exception:
        runtime_count = None
    try:
        runtime_ids = tuple(int(value) for value in index.index.get_ids_list())
    except Exception:
        runtime_ids = None
    search = getattr(index, "search", None)
    if not callable(search):
        search = None
    return IndexSource(path, kind, dimension, runtime_count, runtime_ids, search, memory_policy)


def _memory_index_policy(container: Any) -> MemoryIndexPolicy:
    cfg = getattr(container, "plugin_config", {})
    section = cfg.get("Memory_Index_Settings", {}) if isinstance(cfg, Mapping) else {}
    return memory_index_policy_from_settings(section)


def _canonical_index_kind(value: Any) -> str:
    kind = str(value).strip()
    return "tag" if kind.lower() in {"tag", "tags"} else kind


def _book_lore_path(container: Any, database_path: str | None) -> str | None:
    for name in ("book_lore_db_path", "lore_db_path"):
        value = _path_value(getattr(container, name, None)) if container is not None else None
        if value:
            return value

    config = getattr(container, "plugin_config", None)
    if isinstance(config, Mapping):
        direct = _path_value(config.get("book_lore_db_path") or config.get("lore_db_path"))
        if direct:
            return direct
        section = config.get("BookLore_Settings")
        if isinstance(section, Mapping):
            nested = _path_value(section.get("book_lore_db_path") or section.get("lore_db_path"))
            if nested:
                return nested

    # The sibling is considered only when a real configured WaveMemory DB is present,
    # and only when the independent BookLore file already exists. Nothing is created.
    if database_path:
        sibling = Path(database_path).expanduser().resolve().with_name("book_lore.db")
        if sibling.is_file():
            return str(sibling)
    return None


def _path_value(value: Any) -> str | None:
    if value is None or not str(value).strip():
        return None
    return str(value)


def _optional_int(value: Any) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


@diagnostics_bp.route("/indexes", methods=["GET"])
@require_auth
async def get_index_diagnostics():
    """Return diagnostics only; this endpoint exposes no repair mutation."""
    container = get_container()
    service = build_diagnostics_service(container)
    payload = await asyncio.to_thread(service.collect)
    return jsonify(payload)


__all__ = ["build_diagnostics_service", "diagnostics_bp", "get_index_diagnostics"]
