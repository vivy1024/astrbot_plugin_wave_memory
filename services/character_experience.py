"""角色经历服务：召回、工作台管理、JSONL 导入导出、三种来源的后台任务与旧数据迁移。

角色经历是 Bot 本人进群之前「过去的人生」（书中角色的原著经历、原创角色按设定编写的经历），
与群聊里长出来的记忆（``memories`` 表）分开存放、分开召回：

- 只属于一个 Bot（``bot_id``），只有在 Bot 资料里启用了角色经历的 Bot 才会想起，其他 Bot 完全看不到；
- 只召回「当前时间点」（时间线阶段）及之前阶段的经历，避免剧透和自相矛盾；
- 不进入群友事实、关系、信念：那些学习链路只读记忆表，角色经历不在其中。

后台任务（从原著提取、按设定起草时间线、展开成经历、补算向量）走 durable job 框架，由单独的执行器跑
（一次提取可能要调用模型几百次，不能堵住索引重建等维护任务）；每处理完一个场景就落库并记录，
中断、重启或取消后「续跑」只处理还没处理过的场景。
"""

from __future__ import annotations

import asyncio
import json
import logging
import threading
import time
import uuid
from collections.abc import Callable, Iterable, Mapping, Sequence
from pathlib import Path
from typing import Any

import numpy as np

try:
    from ..domain.character_experience import (
        CANON_LEVELS,
        SOURCE_KINDS,
        STATUSES,
        CharacterExperienceSettings,
        eligible_stage_keys,
        natural_key,
        stage_title_from_key,
    )
    from ..engine.db import fts_cjk
    from ..engine.db.character_experience_repo import CharacterExperienceError
    from ..engine.db.job_repo import JobLeaseLostError
    from ..engine.db.migrations.character_experience import (
        load_legacy_book_notes,
        migrate_legacy_character_experiences,
    )
    from . import character_experience_extract as extract
    from .book_lore_workbench import list_text_sources, read_source_text, resolve_source_roots, safe_source_path
except ImportError:  # 仓库测试把插件根目录当顶层包
    from domain.character_experience import (
        CANON_LEVELS,
        SOURCE_KINDS,
        STATUSES,
        CharacterExperienceSettings,
        eligible_stage_keys,
        natural_key,
        stage_title_from_key,
    )
    from engine.db import fts_cjk
    from engine.db.character_experience_repo import CharacterExperienceError
    from engine.db.job_repo import JobLeaseLostError
    from engine.db.migrations.character_experience import (
        load_legacy_book_notes,
        migrate_legacy_character_experiences,
    )
    from services import character_experience_extract as extract
    from services.book_lore_workbench import list_text_sources, read_source_text, resolve_source_roots, safe_source_path

logger = logging.getLogger("astrbot")

EXTRACT_KIND = "character_experience.extract.v1"
TIMELINE_KIND = "character_experience.timeline.v1"
EXPAND_KIND = "character_experience.expand.v1"
EMBED_KIND = "character_experience.embed.v1"
JOB_KINDS = (EXTRACT_KIND, TIMELINE_KIND, EXPAND_KIND, EMBED_KIND)
JOB_SLOT = "main"

MAX_JSONL_BYTES = 20 * 1024 * 1024
MAX_JSONL_LINES = 20000
MAX_EXTRACT_CALLS = 5000
EMBED_BATCH = 16
# 关键词命中的经历在向量相似度上加的分；没有向量可比时给的基础分。
KEYWORD_BONUS = 0.15
KEYWORD_BASE_SCORE = 0.30
# 连续这么多个场景调用失败（渠道挂了、余额不足…）就停下，等管理员续跑，不白白耗尽整个任务。
MAX_CONSECUTIVE_FAILURES = 5


def _now() -> float:
    return time.time()


def _job_scope(bot_id: str) -> dict[str, str]:
    return {"kind": "character_experience", "bot_id": str(bot_id)}


def _vector_bytes(vector: Any, dimension: int) -> bytes | None:
    if vector is None:
        return None
    arr = np.asarray(vector, dtype=np.float32).reshape(-1)
    if arr.size != int(dimension):
        return None
    norm = float(np.linalg.norm(arr))
    if norm <= 0:
        return None
    return (arr / norm).astype(np.float32).tobytes()


def _preview(text: Any, limit: int = 160) -> str:
    compact = str(text or "").replace("\n", " ").strip()
    return compact if len(compact) <= limit else compact[: limit - 1] + "…"


def _keywords(message: str) -> list[str]:
    try:
        from .injection.channels.fts5 import _keywords as fts_keywords
    except ImportError:  # 仓库测试把插件根目录当顶层包
        from services.injection.channels.fts5 import _keywords as fts_keywords
    return fts_keywords(message or "")


def keyword_match_expr(message: str) -> str:
    """消息 → 角色经历全文索引的查询式（与 fts5 通道同一套切词：长词拆成两字片段）。"""
    expanded: list[str] = []
    for word in _keywords(message):
        pieces = fts_cjk.tokenize(word)
        expanded.extend(pieces if len(pieces) > 3 else [word])
    return fts_cjk.match_expr(expanded)


class CharacterExperienceService:
    def __init__(
        self,
        *,
        repo: Any,
        registry: Any = None,
        embedding_service: Any = None,
        llm_for: Callable[[str], Any] | None = None,
        llm_status: Callable[[str], Mapping[str, Any]] | None = None,
        jobs: Callable[[], Any] | Any = None,
        source_roots: Sequence[str] = (),
        lore_db_path: str = "",
        dimension: int = 1024,
    ) -> None:
        self.repo = repo
        self.registry = registry
        self.embedding = embedding_service
        self._llm_for = llm_for
        self._llm_status = llm_status
        self._jobs = jobs
        self.source_roots = resolve_source_roots(source_roots)
        self.lore_db_path = str(lore_db_path or "")
        self.dimension = int(dimension)
        self._vector_cache: dict[str, tuple[tuple[int, float, int], np.ndarray, list[str], np.ndarray]] = {}
        self._cache_lock = threading.Lock()
        # 原文切场景的结果按 (路径, 修改时间, 大小, 参数) 缓存：预览之后马上开始任务不必再切一遍 13 MB 的原文
        self._scene_cache: dict[tuple[Any, ...], tuple[list[extract.Scene], dict[str, int]]] = {}

    # ------------------------------------------------------------------ 依赖

    def jobs(self) -> Any:
        return self._jobs() if callable(self._jobs) else self._jobs

    def profile(self, bot_id: str, *, enabled_only: bool = False) -> Any:
        registry = self.registry
        if registry is None:
            return None
        try:
            if enabled_only:
                return registry.get(bot_id)
            getter = getattr(registry, "get_any", None) or registry.get
            return getter(bot_id)
        except Exception:
            return None

    def settings_for(self, bot_id: str, *, enabled_only: bool = False) -> CharacterExperienceSettings:
        profile = self.profile(bot_id, enabled_only=enabled_only)
        settings = getattr(profile, "character_experience", None)
        return settings if isinstance(settings, CharacterExperienceSettings) else CharacterExperienceSettings()

    def llm(self, bot_id: str) -> Any:
        if self._llm_for is None:
            return None
        try:
            return self._llm_for(bot_id)
        except Exception:
            return None

    def model_status(self, bot_id: str) -> dict[str, Any]:
        if self._llm_status is not None:
            try:
                return dict(self._llm_status(bot_id))
            except Exception as exc:  # 只影响展示
                return {"available": self.llm(bot_id) is not None, "error": str(exc)}
        return {"available": self.llm(bot_id) is not None}

    def _require_profile(self, bot_id: str) -> Any:
        profile = self.profile(bot_id)
        if profile is None:
            raise CharacterExperienceError("bot_not_found", f"没有 Bot {bot_id!r}")
        return profile

    # ------------------------------------------------------------------ 召回

    def _vector_matrix(self, bot_id: str) -> tuple[np.ndarray, list[str], np.ndarray]:
        fingerprint = self.repo.fingerprint(bot_id)
        with self._cache_lock:
            cached = self._vector_cache.get(bot_id)
            if cached is not None and cached[0] == fingerprint:
                return cached[1], cached[2], cached[3]
        ids: list[int] = []
        stages: list[str] = []
        vectors: list[np.ndarray] = []
        for experience_id, stage_key, blob in self.repo.active_vectors(bot_id):
            arr = np.frombuffer(blob, dtype=np.float32)
            if arr.size != self.dimension:
                continue
            ids.append(experience_id)
            stages.append(stage_key)
            vectors.append(arr)
        matrix = np.vstack(vectors).astype(np.float32) if vectors else np.zeros((0, self.dimension), dtype=np.float32)
        id_array = np.asarray(ids, dtype=np.int64)
        with self._cache_lock:
            self._vector_cache[bot_id] = (fingerprint, id_array, stages, matrix)
        return id_array, stages, matrix

    def _recall_sync(
        self,
        bot_id: str,
        message: str,
        settings: CharacterExperienceSettings,
        query_vector: Any,
    ) -> dict[str, Any]:
        stages = self.repo.list_stages(bot_id)
        eligible, warning = eligible_stage_keys(stages, settings.current_stage)

        def allowed(stage_key: str) -> bool:
            return not stage_key or eligible is None or stage_key in eligible

        sims: dict[int, float] = {}
        if query_vector is not None:
            query = np.asarray(query_vector, dtype=np.float32).reshape(-1)
            norm = float(np.linalg.norm(query))
            if query.size == self.dimension and norm > 0:
                ids, stage_keys, matrix = self._vector_matrix(bot_id)
                if len(ids):
                    mask = np.asarray([allowed(key) for key in stage_keys], dtype=bool)
                    if mask.any():
                        scores = matrix[mask] @ (query / norm)
                        sims = {int(i): float(s) for i, s in zip(ids[mask], scores)}
        keyword_ids = [
            experience_id for experience_id, stage_key in self.repo.fts_search(bot_id, keyword_match_expr(message), limit=30)
            if allowed(stage_key)
        ]
        keyword_set = set(keyword_ids)
        candidates: dict[int, float] = {}
        filtered: list[dict[str, Any]] = []
        for experience_id, sim in sims.items():
            if sim >= settings.min_score or (experience_id in keyword_set and sim >= settings.min_score * 0.8):
                candidates[experience_id] = sim + (KEYWORD_BONUS if experience_id in keyword_set else 0.0)
        for rank, experience_id in enumerate(keyword_ids):
            if experience_id in candidates or experience_id in sims:
                continue  # 有向量却没过相似度下限的关键词命中不取
            candidates[experience_id] = KEYWORD_BASE_SCORE + KEYWORD_BONUS - rank * 0.001
        ranked = sorted(candidates.items(), key=lambda kv: kv[1], reverse=True)
        chosen = ranked[: settings.top_k]
        for experience_id, score in ranked[settings.top_k: settings.top_k + 5]:
            filtered.append({"id": experience_id, "score": round(score, 4), "filter_reason": "top_k"})
        rows = {row["id"]: row for row in self.repo.get_many(bot_id, [experience_id for experience_id, _ in chosen])}
        stage_by_key = {stage["stage_key"]: stage for stage in stages}
        items: list[dict[str, Any]] = []
        for experience_id, score in chosen:
            row = rows.get(experience_id)
            if row is None or row.get("status") != "active" or not allowed(str(row.get("stage_key") or "")):
                continue
            stage = stage_by_key.get(str(row.get("stage_key") or ""))
            items.append({
                **row,
                "score": round(float(score), 4),
                "similarity": round(sims[experience_id], 4) if experience_id in sims else None,
                "keyword_hit": experience_id in keyword_set,
                "stage_ordinal": stage.get("ordinal") if stage else None,
                "stage_title": stage.get("title") if stage else "",
                "stage_story_time": stage.get("story_time") if stage else "",
            })
        # 按她自己的时间顺序讲（阶段 → 阶段内序号），读起来像回忆而不是检索结果。
        items.sort(key=lambda item: (
            item["stage_ordinal"] if item["stage_ordinal"] is not None else -1,
            float(item.get("seq") or 0),
            int(item["id"]),
        ))
        return {
            "items": items,
            "filtered": filtered,
            "warning": warning,
            "current_stage": settings.current_stage,
            "eligible_stages": None if eligible is None else sorted(eligible, key=natural_key),
            "vector_candidates": len(sims),
            "keyword_hits": len(keyword_ids),
        }

    async def recall(
        self,
        bot_id: str,
        message: str,
        *,
        settings: CharacterExperienceSettings | None = None,
        query_vector: Any = None,
    ) -> dict[str, Any]:
        """召回当前时间点及之前的启用经历；按角色时间顺序返回。"""
        settings = settings or self.settings_for(bot_id, enabled_only=True)
        return await asyncio.to_thread(self._recall_sync, str(bot_id), str(message or ""), settings, query_vector)

    # ------------------------------------------------------------------ 概览

    def list_jobs(self, bot_id: str, *, limit: int = 10) -> list[dict[str, Any]]:
        try:
            rows = self.repo.cm.execute_read(
                """SELECT r.run_id, q.request_id, q.kind, q.payload_json, r.status, r.progress_json, r.result_json,
                          r.error_code, r.error_message, r.created_at, r.updated_at, r.cursor_generation
                     FROM background_job_runs r JOIN job_requests q ON q.request_id = r.request_id
                    WHERE q.kind LIKE 'character_experience.%' AND q.scope_json = ?
                    ORDER BY r.created_at DESC LIMIT ?""",
                (json.dumps(_job_scope(bot_id), ensure_ascii=False, sort_keys=True, separators=(",", ":")), max(1, int(limit))),
            ).fetchall()
        except Exception:
            return []
        jobs: list[dict[str, Any]] = []
        for row in rows:
            payload = json.loads(row[3] or "{}")
            payload.pop("scene_keys", None)
            payload.pop("event_keys", None)
            if "setting" in payload:
                payload["setting"] = _preview(payload["setting"], 80)
            jobs.append({
                "run_id": row[0], "request_id": row[1], "kind": row[2], "payload": payload, "status": row[4],
                "progress": json.loads(row[5] or "{}"), "result": json.loads(row[6]) if row[6] else None,
                "error_code": row[7], "error_message": row[8], "created_at": row[9], "updated_at": row[10],
                "generation": row[11],
                "resumable": row[4] in {"failed", "cancelled"} and row[2] != EMBED_KIND,
            })
        return jobs

    def sources(self) -> list[dict[str, Any]]:
        out = []
        for path in list_text_sources(self.source_roots):
            try:
                stat = path.stat()
            except OSError:
                continue
            out.append({"path": str(path), "name": path.name, "size": stat.st_size, "modified_at": stat.st_mtime})
        return out

    def overview(self, bot_id: str) -> dict[str, Any]:
        profile = self._require_profile(bot_id)
        settings = profile.character_experience
        stages = self.repo.list_stages(bot_id)
        counts = self.repo.counts(bot_id)
        eligible, warning = eligible_stage_keys(stages, settings.current_stage)
        by_stage = counts["by_stage"]
        recallable = sum(
            stats.get("active", 0) for key, stats in by_stage.items()
            if not key or eligible is None or key in eligible
        )
        known = {stage["stage_key"] for stage in stages}
        warnings: list[dict[str, str]] = []
        if warning == "current_stage_missing":
            warnings.append({"code": warning, "message": f"当前时间点「{settings.current_stage}」不在时间线里：只有不分阶段的经历能被想起"})
        unassigned_active = by_stage.get("", {}).get("active", 0)
        if unassigned_active and settings.current_stage:
            warnings.append({"code": "unassigned_active", "message": f"{unassigned_active} 条启用的经历没分阶段，不受当前时间点限制，任何时候都能想起"})
        orphan = sorted(key for key in by_stage if key and key not in known)
        if orphan:
            warnings.append({"code": "unknown_stages", "message": f"这些阶段不在时间线里：{'、'.join(orphan[:5])}；设了当前时间点时它们不会被想起"})
        if counts["active_missing_vectors"]:
            warnings.append({"code": "missing_vectors", "message": f"{counts['active_missing_vectors']} 条启用的经历还没有向量，只能靠关键词想起；点「补算向量」"})
        if not settings.enabled and counts["by_status"].get("active"):
            warnings.append({"code": "feature_disabled", "message": "这个 Bot 还没开启角色经历：启用的经历暂时不会被想起"})
        stage_items = []
        for stage in stages:
            stats = by_stage.get(stage["stage_key"], {s: 0 for s in STATUSES})
            stage_items.append({
                **stage,
                "counts": stats,
                "is_current": stage["stage_key"] == settings.current_stage,
                "recallable": eligible is None or stage["stage_key"] in eligible,
            })
        return {
            "bot": {
                "db_id": profile.db_id,
                "name": profile.name,
                "aliases": list(profile.aliases),
                "enabled": bool(profile.enabled),
                "version": int(profile.version),
                "persona_text": self.persona_text(profile),
            },
            "settings": {
                "enabled": settings.enabled,
                "current_stage": settings.current_stage,
                "top_k": settings.top_k,
                "max_chars": settings.max_chars,
                "min_score": settings.min_score,
                "legacy_sources": list(settings.legacy_sources),
                "lore_categories": list(settings.lore_categories),
            },
            "stages": stage_items,
            "unassigned": by_stage.get("", {s: 0 for s in STATUSES}),
            "counts": counts,
            "recallable_active": recallable,
            "warnings": warnings,
            "model": self.model_status(bot_id),
            "embedding_available": self.embedding is not None,
            "jobs": self.list_jobs(bot_id),
            "scene_stats": self.repo.scene_stats(bot_id),
            "migrations": [item for item in self.repo.list_migrations() if item.get("report", {}).get("bot_id") == bot_id],
            "sources": self.sources(),
        }

    @staticmethod
    def persona_text(profile: Any) -> str:
        """Bot 资料里能带进「按设定编写」的人设：常驻书设标题、书设条目与 MetaThinking 提示词。"""
        persona = getattr(profile, "persona", None)
        parts: list[str] = []
        title = str(getattr(persona, "lore_title", "") or "").strip()
        if title:
            parts.append(title)
        lines = [str(line).strip() for line in (getattr(persona, "lore_lines", None) or []) if str(line).strip()]
        if lines:
            parts.append("\n".join(lines))
        meta = str(getattr(profile, "meta_prompt", "") or "").strip()
        if meta:
            parts.append(meta)
        return "\n\n".join(parts)

    # ------------------------------------------------------------------ 设置（写在 Bot 资料里）

    def update_settings(self, bot_id: str, patch: Mapping[str, Any], *, expected_version: int, changed_by: str = "webui") -> Any:
        """改 Bot 资料的 ``character_experience`` 段（版本对不上抛 BotProfileConflict → 409）。"""
        profile = self._require_profile(bot_id)
        allowed = {"enabled", "current_stage", "top_k", "max_chars", "min_score", "legacy_sources", "lore_categories"}
        unknown = set(patch) - allowed
        if unknown:
            raise CharacterExperienceError("unknown_fields", f"不能修改这些字段：{', '.join(sorted(unknown))}")
        if "current_stage" in patch:
            key = str(patch.get("current_stage") or "").strip()
            if key and self.repo.get_stage(bot_id, key) is None:
                raise CharacterExperienceError("stage_not_found", f"时间线里没有阶段 {key}")
        data = profile.to_dict()
        data["character_experience"] = {**dict(data.get("character_experience") or {}), **dict(patch)}
        updated = type(profile).from_dict(data)
        return self.registry.save(updated, expected_version=int(expected_version), changed_by=changed_by, reason="角色经历设置")

    # ------------------------------------------------------------------ 经历

    def create(self, bot_id: str, fields: Mapping[str, Any], *, created_by: str = "webui") -> dict[str, Any]:
        self._require_profile(bot_id)
        item = self.repo.clean_item({**dict(fields), "source_kind": fields.get("source_kind") or "manual"})
        if item["stage_key"] and self.repo.get_stage(bot_id, item["stage_key"]) is None:
            raise CharacterExperienceError("stage_not_found", f"时间线里没有阶段 {item['stage_key']}")
        result = self.repo.add_many(bot_id, [item], created_by=created_by)
        if not result["inserted"]:
            raise CharacterExperienceError("duplicate_content", f"已有同样内容的经历 #{(result['duplicates'] or result['updated'])[0]}")
        created = self.repo.get(bot_id, result["inserted"][0])
        assert created is not None
        return created

    # ------------------------------------------------------------------ JSONL 导入导出

    def import_jsonl(
        self,
        bot_id: str,
        text: str,
        *,
        status: str = "draft",
        canon_level: str = "canon",
        on_duplicate: str = "skip",
        created_by: str = "webui",
    ) -> dict[str, Any]:
        """兼容 v5 ``bzz_memories_raw.jsonl``（memory / emotion / characters / arc / stage / approx_time /
        chapter / title / scene_idx）与本功能导出的扩展字段；缺的阶段按 arc 自动建。"""
        self._require_profile(bot_id)
        raw = str(text or "")
        if len(raw.encode("utf-8")) > MAX_JSONL_BYTES:
            raise CharacterExperienceError("jsonl_too_large", "一次最多导入 20 MB")
        if status not in {"draft", "active", "as_file"}:
            raise CharacterExperienceError("invalid_status", "导入状态只能是 draft / active / as_file")
        if canon_level not in CANON_LEVELS:
            raise CharacterExperienceError("invalid_canon_level", f"可信级别只能是 {', '.join(CANON_LEVELS)}")
        if on_duplicate not in {"skip", "fill", "overwrite"}:
            raise CharacterExperienceError("invalid_on_duplicate", "重复处理只能是 skip / fill / overwrite")
        lines = [line for line in raw.splitlines() if line.strip()]
        if len(lines) > MAX_JSONL_LINES:
            raise CharacterExperienceError("jsonl_too_many_lines", f"一次最多导入 {MAX_JSONL_LINES} 行")
        items: list[dict[str, Any]] = []
        errors: list[dict[str, Any]] = []
        stage_meta: dict[str, dict[str, Any]] = {}
        per_scene: dict[tuple[Any, Any], int] = {}
        for number, line in enumerate(lines, 1):
            try:
                record = json.loads(line)
                if not isinstance(record, dict):
                    raise ValueError("每行必须是一个 JSON 对象")
                item = self._jsonl_item(record, number, status=status, canon_level=canon_level, per_scene=per_scene)
                cleaned = self.repo.clean_item(item)
            except (ValueError, CharacterExperienceError) as exc:
                if len(errors) < 50:
                    errors.append({"line": number, "message": str(exc)[:200]})
                continue
            items.append(cleaned)
            key = cleaned["stage_key"]
            if key and key not in stage_meta:
                stage_meta[key] = {
                    "stage_key": key,
                    "title": str(record.get("stage_title") or "") or stage_title_from_key(key),
                    "ordinal": record.get("stage_ordinal"),
                    "status": "confirmed",
                    "source_kind": "jsonl_import",
                }
        stage_defs = sorted(
            stage_meta.values(),
            key=lambda s: (s["ordinal"] is None, s["ordinal"] if isinstance(s["ordinal"], (int, float)) else 0, natural_key(s["stage_key"])),
        )
        for ordinal, stage in enumerate(stage_defs, 1):
            stage["ordinal"] = ordinal
        created_stages = self.repo.ensure_stages(bot_id, stage_defs)
        result = self.repo.add_many(bot_id, items, created_by=created_by, on_duplicate=on_duplicate)
        return {
            "lines": len(lines),
            "valid": len(items),
            "inserted": len(result["inserted"]),
            "updated": len(result["updated"]),
            "duplicates": len(result["duplicates"]),
            "errors": errors,
            "error_count": len(lines) - len(items),
            "stages_created": created_stages,
        }

    @staticmethod
    def _jsonl_item(record: Mapping[str, Any], number: int, *, status: str, canon_level: str, per_scene: dict) -> dict[str, Any]:
        content = str(record.get("memory") or record.get("content") or "").strip()
        if not content:
            raise ValueError("缺少 memory（正文）")
        chapter = record.get("chapter")
        scene_idx = record.get("scene_idx")
        chapter_int = int(chapter) if isinstance(chapter, (int, float)) or str(chapter or "").isdigit() else None
        scene_int = int(scene_idx) if isinstance(scene_idx, (int, float)) or str(scene_idx or "").isdigit() else None
        if record.get("seq") not in (None, ""):
            seq = float(record.get("seq"))
        elif chapter_int is not None:
            slot = (chapter_int, scene_int)
            per_scene[slot] = per_scene.get(slot, -1) + 1
            seq = float(chapter_int * 1_000_000 + (scene_int or 0) * 1000 + per_scene[slot] % 1000)
        else:
            seq = float(number)
        source_ref = dict(record.get("source_ref") or {}) if isinstance(record.get("source_ref"), Mapping) else {}
        if chapter_int is not None:
            source_ref.setdefault("chapter", chapter_int)
        if record.get("title"):
            source_ref.setdefault("chapter_title", str(record.get("title")))
        if scene_int is not None:
            source_ref.setdefault("scene_idx", scene_int)
        source_ref.setdefault("import_line", number)
        line_status = str(record.get("status") or "")
        line_canon = str(record.get("canon_level") or "")
        line_kind = str(record.get("source_kind") or "")
        return {
            "content": content,
            "emotion": record.get("emotion"),
            "characters": record.get("characters"),
            "stage_key": record.get("stage_key") or record.get("arc") or "",
            "plot_stage": record.get("plot_stage") or record.get("stage") or "",
            "story_time": record.get("story_time") or record.get("approx_time") or "",
            "seq": seq,
            "canon_level": line_canon if line_canon in CANON_LEVELS else canon_level,
            "status": (line_status if line_status in STATUSES else "draft") if status == "as_file" else status,
            "source_kind": line_kind if line_kind in SOURCE_KINDS else "jsonl_import",
            "source_ref": source_ref,
        }

    def export_jsonl(self, bot_id: str, *, statuses: Iterable[str] | None = None) -> str:
        """导出为 JSONL：v5 格式字段（memory / arc / stage / approx_time / chapter / title / scene_idx）加扩展字段，可原样再导入。"""
        self._require_profile(bot_id)
        stages = {stage["stage_key"]: stage for stage in self.repo.list_stages(bot_id)}
        lines: list[str] = []
        for row in self.repo.export_rows(bot_id, statuses=[s for s in (statuses or []) if s in STATUSES] or None):
            stage = stages.get(row["stage_key"]) or {}
            ref = row.get("source_ref") or {}
            record = {
                "memory": row["content"],
                "emotion": row["emotion"],
                "characters": row["characters"],
                "arc": row["stage_key"],
                "stage": row["plot_stage"],
                "approx_time": row["story_time"],
                "chapter": ref.get("chapter"),
                "title": ref.get("chapter_title", ""),
                "scene_idx": ref.get("scene_idx", ref.get("scene_index")),
                "stage_key": row["stage_key"],
                "stage_title": stage.get("title", ""),
                "stage_ordinal": stage.get("ordinal"),
                "plot_stage": row["plot_stage"],
                "story_time": row["story_time"],
                "seq": row["seq"],
                "canon_level": row["canon_level"],
                "status": row["status"],
                "source_kind": row["source_kind"],
                "source_ref": ref,
                "id": row["id"],
            }
            lines.append(json.dumps(record, ensure_ascii=False))
        return "\n".join(lines) + ("\n" if lines else "")

    # ------------------------------------------------------------------ 从原著提取：计划与确认

    def _scenes(self, path: Path, params: Mapping[str, Any]) -> tuple[list[extract.Scene], dict[str, int]]:
        stat = path.stat()
        key = (
            str(path), stat.st_mtime, stat.st_size, params["name"], tuple(params["aliases"]),
            params.get("chapter_from"), params.get("chapter_to"), params["min_scene_chars"], params["require_full_name"],
        )
        cached = self._scene_cache.get(key)
        if cached is not None:
            return cached
        text = read_source_text(path)
        result = extract.plan_scenes(
            text,
            name=params["name"],
            aliases=params["aliases"],
            chapter_from=params.get("chapter_from"),
            chapter_to=params.get("chapter_to"),
            min_chars=params["min_scene_chars"],
            require_full_name=params["require_full_name"],
        )
        self._scene_cache = {key: result}  # 只留最近一份，避免长期占着几十 MB
        return result

    @staticmethod
    def _extract_params(body: Mapping[str, Any]) -> dict[str, Any]:
        name = str(body.get("name") or "").strip()
        if not name:
            raise CharacterExperienceError("name_required", "请填写角色名")
        aliases = body.get("aliases")
        if isinstance(aliases, str):
            aliases = [part.strip() for part in aliases.replace("，", ",").replace("、", ",").split(",")]
        aliases = [str(a).strip() for a in (aliases or []) if str(a or "").strip() and str(a).strip() != name][:20]

        def _opt_int(value: Any) -> int | None:
            if value in (None, ""):
                return None
            try:
                return max(0, int(value))
            except (TypeError, ValueError) as exc:
                raise CharacterExperienceError("invalid_chapter_range", "章节范围必须是整数") from exc

        chapter_from, chapter_to = _opt_int(body.get("chapter_from")), _opt_int(body.get("chapter_to"))
        if chapter_from is not None and chapter_to is not None and chapter_from > chapter_to:
            raise CharacterExperienceError("invalid_chapter_range", "起始章不能大于结束章")
        try:
            min_chars = int(body.get("min_scene_chars") or extract.MIN_SCENE_CHARS)
            max_chars = int(body.get("max_scene_chars") or extract.MAX_SCENE_CHARS)
            concurrency = int(body.get("concurrency") or 2)
        except (TypeError, ValueError) as exc:
            raise CharacterExperienceError("invalid_params", "场景字数与并发数必须是整数") from exc
        return {
            "path": str(body.get("path") or ""),
            "name": name,
            "aliases": aliases,
            "description": str(body.get("description") or "").strip()[:2000],
            "chapter_from": chapter_from,
            "chapter_to": chapter_to,
            "min_scene_chars": max(30, min(min_chars, 2000)),
            "max_scene_chars": max(300, min(max_chars, 6000)),
            "require_full_name": body.get("require_full_name") is not False,
            "redo": bool(body.get("redo")),
            "concurrency": max(1, min(concurrency, 4)),
        }

    def _stage_for_chapter(self, stages: Sequence[Mapping[str, Any]], chapter: int) -> str:
        for stage in stages:
            low, high = stage.get("chapter_from"), stage.get("chapter_to")
            if low is None and high is None:
                continue
            if (low is None or chapter >= int(low)) and (high is None or chapter <= int(high)):
                return str(stage["stage_key"])
        return ""

    def extraction_plan(self, bot_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        """预览：切出多少场景、已处理过多少、这次预计调用模型多少次、各阶段分到多少。"""
        self._require_profile(bot_id)
        params = self._extract_params(body)
        path = safe_source_path(params["path"], self.source_roots)
        scenes, stats = self._scenes(path, params)
        processed = self.repo.processed_scene_keys(bot_id, [scene.key for scene in scenes])
        todo = scenes if params["redo"] else [scene for scene in scenes if scene.key not in processed]
        stages = self.repo.list_stages(bot_id)
        by_stage: dict[str, int] = {}
        for scene in todo:
            key = self._stage_for_chapter(stages, scene.chapter)
            by_stage[key] = by_stage.get(key, 0) + 1
        return {
            "source": {"path": str(path), "name": path.name},
            "params": {k: v for k, v in params.items() if k != "path"},
            **stats,
            "already_processed": len(processed),
            "calls": len(todo),
            "by_stage": by_stage,
            "unassigned_scenes": by_stage.get("", 0),
            "sample": [
                {"chapter": s.chapter, "title": s.title, "chars": len(s.text), "preview": s.preview()}
                for s in todo[:5]
            ],
            "model": self.model_status(bot_id),
            "scene_keys": [scene.key for scene in todo],
        }

    async def _enqueue(self, bot_id: str, kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        jobs = self.jobs()
        if jobs is None:
            raise CharacterExperienceError("durable_jobs_unavailable", "后台任务服务未就绪")
        envelope = await jobs.enqueue(
            idempotency_key=f"character_experience:{kind}:{bot_id}:{uuid.uuid4().hex}",
            kind=kind,
            scope=_job_scope(bot_id),
            payload=dict(payload),
            schedule_slot=JOB_SLOT,
            cursor={"phase": "queued"},
        )
        return envelope.to_dict() if hasattr(envelope, "to_dict") else dict(envelope)

    @staticmethod
    def _require_confirmation(plan: Mapping[str, Any], body: Mapping[str, Any]) -> None:
        """调用模型的任务必须带上确认：``confirm=true`` 且 ``expected_calls`` 等于这次预计的调用次数。"""
        try:
            expected = int(body.get("expected_calls"))
        except (TypeError, ValueError):
            expected = None
        if body.get("confirm") is not True or expected != int(plan["calls"]):
            error = CharacterExperienceError(
                "confirmation_required",
                f"这次预计调用模型 {plan['calls']} 次，请确认后再开始（带上 confirm=true 与 expected_calls={plan['calls']}）",
            )
            error.plan = {k: v for k, v in plan.items() if k not in {"scene_keys", "event_keys", "setting"}}  # type: ignore[attr-defined]
            raise error

    async def start_extraction(self, bot_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        plan = await asyncio.to_thread(self.extraction_plan, bot_id, body)
        self._require_confirmation(plan, body)
        if plan["calls"] <= 0:
            raise CharacterExperienceError("nothing_to_extract", "没有需要处理的场景（都处理过了；要重新提取请勾选「重新提取」）")
        if plan["calls"] > MAX_EXTRACT_CALLS:
            raise CharacterExperienceError("too_many_calls", f"一次最多 {MAX_EXTRACT_CALLS} 次调用，请缩小章节范围分批提取")
        if self.llm(bot_id) is None:
            raise CharacterExperienceError("model_unavailable", "没有可用的模型：在配置中心「模型分工」里给「角色经历」指定模型，或配置 MetaThinking 默认模型")
        params = self._extract_params(body)
        payload = {**params, "bot_id": bot_id, "expected_calls": plan["calls"], "scene_keys": plan["scene_keys"], "requested_by": "webui"}
        return {"job": await self._enqueue(bot_id, EXTRACT_KIND, payload), "calls": plan["calls"]}

    # ------------------------------------------------------------------ 按设定编写：起草时间线与展开

    def timeline_plan(self, bot_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        profile = self._require_profile(bot_id)
        setting = str(body.get("setting") or "").strip() or self.persona_text(profile)
        paragraphs = extract.split_setting_paragraphs(setting)
        if not paragraphs:
            raise CharacterExperienceError("setting_required", "请填写设定文本（可以从 Bot 资料的人设带入）")
        return {"calls": 1, "paragraphs": len(paragraphs), "setting_chars": len(setting), "setting": setting, "model": self.model_status(bot_id)}

    async def start_timeline_draft(self, bot_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        plan = self.timeline_plan(bot_id, body)
        self._require_confirmation(plan, body)
        if self.llm(bot_id) is None:
            raise CharacterExperienceError("model_unavailable", "没有可用的模型：在配置中心「模型分工」里给「角色经历」指定模型")
        profile = self._require_profile(bot_id)
        payload = {
            "bot_id": bot_id,
            "name": str(body.get("name") or "").strip() or profile.name,
            "setting": plan["setting"],
            "replace_drafts": bool(body.get("replace_drafts")),
            "requested_by": "webui",
        }
        return {"job": await self._enqueue(bot_id, TIMELINE_KIND, payload), "calls": 1}

    def expansion_plan(self, bot_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        self._require_profile(bot_id)
        wanted = [str(key) for key in (body.get("stage_keys") or []) if str(key or "").strip()]
        stages = [stage for stage in self.repo.list_stages(bot_id) if not wanted or stage["stage_key"] in wanted]
        unconfirmed = [stage["stage_key"] for stage in stages if stage["status"] != "confirmed"]
        stages = [stage for stage in stages if stage["status"] == "confirmed"]
        events = [(stage, index, event) for stage in stages for index, event in enumerate(stage["key_events"])]
        keys = [extract.event_key(stage["stage_key"], event["text"]) for stage, _, event in events]
        processed = self.repo.processed_scene_keys(bot_id, keys)
        redo = bool(body.get("redo"))
        todo = [key for key in keys if redo or key not in processed]
        return {
            "stages": [stage["stage_key"] for stage in stages],
            "unconfirmed_stages": unconfirmed,
            "events": len(events),
            "already_processed": len(processed),
            "calls": len(todo),
            "model": self.model_status(bot_id),
            "event_keys": todo,
        }

    async def start_expansion(self, bot_id: str, body: Mapping[str, Any]) -> dict[str, Any]:
        plan = self.expansion_plan(bot_id, body)
        self._require_confirmation(plan, body)
        if plan["calls"] <= 0:
            raise CharacterExperienceError("nothing_to_expand", "没有需要展开的关键事件（阶段要先确认，且要有关键事件）")
        if self.llm(bot_id) is None:
            raise CharacterExperienceError("model_unavailable", "没有可用的模型：在配置中心「模型分工」里给「角色经历」指定模型")
        profile = self._require_profile(bot_id)
        setting = str(body.get("setting") or "").strip() or self.persona_text(profile)
        payload = {
            "bot_id": bot_id,
            "name": str(body.get("name") or "").strip() or profile.name,
            "setting": setting[: extract.SETTING_MAX_CHARS],
            "stage_keys": plan["stages"],
            "redo": bool(body.get("redo")),
            "expected_calls": plan["calls"],
            "event_keys": plan["event_keys"],
            "requested_by": "webui",
        }
        return {"job": await self._enqueue(bot_id, EXPAND_KIND, payload), "calls": plan["calls"]}

    async def start_embedding(self, bot_id: str) -> dict[str, Any] | None:
        """启用状态、还没有向量的经历排一个补算向量的任务（不调用大模型）。"""
        if self.embedding is None:
            raise CharacterExperienceError("embedding_unavailable", "向量服务未就绪")
        if not self.repo.missing_vectors(bot_id, limit=1):
            return None
        return await self._enqueue(bot_id, EMBED_KIND, {"bot_id": bot_id, "requested_by": "webui"})

    async def resume_job(self, run_id: str) -> dict[str, Any]:
        """失败或取消的任务续跑：同一个请求排下一代运行，已处理过的场景直接跳过。"""
        jobs = self.jobs()
        if jobs is None:
            raise CharacterExperienceError("durable_jobs_unavailable", "后台任务服务未就绪")
        run = await jobs.get_run(run_id)
        if run is None:
            raise CharacterExperienceError("job_not_found", f"没有任务 {run_id}")
        request = await jobs.get_request(run.request_id)
        if request is None or request.kind not in JOB_KINDS:
            raise CharacterExperienceError("job_not_found", f"不是角色经历任务 {run_id}")
        if run.status not in {"failed", "cancelled"}:
            raise CharacterExperienceError("job_not_resumable", "只有失败或已取消的任务可以续跑")
        resumed = await jobs.schedule_run(
            request_id=request.request_id,
            schedule_slot=run.schedule_slot,
            cursor_generation=run.cursor_generation,
            cursor=run.cursor,
            reschedule_terminal=True,
        )
        return {"run_id": resumed.run_id, "status": resumed.status, "generation": resumed.cursor_generation, "kind": request.kind}

    # ------------------------------------------------------------------ 后台任务

    def job_handlers(self) -> dict[str, Any]:
        return {
            EXTRACT_KIND: self._run_extract,
            TIMELINE_KIND: self._run_timeline,
            EXPAND_KIND: self._run_expand,
            EMBED_KIND: self._run_embed,
        }

    @staticmethod
    async def _chat(client: Any, system: str, user: str) -> str:
        response = await client.text_chat(prompt=user, system_prompt=system)
        return str(getattr(response, "completion_text", "") or "")

    @staticmethod
    async def _progress(run: Any, runner: Any, progress: Mapping[str, Any], cursor: Mapping[str, Any]) -> bool:
        """记进度并续租；已请求取消时返回 False（调用方停下，执行器随后标记为已取消）。"""
        if await runner.service.cancellation_requested(run.run_id):
            return False
        try:
            await runner.service.update_progress(
                run.run_id,
                lease_owner=runner.lease_owner,
                lease_seconds=runner.lease_seconds,
                progress=dict(progress),
                cursor=dict(cursor),
            )
        except JobLeaseLostError:
            if await runner.service.cancellation_requested(run.run_id):
                return False
            raise
        return True

    async def _run_extract(self, run: Any, request: Any, runner: Any) -> dict[str, Any]:
        payload = dict(request.payload or {})
        bot_id = str(payload.get("bot_id") or "")
        client = self.llm(bot_id)
        if client is None:
            raise RuntimeError("character_experience_model_unavailable")
        params = self._extract_params(payload)
        path = safe_source_path(params["path"], self.source_roots)
        scenes, _stats = await asyncio.to_thread(self._scenes, path, params)
        confirmed = set(payload.get("scene_keys") or [])
        by_request = self._processed_by_request(bot_id, request.request_id)
        processed = await asyncio.to_thread(self.repo.processed_scene_keys, bot_id, [scene.key for scene in scenes])
        todo = [
            scene for scene in scenes
            if scene.key in confirmed and scene.key not in by_request and (params["redo"] or scene.key not in processed)
        ]
        stages = await asyncio.to_thread(self.repo.list_stages, bot_id)
        totals = {"done": len(confirmed) - len(todo), "memories": 0, "empty": 0, "failed": 0}
        total = len(confirmed)
        consecutive_failures = 0
        stopped = False
        for start in range(0, len(todo), params["concurrency"]):
            batch = todo[start:start + params["concurrency"]]
            outputs = await asyncio.gather(*(self._extract_scene(client, params, scene) for scene in batch), return_exceptions=True)
            for scene, output in zip(batch, outputs):
                if isinstance(output, BaseException) or output is None:
                    error = output if isinstance(output, BaseException) else ValueError("unparseable_model_output")
                    consecutive_failures += 1
                    totals["failed"] += 1
                    await asyncio.to_thread(
                        self.repo.record_scene_failure, bot_id, scene.key, kind="novel", job_id=request.request_id, error=str(error),
                    )
                    continue
                consecutive_failures = 0
                items = self._scene_items(scene, output, params, stages, path)
                result = await asyncio.to_thread(
                    self.repo.record_scene, bot_id, scene.key, items, kind="novel", job_id=request.request_id,
                    detail={"chapter": scene.chapter, "title": scene.title, "index": scene.index, "memories": len(items)},
                )
                totals["done"] += 1
                totals["memories"] += len(result["inserted"])
                totals["empty"] += 0 if items else 1
            progress = {"phase": "extract", "processed": totals["done"], "total": total, **totals}
            if not await self._progress(run, runner, progress, {"phase": "extract", "processed": totals["done"]}):
                stopped = True
                break
            if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                raise RuntimeError(f"连续 {consecutive_failures} 个场景调用模型失败，已停下；检查模型后点「续跑」")
        return {"kind": "extract", "bot_id": bot_id, "total": total, "stopped": stopped, **totals}

    def _processed_by_request(self, bot_id: str, request_id: str) -> set[str]:
        rows = self.repo.cm.execute_read(
            "SELECT scene_key FROM character_experience_scene_log WHERE bot_id=? AND job_id=? AND status IN ('done', 'empty')",
            (bot_id, request_id),
        ).fetchall()
        return {str(row[0]) for row in rows}

    async def _extract_scene(self, client: Any, params: Mapping[str, Any], scene: extract.Scene) -> list[dict[str, Any]] | None:
        system, user = extract.extract_prompts(
            name=params["name"], aliases=params["aliases"], description=params["description"],
            chapter_title=scene.title, scene_text=extract.clip_scene(scene.text, params["max_scene_chars"]),
        )
        return extract.parse_memory_list(await self._chat(client, system, user))

    def _scene_items(
        self,
        scene: extract.Scene,
        memories: Sequence[Mapping[str, Any]],
        params: Mapping[str, Any],
        stages: Sequence[Mapping[str, Any]],
        path: Path,
    ) -> list[dict[str, Any]]:
        stage_key = self._stage_for_chapter(stages, scene.chapter)
        items = []
        for n, memory in enumerate(memories):
            try:
                items.append(self.repo.clean_item({
                    "content": memory["memory"],
                    "emotion": memory.get("emotion"),
                    "characters": memory.get("characters"),
                    "story_time": memory.get("time"),
                    "stage_key": stage_key,
                    "seq": float(scene.chapter * 1_000_000 + scene.index * 1000 + n),
                    "canon_level": "canon",
                    "source_kind": "novel_extract",
                    "status": "draft",
                    "source_ref": {
                        "corpus": path.stem,
                        "source": path.name,
                        "chapter": scene.chapter,
                        "chapter_title": scene.title,
                        "scene_index": scene.index,
                        "scene_key": scene.key,
                        "scene_excerpt": extract.clip_scene(scene.text, 600),
                    },
                }))
            except CharacterExperienceError:
                continue
        return items

    async def _run_timeline(self, run: Any, request: Any, runner: Any) -> dict[str, Any]:
        payload = dict(request.payload or {})
        bot_id = str(payload.get("bot_id") or "")
        client = self.llm(bot_id)
        if client is None:
            raise RuntimeError("character_experience_model_unavailable")
        paragraphs = extract.split_setting_paragraphs(str(payload.get("setting") or ""))
        system, user = extract.timeline_prompts(name=str(payload.get("name") or ""), paragraphs=paragraphs)
        stages = extract.parse_timeline(await self._chat(client, system, user), paragraph_count=len(paragraphs))
        if not stages:
            raise RuntimeError("模型没有给出可用的时间线（输出不是约定的 JSON）；可以换个模型或改改设定再试")
        if not await self._progress(run, runner, {"phase": "timeline", "stages": len(stages)}, {"phase": "timeline"}):
            return {"kind": "timeline", "bot_id": bot_id, "stopped": True}
        removed = await asyncio.to_thread(self.repo.delete_unused_draft_stages, bot_id) if payload.get("replace_drafts") else []
        existing = {stage["stage_key"] for stage in await asyncio.to_thread(self.repo.list_stages, bot_id)}
        defs = []
        number = 1
        for stage in stages:
            while f"oc{number:02d}" in existing:
                number += 1
            key = f"oc{number:02d}"
            existing.add(key)
            defs.append({
                "stage_key": key,
                "title": stage["title"],
                "story_time": stage["story_time"],
                "description": stage["description"],
                "key_events": stage["key_events"],
                "status": "draft",
                "source_kind": "setting_compose",
                "source_ref": {"basis": stage["basis"], "setting_excerpt": extract.setting_excerpt(paragraphs, stage["basis"])},
            })
        created = await asyncio.to_thread(self.repo.ensure_stages, bot_id, defs)
        return {"kind": "timeline", "bot_id": bot_id, "stages_created": created, "removed_drafts": removed}

    async def _run_expand(self, run: Any, request: Any, runner: Any) -> dict[str, Any]:
        payload = dict(request.payload or {})
        bot_id = str(payload.get("bot_id") or "")
        client = self.llm(bot_id)
        if client is None:
            raise RuntimeError("character_experience_model_unavailable")
        setting = str(payload.get("setting") or "")
        paragraphs = extract.split_setting_paragraphs(setting)
        wanted = set(payload.get("stage_keys") or [])
        confirmed = set(payload.get("event_keys") or [])
        by_request = self._processed_by_request(bot_id, request.request_id)
        stages = [stage for stage in await asyncio.to_thread(self.repo.list_stages, bot_id) if stage["stage_key"] in wanted]
        todo = []
        for stage in stages:
            for index, event in enumerate(stage["key_events"]):
                key = extract.event_key(stage["stage_key"], event["text"])
                if key in confirmed and key not in by_request:
                    todo.append((stage, index, event, key))
        totals = {"done": len(confirmed) - len(todo), "memories": 0, "empty": 0, "failed": 0}
        total = len(confirmed)
        consecutive_failures = 0
        stopped = False
        for stage, index, event, key in todo:
            basis = event.get("basis") or stage.get("source_ref", {}).get("basis") or []
            excerpt = extract.setting_excerpt(paragraphs, basis) if basis else setting[:3000]
            system, user = extract.expand_prompts(
                name=str(payload.get("name") or ""), setting=excerpt or setting[:3000],
                stage_title=stage["title"], stage_time=stage["story_time"], stage_description=stage["description"],
                event=event["text"],
            )
            try:
                memories = extract.parse_memory_list(await self._chat(client, system, user))
                if memories is None:
                    raise ValueError("unparseable_model_output")
            except Exception as exc:
                consecutive_failures += 1
                totals["failed"] += 1
                await asyncio.to_thread(self.repo.record_scene_failure, bot_id, key, kind="event", job_id=request.request_id, error=str(exc))
                if consecutive_failures >= MAX_CONSECUTIVE_FAILURES:
                    raise RuntimeError(f"连续 {consecutive_failures} 个事件调用模型失败，已停下；检查模型后点「续跑」") from exc
                continue
            consecutive_failures = 0
            items = []
            for n, memory in enumerate(memories):
                try:
                    items.append(self.repo.clean_item({
                        "content": memory["memory"],
                        "emotion": memory.get("emotion"),
                        "characters": memory.get("characters"),
                        "story_time": stage["story_time"],
                        "stage_key": stage["stage_key"],
                        "plot_stage": event["text"][:100],
                        "seq": float(index * 1000 + n),
                        "canon_level": "invented",
                        "source_kind": "setting_compose",
                        "status": "draft",
                        "source_ref": {
                            "stage_key": stage["stage_key"],
                            "event": event["text"],
                            "basis": list(basis),
                            "setting_excerpt": excerpt[:1200],
                        },
                    }))
                except CharacterExperienceError:
                    continue
            result = await asyncio.to_thread(
                self.repo.record_scene, bot_id, key, items, kind="event", job_id=request.request_id,
                detail={"stage_key": stage["stage_key"], "event": event["text"][:120], "memories": len(items)},
            )
            totals["done"] += 1
            totals["memories"] += len(result["inserted"])
            totals["empty"] += 0 if items else 1
            progress = {"phase": "expand", "processed": totals["done"], "total": total, **totals}
            if not await self._progress(run, runner, progress, {"phase": "expand", "processed": totals["done"]}):
                stopped = True
                break
        return {"kind": "expand", "bot_id": bot_id, "total": total, "stopped": stopped, **totals}

    async def embed_missing(self, bot_id: str, *, limit: int = 5000, on_batch: Callable[[int], Any] | None = None) -> dict[str, int]:
        if self.embedding is None:
            raise RuntimeError("embedding_unavailable")
        embedded = failed = 0
        seen: set[int] = set()
        while embedded + failed < limit:
            rows = [row for row in await asyncio.to_thread(self.repo.missing_vectors, bot_id, limit=EMBED_BATCH * 4) if row[0] not in seen]
            if not rows:
                break
            for start in range(0, len(rows), EMBED_BATCH):
                batch = rows[start:start + EMBED_BATCH]
                vectors = await self.embedding.get_embeddings([content for _, content in batch])
                ready: dict[int, bytes] = {}
                for (experience_id, _content), vector in zip(batch, vectors or []):
                    seen.add(experience_id)
                    blob = _vector_bytes(vector, self.dimension)
                    if blob is None:
                        failed += 1
                    else:
                        ready[experience_id] = blob
                for experience_id, _ in batch[len(vectors or []):]:
                    seen.add(experience_id)
                    failed += 1
                embedded += await asyncio.to_thread(self.repo.set_vectors, bot_id, ready)
                if on_batch is not None:
                    keep_going = on_batch(embedded)
                    if asyncio.iscoroutine(keep_going):
                        keep_going = await keep_going
                    if keep_going is False:
                        return {"embedded": embedded, "failed": failed, "stopped": 1}
        return {"embedded": embedded, "failed": failed, "stopped": 0}

    async def _run_embed(self, run: Any, request: Any, runner: Any) -> dict[str, Any]:
        bot_id = str((request.payload or {}).get("bot_id") or "")

        async def on_batch(embedded: int) -> bool:
            return await self._progress(run, runner, {"phase": "embed", "embedded": embedded}, {"phase": "embed", "embedded": embedded})

        result = await self.embed_missing(bot_id, on_batch=on_batch)
        return {"kind": "embed", "bot_id": bot_id, **result}

    # ------------------------------------------------------------------ 旧数据迁移

    def migrate_legacy(self, *, dry_run: bool = False) -> list[dict[str, Any]]:
        """v5 记忆表里的旧第一人称经历 → 对应 Bot 的角色经历（幂等；见 engine/db/migrations/character_experience.py）。"""
        if self.registry is None:
            return []

        def save(profile: Any, expected_version: int) -> Any:
            return self.registry.save(profile, expected_version=expected_version, changed_by="migration", reason="角色经历：迁移旧的第一人称经历")

        reports = migrate_legacy_character_experiences(
            self.repo,
            profile_get=lambda bot_id: self.profile(bot_id),
            profile_save=save,
            book_notes=lambda category: load_legacy_book_notes(self.lore_db_path, category),
            dimension=self.dimension,
            dry_run=dry_run,
        )
        for report in reports:
            if report.get("status") == "applied":
                logger.info(
                    "[WaveMemory] 角色经历：已把旧经历 %s 迁入 Bot %s（导入 %s 条，已有 %s 条，对上书设笔记 %s 条，阶段 %s 个，当前时间点 %s）",
                    report.get("source"), report.get("bot_id"), report.get("imported"), report.get("duplicates"),
                    report.get("matched_notes"), report.get("stages"), report.get("last_stage") or "不限",
                )
        return reports


__all__ = [
    "EMBED_KIND",
    "EXPAND_KIND",
    "EXTRACT_KIND",
    "JOB_KINDS",
    "TIMELINE_KIND",
    "CharacterExperienceService",
    "keyword_match_expr",
]
