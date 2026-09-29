"""角色经历仓储：按 Bot 的第一人称经历、时间线阶段、任务场景记录与一次性迁移标记（自有小表，不经 DomainCommand）。

- 所有写入都在一个事务里完成，全文索引 ``character_experiences_fts`` 在同一事务里同步；
- 管理台的修改带 ``expected_version``：版本对不上整体回滚并抛 :class:`CharacterExperienceConflict`（API 返回 409）；
- 向量是派生数据，补算向量不改版本号（不会让正在编辑的管理员撞上版本冲突）；
- 同一个 Bot 内按规范化正文指纹去重（``UNIQUE(bot_id, content_hash)``），重复导入 / 续跑不会多出行。
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from .connection import ConnectionManager
from .fts_cjk import token_text
from .migrations.character_experience import FTS_TABLE, ensure_character_experience_schema

try:
    from ...domain.character_experience import (
        CANON_LEVELS,
        SOURCE_KINDS,
        STAGE_STATUSES,
        STATUSES,
        content_hash,
        validate_stage_key,
    )
except ImportError:  # 仓库测试把插件根目录当顶层包
    from domain.character_experience import (
        CANON_LEVELS,
        SOURCE_KINDS,
        STAGE_STATUSES,
        STATUSES,
        content_hash,
        validate_stage_key,
    )

MAX_CONTENT_CHARS = 2000
_EXPERIENCE_COLUMNS = (
    "id", "bot_id", "content", "content_hash", "story_time", "stage_key", "seq", "plot_stage", "emotion",
    "characters", "canon_level", "source_kind", "source_ref", "status", "version", "job_id", "created_by",
    "created_at", "updated_at",
)
_SELECT = ", ".join(f"e.{name}" for name in _EXPERIENCE_COLUMNS) + ", (e.vector IS NOT NULL) AS has_vector"
_METADATA_FIELDS = ("story_time", "stage_key", "seq", "plot_stage", "emotion", "characters", "source_ref")
_EDITABLE_FIELDS = frozenset({"content", "story_time", "stage_key", "seq", "plot_stage", "emotion", "characters", "canon_level", "status"})
_STAGE_COLUMNS = (
    "bot_id", "stage_key", "ordinal", "title", "story_time", "description", "key_events", "chapter_from",
    "chapter_to", "status", "source_kind", "source_ref", "version", "created_at", "updated_at",
)
_STAGE_EDITABLE = frozenset({"title", "story_time", "description", "key_events", "chapter_from", "chapter_to", "status", "ordinal"})


class CharacterExperienceError(ValueError):
    """数据不合法或操作不允许；``code`` 供 API 返回稳定错误码。"""

    def __init__(self, code: str, message: str = "") -> None:
        super().__init__(message or code)
        self.code = code
        self.reason_code = code


class CharacterExperienceConflict(CharacterExperienceError):
    """经历或阶段已被别处改过（版本号对不上）；本次改动整体未写入。"""

    def __init__(self, ids: Iterable[Any], message: str = "") -> None:
        self.ids = [str(value) for value in ids]
        super().__init__("version_conflict", message or f"已被修改（{', '.join(self.ids)}），请刷新后重试")


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _loads(raw: Any, default: Any) -> Any:
    try:
        value = json.loads(raw) if isinstance(raw, str) and raw else default
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _text(value: Any, limit: int) -> str:
    return ("" if value is None else str(value).strip())[:limit]


def _names(value: Any) -> list[str]:
    if value is None:
        return []
    items = value if isinstance(value, (list, tuple)) else str(value).replace("，", ",").replace("、", ",").split(",")
    out: list[str] = []
    for item in items:
        text = _text(item, 40)
        if text and text not in out:
            out.append(text)
    return out[:20]


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _events(value: Any) -> list[dict[str, Any]]:
    """关键事件：字符串或 {text, basis:[段落编号]}，统一成对象。"""
    items = value if isinstance(value, list) else ([value] if isinstance(value, (str, dict)) else [])
    out: list[dict[str, Any]] = []
    for item in items:
        if isinstance(item, Mapping):
            text = _text(item.get("text") or item.get("event"), 300)
            basis = [int(b) for b in (item.get("basis") or []) if isinstance(b, (int, float)) or str(b).isdigit()]
        else:
            text, basis = _text(item, 300), []
        if text:
            out.append({"text": text, "basis": sorted(set(basis))[:10]})
    return out[:20]


def _fts_text(item: Mapping[str, Any]) -> str:
    parts = [
        str(item.get("content") or ""),
        str(item.get("plot_stage") or ""),
        str(item.get("story_time") or ""),
        " ".join(item.get("characters") or []) if isinstance(item.get("characters"), list) else str(item.get("characters") or ""),
    ]
    return token_text(" ".join(part for part in parts if part))


def _row(row: Sequence[Any]) -> dict[str, Any]:
    item = dict(zip((*_EXPERIENCE_COLUMNS, "has_vector"), row))
    item["characters"] = _loads(item.get("characters"), [])
    item["source_ref"] = _loads(item.get("source_ref"), {})
    item["has_vector"] = bool(item.get("has_vector"))
    return item


def _stage_row(row: Sequence[Any]) -> dict[str, Any]:
    item = dict(zip(_STAGE_COLUMNS, row))
    item["key_events"] = _events(_loads(item.get("key_events"), []))
    item["source_ref"] = _loads(item.get("source_ref"), {})
    return item


class CharacterExperienceRepo:
    def __init__(self, cm: ConnectionManager, *, ensure_schema: bool = True) -> None:
        if not isinstance(cm, ConnectionManager):
            raise TypeError("cm must be a ConnectionManager")
        self.cm = cm
        if ensure_schema:
            ensure_character_experience_schema(cm)

    # ─── 校验 ───

    @staticmethod
    def clean_item(raw: Mapping[str, Any], *, defaults: Mapping[str, Any] | None = None) -> dict[str, Any]:
        """一条新经历的规范形；正文为空或枚举值非法时抛 :class:`CharacterExperienceError`。"""
        data = {**(defaults or {}), **{k: v for k, v in raw.items() if v is not None}}
        content = _text(data.get("content"), MAX_CONTENT_CHARS + 1)
        if not content:
            raise CharacterExperienceError("content_required", "经历正文不能为空")
        if len(content) > MAX_CONTENT_CHARS:
            raise CharacterExperienceError("content_too_long", f"一条经历最多 {MAX_CONTENT_CHARS} 字")
        stage_key = _text(data.get("stage_key"), 64)
        if stage_key:
            try:
                stage_key = validate_stage_key(stage_key)
            except ValueError as exc:
                raise CharacterExperienceError("invalid_stage_key", str(exc)) from exc
        canon = _text(data.get("canon_level"), 20) or "canon"
        if canon not in CANON_LEVELS:
            raise CharacterExperienceError("invalid_canon_level", f"可信级别只能是 {', '.join(CANON_LEVELS)}")
        status = _text(data.get("status"), 20) or "draft"
        if status not in STATUSES:
            raise CharacterExperienceError("invalid_status", f"状态只能是 {', '.join(STATUSES)}")
        source_kind = _text(data.get("source_kind"), 40) or "manual"
        if source_kind not in SOURCE_KINDS:
            source_kind = "manual"
        try:
            seq = float(data.get("seq") or 0.0)
        except (TypeError, ValueError):
            seq = 0.0
        source_ref = data.get("source_ref") if isinstance(data.get("source_ref"), Mapping) else {}
        vector = data.get("vector")
        return {
            "content": content,
            "content_hash": content_hash(content),
            "story_time": _text(data.get("story_time"), 100),
            "stage_key": stage_key,
            "seq": seq,
            "plot_stage": _text(data.get("plot_stage"), 100),
            "emotion": _text(data.get("emotion"), 20),
            "characters": _names(data.get("characters")),
            "canon_level": canon,
            "source_kind": source_kind,
            "source_ref": json.loads(_dumps(dict(source_ref))),
            "status": status,
            "vector": bytes(vector) if isinstance(vector, (bytes, bytearray, memoryview)) else None,
        }

    # ─── 读 ───

    def get(self, bot_id: str, experience_id: int) -> dict[str, Any] | None:
        row = self.cm.execute_read(
            f"SELECT {_SELECT} FROM character_experiences e WHERE e.id=? AND e.bot_id=?",
            (int(experience_id), str(bot_id or "")),
        ).fetchone()
        return _row(row) if row else None

    def get_many(self, bot_id: str, ids: Iterable[int]) -> list[dict[str, Any]]:
        wanted = sorted({int(value) for value in ids})
        if not wanted:
            return []
        rows = self.cm.execute_read(
            f"SELECT {_SELECT} FROM character_experiences e WHERE e.bot_id=? AND e.id IN ({','.join('?' for _ in wanted)})",
            (str(bot_id or ""), *wanted),
        ).fetchall()
        return [_row(row) for row in rows]

    def list(
        self,
        bot_id: str,
        *,
        statuses: Iterable[str] | None = None,
        stage_key: str | None = None,
        query: str = "",
        canon_level: str = "",
        source_kind: str = "",
        order: str = "timeline",
        limit: int = 50,
        offset: int = 0,
    ) -> tuple[list[dict[str, Any]], int]:
        """``stage_key`` 为 None 表示不按阶段过滤，空串表示「未分阶段」。"""
        where = ["e.bot_id=?"]
        params: list[Any] = [str(bot_id or "")]
        wanted = [s for s in (statuses or []) if s in STATUSES]
        if wanted:
            where.append(f"e.status IN ({','.join('?' for _ in wanted)})")
            params.extend(wanted)
        if stage_key is not None:
            where.append("e.stage_key=?")
            params.append(str(stage_key))
        if canon_level:
            where.append("e.canon_level=?")
            params.append(canon_level)
        if source_kind:
            where.append("e.source_kind=?")
            params.append(source_kind)
        text = str(query or "").strip()
        if text:
            like = "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
            where.append(
                "(e.content LIKE ? ESCAPE '\\' OR e.plot_stage LIKE ? ESCAPE '\\' "
                "OR e.story_time LIKE ? ESCAPE '\\' OR e.characters LIKE ? ESCAPE '\\' OR e.emotion LIKE ? ESCAPE '\\')"
            )
            params.extend([like] * 5)
        clause = " AND ".join(where)
        total = int(self.cm.execute_read(
            f"SELECT COUNT(*) FROM character_experiences e WHERE {clause}", params,
        ).fetchone()[0])
        if order == "recent":
            order_by = "e.updated_at DESC, e.id DESC"
        else:
            order_by = "COALESCE(s.ordinal, -1), e.seq, e.id"
        rows = self.cm.execute_read(
            f"""SELECT {_SELECT} FROM character_experiences e
                  LEFT JOIN character_timeline_stages s ON s.bot_id=e.bot_id AND s.stage_key=e.stage_key
                 WHERE {clause} ORDER BY {order_by} LIMIT ? OFFSET ?""",
            (*params, max(1, min(int(limit), 500)), max(0, int(offset))),
        ).fetchall()
        return [_row(row) for row in rows], total

    def export_rows(self, bot_id: str, *, statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        items, _total = self.list(bot_id, statuses=statuses, limit=500)
        # list() 最多 500 条：导出按页取全
        if _total > len(items):
            offset = len(items)
            while offset < _total:
                page, _ = self.list(bot_id, statuses=statuses, limit=500, offset=offset)
                if not page:
                    break
                items.extend(page)
                offset += len(page)
        return items

    def counts(self, bot_id: str) -> dict[str, Any]:
        bot = str(bot_id or "")
        by_status = {status: 0 for status in STATUSES}
        by_stage: dict[str, dict[str, int]] = {}
        by_canon = {level: 0 for level in CANON_LEVELS}
        by_source: dict[str, int] = {}
        for stage_key, status, canon, source_kind, count in self.cm.execute_read(
            """SELECT stage_key, status, canon_level, source_kind, COUNT(*) FROM character_experiences
                WHERE bot_id=? GROUP BY stage_key, status, canon_level, source_kind""",
            (bot,),
        ).fetchall():
            count = int(count)
            by_status[status] = by_status.get(status, 0) + count
            stage = by_stage.setdefault(str(stage_key or ""), {s: 0 for s in STATUSES})
            stage[status] = stage.get(status, 0) + count
            by_canon[canon] = by_canon.get(canon, 0) + count
            by_source[source_kind] = by_source.get(source_kind, 0) + count
        missing = int(self.cm.execute_read(
            "SELECT COUNT(*) FROM character_experiences WHERE bot_id=? AND status='active' AND vector IS NULL",
            (bot,),
        ).fetchone()[0])
        return {
            "total": sum(by_status.values()),
            "by_status": by_status,
            "by_stage": by_stage,
            "by_canon": by_canon,
            "by_source": by_source,
            "active_missing_vectors": missing,
        }

    def bot_ids(self) -> list[str]:
        rows = self.cm.execute_read(
            "SELECT bot_id FROM character_experiences UNION SELECT bot_id FROM character_timeline_stages ORDER BY 1",
        ).fetchall()
        return [str(row[0]) for row in rows if row[0]]

    # ─── 召回 ───

    def fingerprint(self, bot_id: str) -> tuple[int, float, int]:
        row = self.cm.execute_read(
            """SELECT COUNT(*), COALESCE(MAX(updated_at), 0), COALESCE(SUM(vector IS NOT NULL), 0)
                 FROM character_experiences WHERE bot_id=? AND status='active'""",
            (str(bot_id or ""),),
        ).fetchone()
        return int(row[0]), float(row[1]), int(row[2])

    def active_vectors(self, bot_id: str) -> list[tuple[int, str, bytes]]:
        rows = self.cm.execute_read(
            """SELECT id, stage_key, vector FROM character_experiences
                WHERE bot_id=? AND status='active' AND vector IS NOT NULL ORDER BY id""",
            (str(bot_id or ""),),
        ).fetchall()
        return [(int(r[0]), str(r[1] or ""), bytes(r[2])) for r in rows]

    def fts_search(self, bot_id: str, expr: str, *, limit: int = 30) -> list[tuple[int, str]]:
        """启用状态的经历里做关键词检索，按 bm25 排序返回 (id, stage_key)。"""
        if not expr:
            return []
        try:
            rows = self.cm.execute_read(
                f"""SELECT e.id, e.stage_key FROM {FTS_TABLE}
                      JOIN character_experiences e ON e.id = {FTS_TABLE}.rowid
                     WHERE {FTS_TABLE} MATCH ? AND e.bot_id=? AND e.status='active'
                     ORDER BY rank LIMIT ?""",
                (expr, str(bot_id or ""), max(1, int(limit))),
            ).fetchall()
        except Exception:
            return []
        return [(int(r[0]), str(r[1] or "")) for r in rows]

    def missing_vectors(self, bot_id: str, *, limit: int = 200) -> list[tuple[int, str]]:
        rows = self.cm.execute_read(
            """SELECT id, content FROM character_experiences
                WHERE bot_id=? AND status='active' AND vector IS NULL ORDER BY id LIMIT ?""",
            (str(bot_id or ""), max(1, int(limit))),
        ).fetchall()
        return [(int(r[0]), str(r[1] or "")) for r in rows]

    def set_vectors(self, bot_id: str, vectors: Mapping[int, bytes]) -> int:
        if not vectors:
            return 0
        with self.cm.write_transaction() as tx:
            changed = 0
            for experience_id, blob in vectors.items():
                changed += tx.execute(
                    "UPDATE character_experiences SET vector=? WHERE id=? AND bot_id=?",
                    (bytes(blob), int(experience_id), str(bot_id or "")),
                ).rowcount
            return changed

    # ─── 写：经历 ───

    def _fts_sync(self, tx: Any, experience_id: int, item: Mapping[str, Any] | None) -> None:
        tx.execute(f"DELETE FROM {FTS_TABLE} WHERE rowid=?", (int(experience_id),))
        if item is not None:
            tokens = _fts_text(item)
            if tokens:
                tx.execute(f"INSERT INTO {FTS_TABLE}(rowid, tokens) VALUES (?, ?)", (int(experience_id), tokens))

    def _insert(self, tx: Any, bot: str, item: Mapping[str, Any], *, created_by: str, job_id: str, now: float) -> int | None:
        cursor = tx.execute(
            """INSERT INTO character_experiences (bot_id, content, content_hash, story_time, stage_key, seq, plot_stage,
                   emotion, characters, canon_level, source_kind, source_ref, status, version, vector, job_id, created_by,
                   created_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?, ?, ?, ?)
               ON CONFLICT(bot_id, content_hash) DO NOTHING""",
            (
                bot, item["content"], item["content_hash"], item["story_time"], item["stage_key"], item["seq"],
                item["plot_stage"], item["emotion"], _dumps(item["characters"]), item["canon_level"],
                item["source_kind"], _dumps(item["source_ref"]), item["status"], item.get("vector"),
                str(job_id or "")[:80], str(created_by or "")[:40], now, now,
            ),
        )
        if cursor.rowcount != 1:
            return None
        experience_id = int(cursor.lastrowid)
        self._fts_sync(tx, experience_id, item)
        return experience_id

    def _merge_duplicate(self, tx: Any, bot: str, item: Mapping[str, Any], mode: str, now: float) -> tuple[int, bool]:
        row = tx.execute(
            f"SELECT {_SELECT} FROM character_experiences e WHERE e.bot_id=? AND e.content_hash=?",
            (bot, item["content_hash"]),
        ).fetchone()
        current = _row(row)
        if mode not in {"fill", "overwrite"}:
            return int(current["id"]), False
        updates: dict[str, Any] = {}
        for name in _METADATA_FIELDS:
            incoming = item.get(name)
            existing = current.get(name)
            if name == "source_ref":
                merged = dict(existing or {})
                for key, value in (incoming or {}).items():
                    if mode == "overwrite" or key not in merged:
                        merged[key] = value
                if merged != existing:
                    updates[name] = merged
                continue
            if incoming in (None, "", [], 0, 0.0):
                continue
            if mode == "overwrite" or existing in (None, "", [], 0, 0.0):
                if incoming != existing:
                    updates[name] = incoming
        if mode == "overwrite" and item.get("canon_level") and item["canon_level"] != current["canon_level"]:
            updates["canon_level"] = item["canon_level"]
        if item.get("vector") is not None and not current["has_vector"]:
            tx.execute("UPDATE character_experiences SET vector=? WHERE id=?", (item["vector"], current["id"]))
        if not updates:
            return int(current["id"]), False
        assignments = ", ".join(f"{name}=?" for name in updates)
        values = [_dumps(value) if name in {"characters", "source_ref"} else value for name, value in updates.items()]
        tx.execute(
            f"UPDATE character_experiences SET {assignments}, version=version+1, updated_at=? WHERE id=?",
            (*values, now, current["id"]),
        )
        self._fts_sync(tx, int(current["id"]), {**current, **updates})
        return int(current["id"]), True

    def add_many(
        self,
        bot_id: str,
        items: Sequence[Mapping[str, Any]],
        *,
        created_by: str = "",
        job_id: str = "",
        on_duplicate: str = "skip",
        now: float | None = None,
        _tx: Any = None,
    ) -> dict[str, list[int]]:
        """写入一批经历（已 :meth:`clean_item`）；同正文已存在时按 ``on_duplicate`` 跳过 / 补空字段 / 覆盖元数据。"""
        bot = str(bot_id or "").strip()
        if not bot:
            raise CharacterExperienceError("bot_id_required", "bot_id 不能为空")
        stamp = float(now if now is not None else time.time())
        result: dict[str, list[int]] = {"inserted": [], "duplicates": [], "updated": []}

        def _apply(tx: Any) -> None:
            for item in items:
                new_id = self._insert(tx, bot, item, created_by=created_by, job_id=job_id, now=stamp)
                if new_id is not None:
                    result["inserted"].append(new_id)
                    continue
                existing_id, changed = self._merge_duplicate(tx, bot, item, on_duplicate, stamp)
                result["updated" if changed else "duplicates"].append(existing_id)

        if _tx is not None:
            _apply(_tx)
        else:
            with self.cm.write_transaction() as tx:
                _apply(tx)
        return result

    def update(
        self,
        bot_id: str,
        experience_id: int,
        fields: Mapping[str, Any],
        *,
        expected_version: int,
        now: float | None = None,
    ) -> dict[str, Any]:
        unknown = set(fields) - _EDITABLE_FIELDS
        if unknown:
            raise CharacterExperienceError("unknown_fields", f"不能修改这些字段：{', '.join(sorted(unknown))}")
        bot = str(bot_id or "")
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            row = tx.execute(
                f"SELECT {_SELECT} FROM character_experiences e WHERE e.id=? AND e.bot_id=?",
                (int(experience_id), bot),
            ).fetchone()
            if row is None:
                raise CharacterExperienceError("experience_not_found", f"没有这条经历 #{experience_id}")
            current = _row(row)
            if int(current["version"]) != int(expected_version):
                raise CharacterExperienceConflict([experience_id])
            merged = self.clean_item({**current, **dict(fields), "vector": None})
            updates = {name: merged[name] for name in fields if name in merged}
            if "content" in updates:
                updates["content_hash"] = merged["content_hash"]
                if merged["content_hash"] != current["content_hash"]:
                    clash = tx.execute(
                        "SELECT id FROM character_experiences WHERE bot_id=? AND content_hash=? AND id!=?",
                        (bot, merged["content_hash"], int(experience_id)),
                    ).fetchone()
                    if clash is not None:
                        raise CharacterExperienceError("duplicate_content", f"已有同样内容的经历 #{clash[0]}")
            if not updates:
                return current
            assignments = ", ".join(f"{name}=?" for name in updates)
            values = [_dumps(value) if name in {"characters", "source_ref"} else value for name, value in updates.items()]
            # 正文改了，旧向量不再对应：清掉，等补算。
            clear_vector = ", vector=NULL" if "content" in updates and merged["content_hash"] != current["content_hash"] else ""
            tx.execute(
                f"UPDATE character_experiences SET {assignments}{clear_vector}, version=version+1, updated_at=? WHERE id=?",
                (*values, stamp, int(experience_id)),
            )
            self._fts_sync(tx, int(experience_id), {**current, **updates})
        updated = self.get(bot, int(experience_id))
        assert updated is not None
        return updated

    def _bulk(self, bot_id: str, items: Sequence[tuple[int, int]], assignments: str, values: Sequence[Any], now: float | None) -> list[int]:
        bot = str(bot_id or "")
        stamp = float(now if now is not None else time.time())
        if not items:
            return []
        conflicts: list[int] = []
        changed: list[int] = []
        with self.cm.write_transaction() as tx:
            for experience_id, version in items:
                cursor = tx.execute(
                    f"UPDATE character_experiences SET {assignments}, version=version+1, updated_at=? "
                    "WHERE id=? AND bot_id=? AND version=?",
                    (*values, stamp, int(experience_id), bot, int(version)),
                )
                if cursor.rowcount != 1:
                    conflicts.append(int(experience_id))
                else:
                    changed.append(int(experience_id))
            if conflicts:
                raise CharacterExperienceConflict(conflicts)
        return changed

    def set_status(self, bot_id: str, items: Sequence[tuple[int, int]], status: str, *, now: float | None = None) -> list[int]:
        """批量改状态，全部成功或全部不写（任一条版本对不上抛冲突）。"""
        if status not in STATUSES:
            raise CharacterExperienceError("invalid_status", f"状态只能是 {', '.join(STATUSES)}")
        return self._bulk(bot_id, items, "status=?", (status,), now)

    def set_stage(self, bot_id: str, items: Sequence[tuple[int, int]], stage_key: str, *, now: float | None = None) -> list[int]:
        key = str(stage_key or "").strip()
        if key:
            if self.get_stage(bot_id, key) is None:
                raise CharacterExperienceError("stage_not_found", f"时间线里没有阶段 {key}")
        return self._bulk(bot_id, items, "stage_key=?", (key,), now)

    # ─── 时间线阶段 ───

    def list_stages(self, bot_id: str) -> list[dict[str, Any]]:
        rows = self.cm.execute_read(
            f"SELECT {', '.join(_STAGE_COLUMNS)} FROM character_timeline_stages WHERE bot_id=? ORDER BY ordinal, stage_key",
            (str(bot_id or ""),),
        ).fetchall()
        return [_stage_row(row) for row in rows]

    def get_stage(self, bot_id: str, stage_key: str) -> dict[str, Any] | None:
        row = self.cm.execute_read(
            f"SELECT {', '.join(_STAGE_COLUMNS)} FROM character_timeline_stages WHERE bot_id=? AND stage_key=?",
            (str(bot_id or ""), str(stage_key or "")),
        ).fetchone()
        return _stage_row(row) if row else None

    @staticmethod
    def _clean_stage(fields: Mapping[str, Any]) -> dict[str, Any]:
        out: dict[str, Any] = {}
        for name, value in fields.items():
            if name in {"title", "story_time"}:
                out[name] = _text(value, 100)
            elif name == "description":
                out[name] = _text(value, 1000)
            elif name == "key_events":
                out[name] = _events(value)
            elif name in {"chapter_from", "chapter_to"}:
                out[name] = _optional_int(value)
            elif name == "status":
                status = _text(value, 20) or "confirmed"
                if status not in STAGE_STATUSES:
                    raise CharacterExperienceError("invalid_stage_status", f"阶段状态只能是 {', '.join(STAGE_STATUSES)}")
                out[name] = status
            elif name == "ordinal":
                parsed = _optional_int(value)
                if parsed is not None:
                    out[name] = parsed
            elif name == "source_kind":
                out[name] = _text(value, 40) or "manual"
            elif name == "source_ref":
                out[name] = dict(value) if isinstance(value, Mapping) else {}
        return out

    def _insert_stage(self, tx: Any, bot: str, key: str, fields: Mapping[str, Any], now: float) -> None:
        ordinal = fields.get("ordinal")
        if ordinal is None:
            ordinal = int(tx.execute(
                "SELECT COALESCE(MAX(ordinal), 0) + 1 FROM character_timeline_stages WHERE bot_id=?", (bot,),
            ).fetchone()[0])
        tx.execute(
            f"""INSERT INTO character_timeline_stages ({', '.join(_STAGE_COLUMNS)})
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 1, ?, ?)""",
            (
                bot, key, int(ordinal), fields.get("title") or key, fields.get("story_time", ""),
                fields.get("description", ""), _dumps(fields.get("key_events", [])), fields.get("chapter_from"),
                fields.get("chapter_to"), fields.get("status", "confirmed"), fields.get("source_kind", "manual"),
                _dumps(fields.get("source_ref", {})), now, now,
            ),
        )

    def save_stage(
        self,
        bot_id: str,
        stage_key: str,
        fields: Mapping[str, Any],
        *,
        expected_version: int | None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """新增（``expected_version`` 为 0 / None 且不存在）或按版本更新一个阶段。"""
        bot = str(bot_id or "").strip()
        try:
            key = validate_stage_key(stage_key)
        except ValueError as exc:
            raise CharacterExperienceError("invalid_stage_key", str(exc)) from exc
        unknown = set(fields) - _STAGE_EDITABLE - {"source_kind", "source_ref"}
        if unknown:
            raise CharacterExperienceError("unknown_fields", f"不能修改这些字段：{', '.join(sorted(unknown))}")
        cleaned = self._clean_stage(fields)
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            row = tx.execute(
                "SELECT version FROM character_timeline_stages WHERE bot_id=? AND stage_key=?", (bot, key),
            ).fetchone()
            if row is None:
                if expected_version not in (None, 0):
                    raise CharacterExperienceConflict([key])
                self._insert_stage(tx, bot, key, cleaned, stamp)
            else:
                if expected_version is None or int(expected_version) != int(row[0]):
                    raise CharacterExperienceConflict([key])
                if cleaned:
                    assignments = ", ".join(f"{name}=?" for name in cleaned)
                    values = [_dumps(v) if name in {"key_events", "source_ref"} else v for name, v in cleaned.items()]
                    tx.execute(
                        f"UPDATE character_timeline_stages SET {assignments}, version=version+1, updated_at=? "
                        "WHERE bot_id=? AND stage_key=?",
                        (*values, stamp, bot, key),
                    )
        saved = self.get_stage(bot, key)
        assert saved is not None
        return saved

    def delete_stage(self, bot_id: str, stage_key: str, *, expected_version: int) -> None:
        bot = str(bot_id or "")
        with self.cm.write_transaction() as tx:
            row = tx.execute(
                "SELECT version FROM character_timeline_stages WHERE bot_id=? AND stage_key=?", (bot, str(stage_key)),
            ).fetchone()
            if row is None:
                raise CharacterExperienceError("stage_not_found", f"时间线里没有阶段 {stage_key}")
            if int(row[0]) != int(expected_version):
                raise CharacterExperienceConflict([stage_key])
            used = int(tx.execute(
                "SELECT COUNT(*) FROM character_experiences WHERE bot_id=? AND stage_key=?", (bot, str(stage_key)),
            ).fetchone()[0])
            if used:
                raise CharacterExperienceError("stage_in_use", f"还有 {used} 条经历在这个阶段，先把它们移到别的阶段")
            tx.execute("DELETE FROM character_timeline_stages WHERE bot_id=? AND stage_key=?", (bot, str(stage_key)))

    def reorder_stages(self, bot_id: str, items: Sequence[tuple[str, int]], *, now: float | None = None) -> list[dict[str, Any]]:
        """按给定顺序重排全部阶段（序号 1..n）；必须列出全部阶段并带版本号。"""
        bot = str(bot_id or "")
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            existing = {
                str(row[0]): int(row[1])
                for row in tx.execute("SELECT stage_key, version FROM character_timeline_stages WHERE bot_id=?", (bot,)).fetchall()
            }
            keys = [str(key) for key, _ in items]
            if sorted(keys) != sorted(existing) or len(set(keys)) != len(keys):
                raise CharacterExperienceError("stage_list_mismatch", "重排必须列出全部阶段，且不能重复")
            conflicts = [key for key, version in items if existing.get(str(key)) != int(version)]
            if conflicts:
                raise CharacterExperienceConflict(conflicts)
            for ordinal, key in enumerate(keys, 1):
                tx.execute(
                    "UPDATE character_timeline_stages SET ordinal=?, version=version+1, updated_at=? WHERE bot_id=? AND stage_key=?",
                    (ordinal, stamp, bot, key),
                )
        return self.list_stages(bot)

    def confirm_stages(self, bot_id: str, items: Sequence[tuple[str, int]], *, now: float | None = None) -> list[str]:
        bot = str(bot_id or "")
        stamp = float(now if now is not None else time.time())
        conflicts: list[str] = []
        done: list[str] = []
        with self.cm.write_transaction() as tx:
            for key, version in items:
                cursor = tx.execute(
                    "UPDATE character_timeline_stages SET status='confirmed', version=version+1, updated_at=? "
                    "WHERE bot_id=? AND stage_key=? AND version=?",
                    (stamp, bot, str(key), int(version)),
                )
                (done if cursor.rowcount == 1 else conflicts).append(str(key))
            if conflicts:
                raise CharacterExperienceConflict(conflicts)
        return done

    def ensure_stages(self, bot_id: str, stages: Sequence[Mapping[str, Any]], *, now: float | None = None, _tx: Any = None) -> list[str]:
        """补上时间线里还没有的阶段（已有的不动）；新阶段接在现有阶段之后，保持给定的相对顺序。"""
        bot = str(bot_id or "")
        stamp = float(now if now is not None else time.time())
        created: list[str] = []

        def _apply(tx: Any) -> None:
            existing = {str(r[0]) for r in tx.execute("SELECT stage_key FROM character_timeline_stages WHERE bot_id=?", (bot,)).fetchall()}
            for stage in sorted(stages, key=lambda s: (s.get("ordinal") is None, s.get("ordinal") or 0)):
                try:
                    key = validate_stage_key(stage.get("stage_key"))
                except ValueError:
                    continue
                if key in existing:
                    continue
                fields = self._clean_stage({k: v for k, v in stage.items() if k != "ordinal" and k != "stage_key"})
                self._insert_stage(tx, bot, key, fields, stamp)
                existing.add(key)
                created.append(key)

        if _tx is not None:
            _apply(_tx)
        else:
            with self.cm.write_transaction() as tx:
                _apply(tx)
        return created

    def delete_unused_draft_stages(self, bot_id: str) -> list[str]:
        """删掉还没有任何经历的草稿阶段（重新起草时间线前用）。"""
        bot = str(bot_id or "")
        with self.cm.write_transaction() as tx:
            keys = [
                str(row[0]) for row in tx.execute(
                    """SELECT s.stage_key FROM character_timeline_stages s
                        WHERE s.bot_id=? AND s.status='draft' AND NOT EXISTS (
                            SELECT 1 FROM character_experiences e WHERE e.bot_id=s.bot_id AND e.stage_key=s.stage_key)""",
                    (bot,),
                ).fetchall()
            ]
            for key in keys:
                tx.execute("DELETE FROM character_timeline_stages WHERE bot_id=? AND stage_key=?", (bot, key))
        return keys

    # ─── 任务场景记录 ───

    def processed_scene_keys(self, bot_id: str, keys: Iterable[str]) -> set[str]:
        wanted = [str(key) for key in keys]
        done: set[str] = set()
        for start in range(0, len(wanted), 500):
            chunk = wanted[start:start + 500]
            rows = self.cm.execute_read(
                f"""SELECT scene_key FROM character_experience_scene_log
                     WHERE bot_id=? AND status IN ('done', 'empty') AND scene_key IN ({','.join('?' for _ in chunk)})""",
                (str(bot_id or ""), *chunk),
            ).fetchall()
            done.update(str(row[0]) for row in rows)
        return done

    def record_scene(
        self,
        bot_id: str,
        scene_key: str,
        items: Sequence[Mapping[str, Any]],
        *,
        kind: str,
        job_id: str,
        created_by: str = "job",
        detail: Mapping[str, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, list[int]]:
        """一个场景（或关键事件）的产出与处理记录在同一个事务里写入：写了经历就一定记下「已处理」。"""
        bot = str(bot_id or "")
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            result = self.add_many(bot, items, created_by=created_by, job_id=job_id, now=stamp, _tx=tx)
            tx.execute(
                """INSERT INTO character_experience_scene_log (bot_id, scene_key, kind, job_id, status, memories, attempts, detail, processed_at)
                   VALUES (?, ?, ?, ?, ?, ?, 1, ?, ?)
                   ON CONFLICT(bot_id, scene_key) DO UPDATE SET kind=excluded.kind, job_id=excluded.job_id,
                       status=excluded.status, memories=excluded.memories, attempts=attempts+1,
                       detail=excluded.detail, processed_at=excluded.processed_at""",
                (bot, str(scene_key), kind, str(job_id or ""), "done" if items else "empty",
                 len(result["inserted"]), _dumps(dict(detail or {})), stamp),
            )
        return result

    def record_scene_failure(self, bot_id: str, scene_key: str, *, kind: str, job_id: str, error: str, now: float | None = None) -> None:
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            tx.execute(
                """INSERT INTO character_experience_scene_log (bot_id, scene_key, kind, job_id, status, memories, attempts, detail, processed_at)
                   VALUES (?, ?, ?, ?, 'failed', 0, 1, ?, ?)
                   ON CONFLICT(bot_id, scene_key) DO UPDATE SET job_id=excluded.job_id, status='failed',
                       attempts=attempts+1, detail=excluded.detail, processed_at=excluded.processed_at
                   WHERE character_experience_scene_log.status NOT IN ('done', 'empty')""",
                (str(bot_id or ""), str(scene_key), kind, str(job_id or ""), _dumps({"error": str(error)[:300]}), stamp),
            )

    def scene_stats(self, bot_id: str) -> dict[str, dict[str, int]]:
        out: dict[str, dict[str, int]] = {}
        for kind, status, count in self.cm.execute_read(
            "SELECT kind, status, COUNT(*) FROM character_experience_scene_log WHERE bot_id=? GROUP BY kind, status",
            (str(bot_id or ""),),
        ).fetchall():
            out.setdefault(str(kind), {})[str(status)] = int(count)
        return out

    # ─── 一次性迁移 ───

    def get_migration(self, key: str) -> dict[str, Any] | None:
        row = self.cm.execute_read(
            "SELECT key, status, report, applied_at FROM character_experience_migrations WHERE key=?", (str(key),),
        ).fetchone()
        if row is None:
            return None
        return {"key": row[0], "status": row[1], "report": _loads(row[2], {}), "applied_at": float(row[3])}

    def list_migrations(self) -> list[dict[str, Any]]:
        rows = self.cm.execute_read(
            "SELECT key, status, report, applied_at FROM character_experience_migrations ORDER BY applied_at",
        ).fetchall()
        return [{"key": r[0], "status": r[1], "report": _loads(r[2], {}), "applied_at": float(r[3])} for r in rows]

    def put_migration(self, key: str, status: str, report: Mapping[str, Any], *, now: float | None = None) -> None:
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            tx.execute(
                """INSERT INTO character_experience_migrations (key, status, report, applied_at) VALUES (?, ?, ?, ?)
                   ON CONFLICT(key) DO UPDATE SET status=excluded.status, report=excluded.report, applied_at=excluded.applied_at""",
                (str(key), str(status), _dumps(dict(report)), stamp),
            )

    def import_legacy(
        self,
        bot_id: str,
        items: Sequence[Mapping[str, Any]],
        stages: Sequence[Mapping[str, Any]],
        *,
        created_by: str = "migration",
        now: float | None = None,
    ) -> dict[str, list[int]]:
        """迁移：阶段与经历在一个事务里写入（经历按正文去重，已有的不动）。"""
        stamp = float(now if now is not None else time.time())
        cleaned = [self.clean_item(item) for item in items]
        with self.cm.write_transaction() as tx:
            self.ensure_stages(bot_id, stages, now=stamp, _tx=tx)
            return self.add_many(bot_id, cleaned, created_by=created_by, now=stamp, _tx=tx)


__all__ = [
    "CharacterExperienceConflict",
    "CharacterExperienceError",
    "CharacterExperienceRepo",
    "MAX_CONTENT_CHARS",
]
