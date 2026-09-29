"""角色经历的纯增量 schema，以及 v5 旧经历数据的一次性迁移映射。

表（全部 ``CREATE ... IF NOT EXISTS``，重复执行无副作用）：

- ``character_experiences``：按 Bot 存的一条条第一人称经历（正文、角色自身时间、所属阶段与阶段内序号、
  剧情阶段名、情绪、在场人物、可信级别、出处、状态、版本号、创建方式与时间，可选向量）；
  同一个 Bot 内按规范化正文去重；
- ``character_timeline_stages``：每个 Bot 的时间线阶段（有序），带说明、关键事件、可选章节范围；
- ``character_experience_scene_log``：提取 / 展开任务处理过的场景（含模型判定为空的），续跑时跳过，
  重新提交同一份原文也不会重复调用模型；
- ``character_experience_migrations``：一次性数据迁移的完成标记与报告；
- ``character_experiences_fts``：中文两字切词的全文索引（与 ``fts_memories_cjk`` 同一套切法），召回用。

``LEGACY_CHARACTER_EXPERIENCES`` 是迁移数据，不是运行时逻辑：v5 用仓库外的一次性脚本把白真真的
1416 条第一人称经历写进了记忆表（``source=bzz_experience``，bot_id / session_id / visibility 全为空）
和书设库（``category=白真真经历``）。迁移把它们导入 Bot ``baizz`` 的角色经历，之后运行时只认 Bot 配置
（``character_experience.legacy_sources`` / ``lore_categories``），代码里不再写这些值。
"""

from __future__ import annotations

import re
import sqlite3
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

from ..connection import ConnectionManager

try:
    from ....domain.character_experience import natural_key, normalize_content, stage_title_from_key
except ImportError:  # 仓库测试把插件根目录当顶层包
    from domain.character_experience import natural_key, normalize_content, stage_title_from_key

FTS_TABLE = "character_experiences_fts"
# SQLite 3.43+ 用 contentless_delete 表（按 rowid 删除、不存切好的词）；更老的版本存一份 tokens。
_CONTENTLESS = sqlite3.sqlite_version_info >= (3, 43, 0)
_FTS_OPTIONS = "tokens, content='', contentless_delete=1, tokenize='unicode61'" if _CONTENTLESS else "tokens, tokenize='unicode61'"

_SCHEMA = f"""
CREATE TABLE IF NOT EXISTS character_experiences (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    bot_id TEXT NOT NULL,
    content TEXT NOT NULL,
    content_hash TEXT NOT NULL,
    story_time TEXT NOT NULL DEFAULT '',
    stage_key TEXT NOT NULL DEFAULT '',
    seq REAL NOT NULL DEFAULT 0,
    plot_stage TEXT NOT NULL DEFAULT '',
    emotion TEXT NOT NULL DEFAULT '',
    characters TEXT NOT NULL DEFAULT '[]',
    canon_level TEXT NOT NULL DEFAULT 'canon',
    source_kind TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT NOT NULL DEFAULT '{{}}',
    status TEXT NOT NULL DEFAULT 'draft',
    version INTEGER NOT NULL DEFAULT 1,
    vector BLOB,
    job_id TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    UNIQUE (bot_id, content_hash)
);

CREATE INDEX IF NOT EXISTS idx_character_experiences_bot_status
    ON character_experiences (bot_id, status, stage_key, seq);

CREATE INDEX IF NOT EXISTS idx_character_experiences_bot_updated
    ON character_experiences (bot_id, updated_at);

CREATE TABLE IF NOT EXISTS character_timeline_stages (
    bot_id TEXT NOT NULL,
    stage_key TEXT NOT NULL,
    ordinal INTEGER NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    story_time TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    key_events TEXT NOT NULL DEFAULT '[]',
    chapter_from INTEGER,
    chapter_to INTEGER,
    status TEXT NOT NULL DEFAULT 'confirmed',
    source_kind TEXT NOT NULL DEFAULT 'manual',
    source_ref TEXT NOT NULL DEFAULT '{{}}',
    version INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    PRIMARY KEY (bot_id, stage_key)
);

CREATE TABLE IF NOT EXISTS character_experience_scene_log (
    bot_id TEXT NOT NULL,
    scene_key TEXT NOT NULL,
    kind TEXT NOT NULL DEFAULT 'novel',
    job_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL,
    memories INTEGER NOT NULL DEFAULT 0,
    attempts INTEGER NOT NULL DEFAULT 1,
    detail TEXT NOT NULL DEFAULT '{{}}',
    processed_at REAL NOT NULL,
    PRIMARY KEY (bot_id, scene_key)
);

CREATE TABLE IF NOT EXISTS character_experience_migrations (
    key TEXT PRIMARY KEY,
    status TEXT NOT NULL,
    report TEXT NOT NULL DEFAULT '{{}}',
    applied_at REAL NOT NULL
);

CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} USING fts5({_FTS_OPTIONS})
"""


def ensure_character_experience_schema(cm: ConnectionManager) -> None:
    if not isinstance(cm, ConnectionManager):
        raise TypeError("cm must be a ConnectionManager")
    statements = tuple(s.strip() for s in _SCHEMA.split(";") if s.strip())
    with cm.migration_transaction() as tx:
        for statement in statements:
            tx.execute(statement)


# ─── v5 旧经历的迁移数据 ──────────────────────────────────────────────

# 记忆表里的旧来源值 → 归属的 Bot 与书设库里对应的个人经历类别。
LEGACY_CHARACTER_EXPERIENCES: dict[str, dict[str, str]] = {
    "bzz_experience": {"bot_id": "baizz", "lore_category": "白真真经历"},
}

_NOTE_ID_RE = re.compile(r"_ch(\d+)_s(\d+)_(\d+)$")
_NOTE_TITLE_RE = re.compile(r"^ch(\d+)\s+(.*?)\s*\[(.*?)\]\s*$")


def migration_key(source: str, bot_id: str) -> str:
    return f"legacy:{source}->{bot_id}"


def load_legacy_book_notes(lore_db_path: str | Path | None, category: str) -> list[dict[str, Any]]:
    """只读打开书设库，取某个类别的全部笔记（不带向量）；库或表不存在时返回空。"""
    raw = str(lore_db_path or "").strip()
    if not raw or not category:
        return []
    path = Path(raw)
    if not path.is_file():
        return []
    try:
        conn = sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)
    except sqlite3.Error:
        return []
    try:
        conn.execute("PRAGMA query_only = ON")
        columns = {row[1] for row in conn.execute("PRAGMA table_info(book_notes)").fetchall()}
        if not {"id", "category", "content"} <= columns:
            return []
        wanted = [name for name in ("id", "book_name", "arc", "category", "title", "content", "source_file") if name in columns]
        rows = conn.execute(
            f"SELECT {', '.join(wanted)} FROM book_notes WHERE category=?",
            (category,),
        ).fetchall()
        return [dict(zip(wanted, row)) for row in rows]
    except sqlite3.Error:
        return []
    finally:
        conn.close()


def parse_legacy_note(note: Mapping[str, Any]) -> dict[str, Any]:
    """旧导入脚本的笔记格式：id=``bzz_ch0002_s00_0``，title=``ch2 剧情阶段(前10字) [第1年12月初]``。"""
    out: dict[str, Any] = {"book_note_id": str(note.get("id") or ""), "arc": str(note.get("arc") or "").strip()}
    match = _NOTE_ID_RE.search(out["book_note_id"])
    if match:
        out["chapter"], out["scene_idx"], out["line"] = (int(match.group(1)), int(match.group(2)), int(match.group(3)))
    title = str(note.get("title") or "").strip()
    title_match = _NOTE_TITLE_RE.match(title)
    if title_match:
        out.setdefault("chapter", int(title_match.group(1)))
        out["plot_stage"] = title_match.group(2).strip()
        out["story_time"] = title_match.group(3).strip()
    if note.get("book_name"):
        out["corpus"] = str(note.get("book_name"))
    return out


def _seq(chapter: Any, scene_idx: Any, line: Any, fallback: float) -> float:
    if isinstance(chapter, int):
        return float(chapter * 1_000_000 + (scene_idx or 0) * 1000 + (line or 0) % 1000)
    return float(fallback)


def build_legacy_items(
    rows: Sequence[Mapping[str, Any]],
    notes: Sequence[Mapping[str, Any]],
    *,
    source: str,
    lore_category: str,
    dimension: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], dict[str, int]]:
    """记忆表旧行 + 书设笔记 → (经历条目, 阶段定义, 统计)。

    按正文对上书设笔记的，带上阶段（arc）、剧情阶段名、角色时间与章节/场景号；对不上的保留正文，
    阶段取旧行的 group_id（旧脚本写的是 arc），时间留空。向量长度与当前维度一致时一并带过来。
    """
    by_content: dict[str, Mapping[str, Any]] = {}
    for note in notes:
        key = normalize_content(note.get("content"))
        if key and key not in by_content:
            by_content[key] = note
    items: list[dict[str, Any]] = []
    matched = 0
    with_vector = 0
    for row in rows:
        content = str(row.get("content") or "").strip()
        if not content:
            continue
        note = by_content.get(normalize_content(content))
        parsed = parse_legacy_note(note) if note is not None else {}
        matched += 1 if note is not None else 0
        arc = parsed.get("arc") or str(row.get("group_id") or "").strip()
        if arc == source:
            arc = ""
        vector = row.get("vector")
        if not (isinstance(vector, (bytes, bytearray, memoryview)) and len(bytes(vector)) == int(dimension) * 4):
            vector = None
        else:
            with_vector += 1
        source_ref: dict[str, Any] = {
            "legacy_memory_id": int(row.get("id") or 0),
            "legacy_source": source,
        }
        for key in ("book_note_id", "chapter", "scene_idx", "corpus"):
            if parsed.get(key) not in (None, ""):
                source_ref[key] = parsed[key]
        if note is not None:
            source_ref["lore_category"] = lore_category
        items.append({
            "content": content,
            "story_time": parsed.get("story_time", ""),
            "stage_key": arc,
            "seq": _seq(parsed.get("chapter"), parsed.get("scene_idx"), parsed.get("line"), float(row.get("id") or 0)),
            "plot_stage": parsed.get("plot_stage", ""),
            "canon_level": "canon",
            "source_kind": "legacy_migration",
            "source_ref": source_ref,
            "status": "active",
            "vector": bytes(vector) if vector is not None else None,
        })
    stages = _stage_defs(items)
    stats = {"rows": len(rows), "items": len(items), "matched_notes": matched, "with_vector": with_vector, "stages": len(stages)}
    return items, stages, stats


def _stage_defs(items: Iterable[Mapping[str, Any]]) -> list[dict[str, Any]]:
    times: dict[str, list[tuple[float, str]]] = {}
    for item in items:
        key = str(item.get("stage_key") or "")
        if not key:
            continue
        bucket = times.setdefault(key, [])
        if item.get("story_time"):
            bucket.append((float(item.get("seq") or 0), str(item["story_time"])))
    defs: list[dict[str, Any]] = []
    for ordinal, key in enumerate(sorted(times, key=natural_key), 1):
        spans = [text for _, text in sorted(times[key])]
        story_time = ""
        if spans:
            story_time = spans[0] if spans[0] == spans[-1] else f"{spans[0]}～{spans[-1]}"
        defs.append({
            "stage_key": key,
            "ordinal": ordinal,
            "title": stage_title_from_key(key),
            "story_time": story_time,
            "status": "confirmed",
            "source_kind": "legacy_migration",
        })
    return defs


def legacy_memory_rows(cm: ConnectionManager, source: str) -> list[dict[str, Any]]:
    """记忆表里某个旧来源、完全没有归属（bot_id / session_id / visibility 全空）的行。"""
    columns = {row[1] for row in cm.execute_read("PRAGMA table_info(memories)").fetchall()}
    if not {"id", "content", "source"} <= columns:
        return []
    unowned = " AND ".join(
        f"COALESCE({name}, '')=''" for name in ("bot_id", "session_id", "visibility") if name in columns
    ) or "1=1"
    vector = "vector" if "vector" in columns else "NULL"
    group = "group_id" if "group_id" in columns else "''"
    rows = cm.execute_read(
        f"""SELECT id, content, {group}, {vector} FROM memories
             WHERE source=? AND {unowned} AND COALESCE(content, '') != ''
             ORDER BY id""",
        (source,),
    ).fetchall()
    return [{"id": row[0], "content": row[1], "group_id": row[2], "vector": row[3]} for row in rows]


def migrate_legacy_character_experiences(
    repo: Any,
    *,
    profile_get: Callable[[str], Any],
    profile_save: Callable[[Any, int], Any],
    book_notes: Callable[[str], Sequence[Mapping[str, Any]]] = lambda category: [],
    dimension: int = 1024,
    dry_run: bool = False,
    mapping: Mapping[str, Mapping[str, str]] | None = None,
) -> list[dict[str, Any]]:
    """把记忆表里的旧经历导入对应 Bot 的角色经历；返回每个旧来源的报告。

    - 幂等：完成后写标记，之后直接跳过；条目按 (bot_id, 正文指纹) 去重，重复导入不会多出行；
    - 旧行不删除、不修改；由 Bot 配置 ``legacy_sources`` 让普通记忆召回不再返回它们；
    - 目标 Bot 还不存在时跳过且不写标记（之后建了 Bot 会在下次启动或管理台里再迁）；
    - 没有旧数据的库写一个「无需迁移」标记，之后启动不再扫描记忆表；
    - 迁移完为该 Bot 开启角色经历、当前时间点设为最后一个阶段（等价于原来：她能想起全部）。
    """
    reports: list[dict[str, Any]] = []
    for source, target in (mapping or LEGACY_CHARACTER_EXPERIENCES).items():
        bot_id = str(target.get("bot_id") or "")
        category = str(target.get("lore_category") or "")
        key = migration_key(source, bot_id)
        report: dict[str, Any] = {"key": key, "source": source, "bot_id": bot_id, "lore_category": category, "dry_run": dry_run}
        state = repo.get_migration(key)
        previous = dict((state or {}).get("report") or {})
        if state is not None and previous.get("profile_updated", True):
            reports.append({**report, "status": "already_applied", "previous": state})
            continue
        profile = profile_get(bot_id)
        if profile is None:
            reports.append({**report, "status": "target_bot_missing"})
            continue
        if state is not None:
            # 数据已导入、上次没来得及写 Bot 配置：只补配置。
            report.update({k: v for k, v in previous.items() if k not in report})
            if dry_run:
                reports.append({**report, "status": "profile_update_pending"})
                continue
            report["profile_updated"] = _update_profile(
                profile_get, profile_save, bot_id,
                enable=state.get("status") == "applied",
                last_stage=str(previous.get("last_stage") or ""),
                sources=[source] if state.get("status") == "applied" else [],
                categories=[category] if category and (state.get("status") == "applied" or previous.get("lore_notes")) else [],
            )
            repo.put_migration(key, str(state.get("status")), report)
            reports.append({**report, "status": str(state.get("status"))})
            continue
        rows = legacy_memory_rows(repo.cm, source)
        notes = list(book_notes(category) or []) if category else []
        report["lore_notes"] = len(notes)
        if not rows:
            report["rows"] = 0
            if dry_run:
                reports.append({**report, "status": "nothing_to_migrate"})
                continue
            # 没有旧经历行，但书设库里有这个角色的个人经历笔记：只登记类别，让它们继续不当世界观书设用。
            updated = True
            if notes:
                repo.put_migration(key, "nothing_to_migrate", {**report, "profile_updated": False})
                updated = _update_profile(profile_get, profile_save, bot_id, enable=False, last_stage="", sources=[], categories=[category])
            report["profile_updated"] = updated
            repo.put_migration(key, "nothing_to_migrate", report)
            reports.append({**report, "status": "nothing_to_migrate"})
            continue
        items, stages, stats = build_legacy_items(rows, notes, source=source, lore_category=category, dimension=dimension)
        last_stage = stages[-1]["stage_key"] if stages else ""
        report.update(stats)
        report["last_stage"] = last_stage
        report["stage_keys"] = [stage["stage_key"] for stage in stages]
        if dry_run:
            report["would_import"] = len(items)
            reports.append({**report, "status": "preview"})
            continue
        result = repo.import_legacy(bot_id, items, stages, created_by="migration")
        report.update({"imported": len(result["inserted"]), "duplicates": len(result["duplicates"])})
        # 先落数据与标记（profile_updated=False），再改 Bot 配置；配置没写成下次只补配置。
        repo.put_migration(key, "applied", {**report, "profile_updated": False})
        report["profile_updated"] = _update_profile(
            profile_get, profile_save, bot_id, enable=True, last_stage=last_stage,
            sources=[source], categories=[category] if category else [],
        )
        repo.put_migration(key, "applied", report)
        reports.append({**report, "status": "applied"})
    return reports


def _update_profile(
    profile_get: Callable[[str], Any],
    profile_save: Callable[[Any, int], Any],
    bot_id: str,
    *,
    enable: bool,
    last_stage: str,
    sources: Sequence[str],
    categories: Sequence[str],
) -> bool:
    """为 Bot 开启角色经历、登记接管的旧来源与个人经历类别；版本冲突时重读重试一次。"""
    for _attempt in range(2):
        profile = profile_get(bot_id)
        if profile is None:
            return False
        data = profile.to_dict()
        settings = dict(data.get("character_experience") or {})
        if enable:
            settings["enabled"] = True
            if last_stage:
                settings["current_stage"] = last_stage
        settings["legacy_sources"] = _merge(settings.get("legacy_sources"), sources)
        settings["lore_categories"] = _merge(settings.get("lore_categories"), categories)
        data["character_experience"] = settings
        try:
            profile_save(type(profile).from_dict(data), int(profile.version))
            return True
        except Exception as exc:  # 版本冲突：重读后再试一次
            if getattr(exc, "code", "") != "version_conflict":
                raise
    return False


def _merge(existing: Any, extra: Iterable[str]) -> list[str]:
    out = [str(item) for item in (existing or []) if str(item or "").strip()]
    for item in extra:
        if item and item not in out:
            out.append(item)
    return out


__all__ = [
    "FTS_TABLE",
    "LEGACY_CHARACTER_EXPERIENCES",
    "build_legacy_items",
    "ensure_character_experience_schema",
    "legacy_memory_rows",
    "load_legacy_book_notes",
    "migrate_legacy_character_experiences",
    "migration_key",
    "parse_legacy_note",
]
