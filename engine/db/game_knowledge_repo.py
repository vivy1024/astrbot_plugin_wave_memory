"""游戏知识库仓储：游戏库、知识卡、词条、经验反思进度（自有小表，不经 DomainCommand，不进 outbox）。

- ``version`` 做乐观锁：编辑带上读到的版本号，对不上就拒绝（与关系小传、Bot Profile 相同写法）；
- 知识卡 / 词条的中文检索索引（``fts_game_cards`` / ``fts_game_terms``）与正文在同一个写事务里维护，
  切词沿用 ``fts_cjk``（中日韩字串切相邻两字，字母数字按词小写）；
- 导入在一个事务里完成：已有同键条目按 ``skip`` / ``update`` / ``merge`` 处理，已有条目的状态不变；
- 知识卡的 ``bot_id``：空串 = 所有 Bot 共用的攻略，否则是这个 Bot 自己的经验卡（导入、导出、阶段统计都只看共用的）；
- 经验反思读记忆表里的游戏经历（``source='game_event'``，只读），进度记在 ``game_lesson_runs``。

字段规整与校验在 :mod:`domain.game_knowledge`，这里只负责落库与读取。
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Iterable, Mapping, Sequence
from typing import Any

from . import fts_cjk
from .connection import ConnectionManager
from .migrations.game_knowledge import ensure_game_knowledge_schema

try:
    from ...domain.game_knowledge import (
        LIST_MAX,
        STATUSES,
        GameKnowledgeConflict,
        GameKnowledgeError,
        auto_item_key,
        fold,
        normalize_card_fields,
        normalize_game_fields,
        normalize_term_fields,
        parse_conditions,
        validate_game_id,
        validate_status,
    )
except ImportError:  # pragma: no cover - 仓库测试以插件根目录为顶层
    from domain.game_knowledge import (
        LIST_MAX,
        STATUSES,
        GameKnowledgeConflict,
        GameKnowledgeError,
        auto_item_key,
        fold,
        normalize_card_fields,
        normalize_game_fields,
        normalize_term_fields,
        parse_conditions,
        validate_game_id,
        validate_status,
    )

IMPORT_MODES = ("skip", "update", "merge")
LIST_LIMIT_MAX = 200

_GAME_COLUMNS = "game_id, name, aliases, description, enabled, version, created_at, updated_at, lessons_enabled, state_keys"
_CARD_COLUMNS = (
    "id, game_id, card_key, title, stage, points, pitfalls, keywords, conditions, source, source_url, "
    "source_type, status, version, note, meta, created_at, updated_at, bot_id"
)
_RUN_COLUMNS = (
    "id, bot_id, game_id, trigger, status, started_at, finished_at, from_memory_id, to_memory_id, events, "
    "model_calls, counts, error"
)
# 直播请求里见过的状态键最多记这么多个（写经验卡的条件时给模型看）
STATE_KEYS_MAX = 40
_TERM_COLUMNS = (
    "id, game_id, term_key, term, aliases, explanation, related, source, source_url, source_type, status, "
    "version, note, meta, created_at, updated_at"
)
_CARD_CONTENT = ("title", "stage", "points", "pitfalls", "keywords", "conditions", "source", "source_url", "source_type", "note", "meta")
_TERM_CONTENT = ("term", "aliases", "explanation", "related", "source", "source_url", "source_type", "note", "meta")
_JSON_FIELDS = frozenset({"aliases", "points", "pitfalls", "keywords", "conditions", "related", "meta"})
_LIST_FIELDS = frozenset({"aliases", "points", "pitfalls", "keywords", "related"})


def _loads(raw: Any, default: Any) -> Any:
    if raw in (None, ""):
        return default
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return default
    return value if isinstance(value, type(default)) else default


def _dumps(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=isinstance(value, dict))


def _game_row(row: Any) -> dict[str, Any]:
    return {
        "id": str(row[0]),
        "name": str(row[1] or row[0]),
        "aliases": _loads(row[2], []),
        "description": str(row[3] or ""),
        "enabled": bool(row[4]),
        "version": int(row[5] or 1),
        "created_at": float(row[6] or 0.0),
        "updated_at": float(row[7] or 0.0),
        "lessons_enabled": bool(row[8]) if len(row) > 8 and row[8] is not None else True,
        "state_keys": _loads(row[9], {}) if len(row) > 9 else {},
    }


def _card_row(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "game_id": str(row[1]),
        "key": str(row[2]),
        "title": str(row[3] or ""),
        "stage": str(row[4] or ""),
        "points": _loads(row[5], []),
        "pitfalls": _loads(row[6], []),
        "keywords": _loads(row[7], []),
        "conditions": _loads(row[8], []),
        "source": str(row[9] or ""),
        "source_url": str(row[10] or ""),
        "source_type": str(row[11] or "manual"),
        "status": str(row[12] or "draft"),
        "version": int(row[13] or 1),
        "note": str(row[14] or ""),
        "meta": _loads(row[15], {}),
        "created_at": float(row[16] or 0.0),
        "updated_at": float(row[17] or 0.0),
        "bot_id": str(row[18] or "") if len(row) > 18 else "",
    }


def _run_row(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "bot_id": str(row[1] or ""),
        "game_id": str(row[2] or ""),
        "trigger": str(row[3] or ""),
        "status": str(row[4] or ""),
        "started_at": float(row[5] or 0.0),
        "finished_at": float(row[6]) if row[6] is not None else None,
        "from_memory_id": int(row[7] or 0),
        "to_memory_id": int(row[8] or 0),
        "events": int(row[9] or 0),
        "model_calls": int(row[10] or 0),
        "counts": _loads(row[11], {}),
        "error": str(row[12] or ""),
    }


def _term_row(row: Any) -> dict[str, Any]:
    return {
        "id": int(row[0]),
        "game_id": str(row[1]),
        "key": str(row[2]),
        "term": str(row[3] or ""),
        "aliases": _loads(row[4], []),
        "explanation": str(row[5] or ""),
        "related": _loads(row[6], []),
        "source": str(row[7] or ""),
        "source_url": str(row[8] or ""),
        "source_type": str(row[9] or "manual"),
        "status": str(row[10] or "draft"),
        "version": int(row[11] or 1),
        "note": str(row[12] or ""),
        "meta": _loads(row[13], {}),
        "created_at": float(row[14] or 0.0),
        "updated_at": float(row[15] or 0.0),
    }


def card_search_text(card: Mapping[str, Any]) -> str:
    """进检索索引的正文：标题、阶段、要点、注意、关键词、出处。"""
    parts = [card.get("title"), card.get("stage"), *(card.get("points") or []), *(card.get("pitfalls") or []),
             *(card.get("keywords") or []), card.get("source")]
    return "\n".join(str(part) for part in parts if part)


def term_search_text(term: Mapping[str, Any]) -> str:
    parts = [term.get("term"), *(term.get("aliases") or []), term.get("explanation"), *(term.get("related") or [])]
    return "\n".join(str(part) for part in parts if part)


def _merge_lists(incoming: Sequence[Any], existing: Sequence[Any]) -> list[Any]:
    """本次导入的条目在前，旧条目里本次没有的保留在后面（按规整形去重）。"""
    merged: list[Any] = []
    seen: set[str] = set()
    for item in [*incoming, *existing]:
        marker = fold(item)
        if not marker or marker in seen:
            continue
        seen.add(marker)
        merged.append(item)
        if len(merged) >= LIST_MAX:
            break
    return merged


def _merge_conditions(incoming: Sequence[Mapping[str, Any]], existing: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """本次导入的条件为准；旧条件里本次没涉及的键保留。"""
    incoming_keys = {str(item.get("key") or "").casefold() for item in incoming}
    kept = [dict(item) for item in existing if str(item.get("key") or "").casefold() not in incoming_keys]
    return [dict(item) for item in incoming] + kept


class GameKnowledgeRepo:
    def __init__(self, cm: ConnectionManager, *, ensure_schema: bool = True):
        self.cm = cm
        if ensure_schema:
            ensure_game_knowledge_schema(cm)

    # ─────────────────────────── 读取工具 ───────────────────────────

    def _read(self, sql: str, params: Sequence[Any] = ()):
        return self.cm.execute_read(sql, tuple(params))

    # ─────────────────────────── 游戏库 ───────────────────────────

    def list_games(self) -> list[dict[str, Any]]:
        rows = self._read(f"SELECT {_GAME_COLUMNS} FROM game_libraries ORDER BY enabled DESC, name, game_id").fetchall()
        games = [_game_row(row) for row in rows]
        counts = self.counts()
        for game in games:
            game["counts"] = counts.get(game["id"], _empty_counts())
        return games

    def counts(self, game_id: str | None = None) -> dict[str, dict[str, dict[str, int]]]:
        """各状态计数：``cards`` 只算共用的攻略卡，``lessons`` 是各 Bot 的经验卡合计。"""
        result: dict[str, dict[str, dict[str, int]]] = {}
        for table, kind, owner in (
            ("game_knowledge_cards", "cards", "bot_id=''"),
            ("game_knowledge_cards", "lessons", "bot_id<>''"),
            ("game_knowledge_terms", "terms", ""),
        ):
            where, params = [owner] if owner else [], []
            if game_id is not None:
                where.append("game_id=?")
                params.append(game_id)
            sql = f"SELECT game_id, status, COUNT(*) FROM {table}" + (f" WHERE {' AND '.join(where)}" if where else "")
            for gid, status, count in self._read(sql + " GROUP BY game_id, status", params).fetchall():
                bucket = result.setdefault(str(gid), _empty_counts())
                bucket[kind][str(status)] = int(count)
        return result

    def get_game(self, game_id: str) -> dict[str, Any] | None:
        try:
            gid = validate_game_id(game_id)
        except GameKnowledgeError:
            return None
        row = self._read(f"SELECT {_GAME_COLUMNS} FROM game_libraries WHERE game_id=?", (gid,)).fetchone()
        if row is None:
            return None
        game = _game_row(row)
        game["counts"] = self.counts(gid).get(gid, _empty_counts())
        return game

    def create_game(self, payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
        fields = normalize_game_fields(payload)
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            if tx.execute("SELECT 1 FROM game_libraries WHERE game_id=?", (fields["game_id"],)).fetchone():
                raise GameKnowledgeConflict("game_exists", message=f"游戏库 {fields['game_id']} 已存在")
            tx.execute(
                """INSERT INTO game_libraries (game_id, name, aliases, description, enabled, version, created_at, updated_at,
                                               lessons_enabled)
                   VALUES (?, ?, ?, ?, ?, 1, ?, ?, ?)""",
                (fields["game_id"], fields["name"], _dumps(fields["aliases"]), fields["description"],
                 1 if fields["enabled"] else 0, stamp, stamp, 1 if fields.get("lessons_enabled", True) else 0),
            )
        game = self.get_game(fields["game_id"])
        assert game is not None
        return game

    def update_game(self, game_id: str, payload: Mapping[str, Any], *, expected_version: int | None, now: float | None = None) -> dict[str, Any]:
        gid = validate_game_id(game_id)
        fields = normalize_game_fields({k: v for k, v in payload.items() if k not in {"id", "game_id"}}, partial=True)
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            row = tx.execute(f"SELECT {_GAME_COLUMNS} FROM game_libraries WHERE game_id=?", (gid,)).fetchone()
            if row is None:
                raise GameKnowledgeError("game_not_found", f"没有游戏库 {gid}")
            current = _game_row(row)
            if expected_version is not None and int(expected_version) != current["version"]:
                raise GameKnowledgeConflict("game_version_conflict", expected=int(expected_version), actual=current["version"])
            changed = {name: value for name, value in fields.items() if current.get(name if name != "game_id" else "id") != value}
            if changed:
                assignments = ", ".join(f"{name}=?" for name in changed)
                values = [
                    _dumps(value) if name in _JSON_FIELDS else (1 if value else 0) if name in {"enabled", "lessons_enabled"} else value
                    for name, value in changed.items()
                ]
                tx.execute(
                    f"UPDATE game_libraries SET {assignments}, version=version+1, updated_at=? WHERE game_id=?",
                    (*values, stamp, gid),
                )
        game = self.get_game(gid)
        assert game is not None
        return game

    def require_game(self, game_id: str) -> dict[str, Any]:
        game = self.get_game(game_id)
        if game is None:
            raise GameKnowledgeError("game_not_found", f"没有游戏库 {game_id}")
        return game

    # ─────────────────────────── 知识卡 / 词条：通用 ───────────────────────────

    def _spec(self, kind: str) -> tuple[str, str, str, str, Any, tuple[str, ...]]:
        if kind == "card":
            return "game_knowledge_cards", "card_key", _CARD_COLUMNS, "fts_game_cards", _card_row, _CARD_CONTENT
        if kind == "term":
            return "game_knowledge_terms", "term_key", _TERM_COLUMNS, "fts_game_terms", _term_row, _TERM_CONTENT
        raise ValueError(f"unknown kind: {kind}")

    @staticmethod
    def _search_text(kind: str, item: Mapping[str, Any]) -> str:
        return card_search_text(item) if kind == "card" else term_search_text(item)

    def _sync_fts(self, tx, kind: str, item: Mapping[str, Any]) -> None:
        _, _, _, fts_table, _, _ = self._spec(kind)
        tx.execute(f"DELETE FROM {fts_table} WHERE rowid=?", (int(item["id"]),))
        tokens = fts_cjk.token_text(self._search_text(kind, item))
        if tokens:
            tx.execute(f"INSERT INTO {fts_table}(rowid, tokens) VALUES (?, ?)", (int(item["id"]), tokens))

    def _get(self, kind: str, item_id: int, *, game_id: str | None = None, connection=None) -> dict[str, Any] | None:
        table, _, columns, _, to_item, _ = self._spec(kind)
        sql = f"SELECT {columns} FROM {table} WHERE id=?"
        params: list[Any] = [int(item_id)]
        if game_id is not None:
            sql += " AND game_id=?"
            params.append(game_id)
        reader = connection.execute if connection is not None else self.cm.execute_read
        row = reader(sql, tuple(params)).fetchone()
        return to_item(row) if row else None

    def _get_by_key(self, tx, kind: str, game_id: str, key: str) -> dict[str, Any] | None:
        table, key_column, columns, _, to_item, _ = self._spec(kind)
        row = tx.execute(f"SELECT {columns} FROM {table} WHERE game_id=? AND {key_column}=?", (game_id, key)).fetchone()
        return to_item(row) if row else None

    def _normalize(self, kind: str, payload: Mapping[str, Any], *, partial: bool = False) -> dict[str, Any]:
        return normalize_card_fields(payload, partial=partial) if kind == "card" else normalize_term_fields(payload, partial=partial)

    def _insert(self, tx, kind: str, game_id: str, fields: Mapping[str, Any], *, status: str, now: float, bot_id: str = "") -> dict[str, Any]:
        table, key_column, _, _, _, content = self._spec(kind)
        key = fields.get(key_column) or auto_item_key("c" if kind == "card" else "t", fields["title" if kind == "card" else "term"])
        values = {name: fields.get(name, _default(name)) for name in content}
        names = ["game_id", key_column, *content, "status", "version", "created_at", "updated_at"]
        params = [game_id, key, *(_dumps(values[n]) if n in _JSON_FIELDS else values[n] for n in content), status, 1, now, now]
        if kind == "card" and bot_id:
            names.append("bot_id")
            params.append(str(bot_id))
        try:
            cursor = tx.execute(
                f"INSERT INTO {table} ({', '.join(names)}) VALUES ({', '.join('?' for _ in names)})",
                params,
            )
        except sqlite3.IntegrityError as exc:
            raise GameKnowledgeConflict(f"{kind}_key_exists", message=f"同一个游戏里已有键为 {key} 的条目") from exc
        item = self._get(kind, int(cursor.lastrowid), connection=tx)
        assert item is not None
        self._sync_fts(tx, kind, item)
        return item

    def _write_content(self, tx, kind: str, current: Mapping[str, Any], fields: Mapping[str, Any], *, now: float) -> tuple[dict[str, Any], bool]:
        table, _, _, _, _, content = self._spec(kind)
        changed = {name: fields[name] for name in content if name in fields and fields[name] != current.get(name)}
        if not changed:
            return dict(current), False
        assignments = ", ".join(f"{name}=?" for name in changed)
        values = [_dumps(value) if name in _JSON_FIELDS else value for name, value in changed.items()]
        tx.execute(
            f"UPDATE {table} SET {assignments}, version=version+1, updated_at=? WHERE id=?",
            (*values, now, int(current["id"])),
        )
        item = self._get(kind, int(current["id"]), connection=tx)
        assert item is not None
        self._sync_fts(tx, kind, item)
        return item, True

    def create_item(self, kind: str, game_id: str, payload: Mapping[str, Any], *, now: float | None = None) -> dict[str, Any]:
        gid = validate_game_id(game_id)
        fields = self._normalize(kind, payload)
        status = fields.pop("status", "draft")
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            if not tx.execute("SELECT 1 FROM game_libraries WHERE game_id=?", (gid,)).fetchone():
                raise GameKnowledgeError("game_not_found", f"没有游戏库 {gid}")
            return self._insert(tx, kind, gid, fields, status=status, now=stamp)

    def update_item(
        self,
        kind: str,
        item_id: int,
        payload: Mapping[str, Any],
        *,
        expected_version: int | None,
        game_id: str | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """编辑正文（``status`` 也可以一起改）；版本号对不上抛 ``GameKnowledgeConflict``。"""
        fields = self._normalize(kind, {k: v for k, v in payload.items() if k not in {"key", "card_key", "term_key"}}, partial=True)
        status = fields.pop("status", None)
        stamp = float(now if now is not None else time.time())
        with self.cm.write_transaction() as tx:
            current = self._get(kind, item_id, game_id=game_id, connection=tx)
            if current is None:
                raise GameKnowledgeError(f"{kind}_not_found", "找不到这个条目")
            if expected_version is not None and int(expected_version) != current["version"]:
                raise GameKnowledgeConflict(f"{kind}_version_conflict", expected=int(expected_version), actual=current["version"])
            merged = {**current, **fields}
            if kind == "card" and not merged.get("points") and not merged.get("pitfalls"):
                raise GameKnowledgeError("card_content_required", "知识卡至少要有一条要点或注意")
            if kind == "term" and not merged.get("explanation"):
                raise GameKnowledgeError("term_explanation_required", "词条的解释不能为空")
            item, changed = self._write_content(tx, kind, current, fields, now=stamp)
            if status is not None and status != item["status"]:
                tx.execute(
                    f"UPDATE {self._spec(kind)[0]} SET status=?, version=version+{0 if changed else 1}, updated_at=? WHERE id=?",
                    (status, stamp, int(item["id"])),
                )
                item = self._get(kind, int(item["id"]), connection=tx) or item
            return item

    def set_status(
        self,
        kind: str,
        game_id: str,
        item_ids: Iterable[Any],
        status: str,
        *,
        versions: Mapping[Any, Any] | None = None,
        now: float | None = None,
    ) -> dict[str, Any]:
        """批量启用 / 归档 / 退回草稿。带 ``versions`` 时逐条核对版本，对不上的跳过并列在 conflicts 里。"""
        gid = validate_game_id(game_id)
        target = validate_status(status)
        table = self._spec(kind)[0]
        wanted: list[int] = []
        for raw in item_ids or ():
            try:
                number = int(raw)
            except (TypeError, ValueError):
                continue
            if number not in wanted:
                wanted.append(number)
        expected = {int(k): int(v) for k, v in (versions or {}).items() if str(k).lstrip("-").isdigit() and v is not None}
        stamp = float(now if now is not None else time.time())
        result: dict[str, list[Any]] = {"changed": [], "unchanged": [], "missing": [], "conflicts": []}
        with self.cm.write_transaction() as tx:
            for item_id in wanted:
                row = tx.execute(f"SELECT status, version FROM {table} WHERE id=? AND game_id=?", (item_id, gid)).fetchone()
                if row is None:
                    result["missing"].append(item_id)
                    continue
                current_status, current_version = str(row[0]), int(row[1] or 1)
                if item_id in expected and expected[item_id] != current_version:
                    result["conflicts"].append({"id": item_id, "expected": expected[item_id], "actual": current_version})
                    continue
                if current_status == target:
                    result["unchanged"].append(item_id)
                    continue
                tx.execute(
                    f"UPDATE {table} SET status=?, version=version+1, updated_at=? WHERE id=?",
                    (target, stamp, item_id),
                )
                result["changed"].append(item_id)
        return {"status": target, **result}

    def get_item(self, kind: str, item_id: int, *, game_id: str | None = None) -> dict[str, Any] | None:
        return self._get(kind, item_id, game_id=game_id)

    def get_item_by_key(self, kind: str, game_id: str, key: str) -> dict[str, Any] | None:
        table, key_column, columns, _, to_item, _ = self._spec(kind)
        row = self._read(f"SELECT {columns} FROM {table} WHERE game_id=? AND {key_column}=?", (game_id, key)).fetchone()
        return to_item(row) if row else None

    def list_items(
        self,
        kind: str,
        game_id: str,
        *,
        status: str | Sequence[str] | None = None,
        stage: str | None = None,
        query: str = "",
        source_type: str | None = None,
        bot_id: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> dict[str, Any]:
        """``bot_id``（只对知识卡）：None = 全部；空串 = 共用的攻略；某个 Bot = 它自己的经验卡。"""
        table, key_column, columns, fts_table, to_item, _ = self._spec(kind)
        gid = validate_game_id(game_id)
        where, params = ["game_id=?"], [gid]
        if kind == "card" and bot_id is not None:
            where.append("bot_id=?")
            params.append(str(bot_id))
        statuses = [status] if isinstance(status, str) else list(status or [])
        statuses = [validate_status(s) for s in statuses if s]
        if statuses:
            where.append(f"status IN ({','.join('?' for _ in statuses)})")
            params += statuses
        if stage is not None and kind == "card":
            where.append("stage=?")
            params.append(stage)
        if source_type:
            where.append("source_type=?")
            params.append(source_type)
        text = str(query or "").strip()
        if text:
            title_column = "title" if kind == "card" else "term"
            escaped = text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like = f"%{escaped}%"
            clauses = [f"{key_column} LIKE ? ESCAPE '\\'", f"{title_column} LIKE ? ESCAPE '\\'"]
            clause_params: list[Any] = [like, like]
            expr = fts_cjk.match_expr(text.split())
            if expr:
                clauses.append(f"id IN (SELECT rowid FROM {fts_table} WHERE {fts_table} MATCH ?)")
                clause_params.append(expr)
            else:
                # 单个汉字等切不出两字片段的查询：退回正文 LIKE（每个游戏只有几百条，代价很小）
                body_column = "points" if kind == "card" else "explanation"
                clauses.append(f"{body_column} LIKE ? ESCAPE '\\'")
                clause_params.append(like)
            where.append("(" + " OR ".join(clauses) + ")")
            params += clause_params
        sql_where = " AND ".join(where)
        total = int(self._read(f"SELECT COUNT(*) FROM {table} WHERE {sql_where}", params).fetchone()[0])
        size = max(1, min(int(limit), LIST_LIMIT_MAX))
        order = "status='active' DESC, status='draft' DESC, stage, id" if kind == "card" else "status='active' DESC, status='draft' DESC, term, id"
        rows = self._read(
            f"SELECT {columns} FROM {table} WHERE {sql_where} ORDER BY {order} LIMIT ? OFFSET ?",
            [*params, size, max(0, int(offset))],
        ).fetchall()
        return {"items": [to_item(row) for row in rows], "total": total, "limit": size, "offset": max(0, int(offset))}

    def stage_facets(self, game_id: str) -> list[dict[str, Any]]:
        rows = self._read(
            "SELECT stage, COUNT(*) FROM game_knowledge_cards WHERE game_id=? AND bot_id='' GROUP BY stage ORDER BY MIN(id)",
            (validate_game_id(game_id),),
        ).fetchall()
        return [{"name": str(row[0] or ""), "count": int(row[1])} for row in rows]

    def active_items(self, kind: str, game_id: str) -> list[dict[str, Any]]:
        table, _, columns, _, to_item, _ = self._spec(kind)
        rows = self._read(f"SELECT {columns} FROM {table} WHERE game_id=? AND status='active' ORDER BY id", (game_id,)).fetchall()
        return [to_item(row) for row in rows]

    def all_items(
        self, kind: str, game_id: str, *, statuses: Sequence[str] | None = None, bot_id: str | None = None,
    ) -> list[dict[str, Any]]:
        """``bot_id``（只对知识卡）：None = 共用的攻略与所有 Bot 的经验卡；空串 = 只要共用的；某个 Bot = 只要它的。"""
        table, _, columns, _, to_item, _ = self._spec(kind)
        where, params = "game_id=?", [game_id]
        if kind == "card" and bot_id is not None:
            where += " AND bot_id=?"
            params.append(str(bot_id))
        if statuses:
            where += f" AND status IN ({','.join('?' for _ in statuses)})"
            params += list(statuses)
        rows = self._read(f"SELECT {columns} FROM {table} WHERE {where} ORDER BY id", params).fetchall()
        return [to_item(row) for row in rows]

    def search_ids(self, kind: str, game_id: str, expr: str, *, statuses: Sequence[str] = ("active",), limit: int = 20) -> list[tuple[int, float]]:
        """中文检索：``expr`` 是 ``fts_cjk.match_expr`` 的结果；返回 (id, bm25)，越小越相关。"""
        if not expr:
            return []
        table, _, _, fts_table, _, _ = self._spec(kind)
        marks = ",".join("?" for _ in statuses)
        rows = self._read(
            f"""SELECT t.id, bm25({fts_table}) AS rank FROM {fts_table} JOIN {table} AS t ON t.id = {fts_table}.rowid
                 WHERE {fts_table} MATCH ? AND t.game_id=? AND t.status IN ({marks})
                 ORDER BY rank LIMIT ?""",
            (expr, game_id, *statuses, max(1, int(limit))),
        ).fetchall()
        return [(int(row[0]), float(row[1])) for row in rows]

    def revision(self, game_id: str) -> tuple[Any, ...]:
        """缓存用的数据指纹：库本身、启用中的卡与词条有任何改动都会变。"""
        row = self._read(
            """SELECT
                 (SELECT version || ':' || enabled FROM game_libraries WHERE game_id=?),
                 (SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), 0) FROM game_knowledge_cards WHERE game_id=?),
                 (SELECT COUNT(*) || ':' || COALESCE(MAX(updated_at), 0) FROM game_knowledge_terms WHERE game_id=?)""",
            (game_id, game_id, game_id),
        ).fetchone()
        return tuple(row) if row else ()

    # ─────────────────────────── 直播状态键 ───────────────────────────

    def note_state_keys(self, game_id: str, state: Mapping[str, Any], *, now: float | None = None) -> bool:
        """记下直播请求里出现过的状态键与一个示例值（只在出现新键时写一次，不改版本号）。"""
        gid = validate_game_id(game_id)
        with self.cm.write_transaction() as tx:
            row = tx.execute("SELECT state_keys FROM game_libraries WHERE game_id=?", (gid,)).fetchone()
            if row is None:
                return False
            known = _loads(row[0], {})
            added = False
            for key, value in state.items():
                name = str(key)
                if name in known or len(known) >= STATE_KEYS_MAX:
                    continue
                known[name] = {"example": str(value)[:24], "seen_at": float(now if now is not None else time.time())}
                added = True
            if added:
                tx.execute("UPDATE game_libraries SET state_keys=? WHERE game_id=?", (_dumps(known), gid))
            return added

    def ensure_game(self, game_id: str, name: str, *, now: float | None = None) -> dict[str, Any]:
        """没有这个游戏库就按标识和显示名新建一个（经验卡要落在某个库里）；已有时原样返回。"""
        game = self.get_game(game_id)
        if game is not None:
            return game
        try:
            return self.create_game({"id": game_id, "name": name or game_id}, now=now)
        except GameKnowledgeConflict:
            game = self.get_game(game_id)
            assert game is not None
            return game

    # ─────────────────────────── 经验卡与反思进度 ───────────────────────────

    def game_events(self, bot_id: str, *, after_id: int, limit: int) -> list[dict[str, Any]]:
        """这个 Bot 在记忆表位置 ``after_id`` 之后的游戏经历（Cortico 写回的 source=game_event，按写入顺序）。"""
        rows = self._read(
            """SELECT id, session_id, visibility, content, timestamp, provenance FROM memories
                WHERE id > ? AND bot_id=? AND source='game_event' AND COALESCE(quarantine, 0)=0
                  AND COALESCE(resolution_state, '') <> 'deleted'
                ORDER BY id LIMIT ?""",
            (int(after_id), str(bot_id), max(1, int(limit))),
        ).fetchall()
        events = []
        for row in rows:
            try:
                meta = json.loads(row[5] or "{}").get("metadata") or {}
            except (TypeError, ValueError, AttributeError):
                meta = {}
            origin = str(meta.get("origin_kind") or "")
            parts = origin.split(":")
            game_id, event_type = (parts[2], parts[3]) if len(parts) >= 4 and origin.startswith("runtime:game_event:") else ("", "")
            events.append({
                "memory_id": int(row[0]),
                "session_id": str(row[1] or ""),
                "visibility": str(row[2] or ""),
                "content": str(row[3] or ""),
                "timestamp": float(row[4] or 0.0),
                "game_id": game_id,
                "event_type": event_type,
            })
        return events

    def max_memory_id(self) -> int:
        row = self._read("SELECT COALESCE(MAX(id), 0) FROM memories").fetchone()
        return int(row[0] or 0) if row else 0

    def around_event(
        self, bot_id: str, session_id: str, visibility: str, *, start: float, end: float, limit: int = 30,
    ) -> list[dict[str, Any]]:
        """一段游戏经历前后，同一会话里她说的话与别人（弹幕、群友）说的话；不含游戏经历本身。"""
        rows = self._read(
            """SELECT sender_id, content, timestamp FROM memories
                WHERE bot_id=? AND session_id=? AND visibility=? AND timestamp >= ? AND timestamp <= ?
                  AND COALESCE(source, '') <> 'game_event' AND COALESCE(quarantine, 0)=0
                  AND COALESCE(resolution_state, '') <> 'deleted'
                ORDER BY timestamp, id LIMIT ?""",
            (str(bot_id), str(session_id), str(visibility), float(start), float(end), max(1, int(limit))),
        ).fetchall()
        return [{"self": str(row[0] or "") == "bot", "content": str(row[1] or ""), "timestamp": float(row[2] or 0.0)} for row in rows]

    def lesson_cursor(self, bot_id: str) -> int:
        """这个 Bot 的游戏经历已经想到记忆表的哪个位置（成功或跳过的反思里最大的 to_memory_id）。"""
        row = self._read(
            "SELECT COALESCE(MAX(to_memory_id), 0) FROM game_lesson_runs WHERE bot_id=? AND status IN ('succeeded', 'skipped')",
            (str(bot_id),),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def claim_lesson_run(
        self, bot_id: str, *, trigger: str, from_memory_id: int, now: float, stale_after: float,
    ) -> int | None:
        """开始一次反思；同一个 Bot 已有进行中的（且没超时）就返回 None。超时的记为失败。"""
        with self.cm.write_transaction() as tx:
            running = tx.execute(
                "SELECT id, started_at FROM game_lesson_runs WHERE bot_id=? AND status='running' ORDER BY id DESC",
                (str(bot_id),),
            ).fetchall()
            for run_id, started_at in running:
                if now - float(started_at or 0.0) < stale_after:
                    return None
                tx.execute(
                    "UPDATE game_lesson_runs SET status='failed', finished_at=?, error='stale' WHERE id=?",
                    (now, int(run_id)),
                )
            cursor = tx.execute(
                """INSERT INTO game_lesson_runs (bot_id, trigger, status, started_at, from_memory_id, to_memory_id)
                   VALUES (?, ?, 'running', ?, ?, ?)""",
                (str(bot_id), str(trigger), now, int(from_memory_id), int(from_memory_id)),
            )
            return int(cursor.lastrowid)

    def finish_lesson_run(
        self,
        run_id: int,
        *,
        status: str,
        now: float,
        to_memory_id: int | None = None,
        game_id: str = "",
        events: int = 0,
        model_calls: int = 0,
        counts: Mapping[str, Any] | None = None,
        error: str = "",
    ) -> None:
        with self.cm.write_transaction() as tx:
            tx.execute(
                """UPDATE game_lesson_runs
                      SET status=?, finished_at=?, to_memory_id=COALESCE(?, to_memory_id), game_id=?, events=?,
                          model_calls=?, counts=?, error=?
                    WHERE id=?""",
                (status, now, None if to_memory_id is None else int(to_memory_id), str(game_id or ""), int(events),
                 int(model_calls), _dumps(dict(counts or {})), str(error or "")[:500], int(run_id)),
            )

    def lesson_runs(
        self, *, bot_id: str | None = None, game_id: str | None = None, limit: int = 20, meaningful: bool = True,
    ) -> list[dict[str, Any]]:
        """最近的反思记录；``meaningful`` 时去掉「没读到经历」的空跑（它们只用来推进游标）。"""
        where, params = (["(events > 0 OR status IN ('failed', 'running'))"] if meaningful else []), []
        if bot_id is not None:
            where.append("bot_id=?")
            params.append(str(bot_id))
        if game_id is not None:
            # 一次反思可能同时想了几个游戏：game_id 记的是逗号分隔的列表
            where.append("(',' || game_id || ',') LIKE ?")
            params.append(f"%,{game_id},%")
        sql = f"SELECT {_RUN_COLUMNS} FROM game_lesson_runs" + (f" WHERE {' AND '.join(where)}" if where else "")
        rows = self._read(sql + " ORDER BY id DESC LIMIT ?", (*params, max(1, int(limit)))).fetchall()
        return [_run_row(row) for row in rows]

    def lesson_model_calls_since(self, bot_id: str, since: float) -> int:
        row = self._read(
            "SELECT COALESCE(SUM(model_calls), 0) FROM game_lesson_runs WHERE bot_id=? AND started_at >= ?",
            (str(bot_id), float(since)),
        ).fetchone()
        return int(row[0] or 0) if row else 0

    def lesson_bot_ids(self) -> list[str]:
        """写过经验卡、或有过反思记录的 Bot。"""
        rows = self._read(
            """SELECT bot_id FROM game_knowledge_cards WHERE bot_id<>''
               UNION SELECT bot_id FROM game_lesson_runs WHERE bot_id<>''"""
        ).fetchall()
        return sorted({str(row[0]) for row in rows if row[0]})

    def apply_lessons(
        self,
        game_id: str,
        bot_id: str,
        *,
        creates: Sequence[Mapping[str, Any]] = (),
        updates: Sequence[tuple[int, int, Mapping[str, Any]]] = (),
        archives: Sequence[tuple[int, int]] = (),
        now: float | None = None,
    ) -> dict[str, list[int]]:
        """在一个事务里写一次反思的结果。``updates`` / ``archives`` 是 (卡 id, 读到的版本, …)：
        版本对不上（反思期间管理员改过）就跳过这一条，列在 conflicts 里。"""
        gid = validate_game_id(game_id)
        stamp = float(now if now is not None else time.time())
        result: dict[str, list[int]] = {"created": [], "updated": [], "archived": [], "conflicts": []}
        with self.cm.write_transaction() as tx:
            if not tx.execute("SELECT 1 FROM game_libraries WHERE game_id=?", (gid,)).fetchone():
                raise GameKnowledgeError("game_not_found", f"没有游戏库 {gid}")
            for card_id, version, fields in updates:
                current = self._get("card", card_id, game_id=gid, connection=tx)
                if current is None or current.get("bot_id") != bot_id or current["version"] != int(version):
                    result["conflicts"].append(int(card_id))
                    continue
                normalized = normalize_card_fields(fields, partial=True)
                status = normalized.pop("status", None)
                item, changed = self._write_content(tx, "card", current, normalized, now=stamp)
                if status is not None and status != item["status"]:
                    tx.execute(
                        f"UPDATE game_knowledge_cards SET status=?, version=version+{0 if changed else 1}, updated_at=? WHERE id=?",
                        (status, stamp, int(item["id"])),
                    )
                    changed = True
                if changed:
                    result["updated"].append(int(item["id"]))
            for card_id, version in archives:
                row = tx.execute(
                    "SELECT version, bot_id, status FROM game_knowledge_cards WHERE id=? AND game_id=?", (int(card_id), gid),
                ).fetchone()
                if row is None or str(row[1]) != bot_id or int(row[0]) != int(version):
                    result["conflicts"].append(int(card_id))
                    continue
                if str(row[2]) != "archived":
                    tx.execute(
                        "UPDATE game_knowledge_cards SET status='archived', version=version+1, updated_at=? WHERE id=?",
                        (stamp, int(card_id)),
                    )
                    result["archived"].append(int(card_id))
            for raw in creates:
                fields = normalize_card_fields(raw)
                status = fields.pop("status", "active")
                item = self._insert(tx, "card", gid, fields, status=status, now=stamp, bot_id=bot_id)
                result["created"].append(int(item["id"]))
        return result

    # ─────────────────────────── 导入 ───────────────────────────

    def import_items(
        self,
        game_id: str,
        *,
        cards: Sequence[Mapping[str, Any]] = (),
        terms: Sequence[Mapping[str, Any]] = (),
        on_existing: str = "skip",
        now: float | None = None,
    ) -> dict[str, Any]:
        """在一个事务里导入已规整的条目（``normalize_*_fields`` 的结果，带 ``status``）。

        已有同键条目：``skip`` 跳过；``update`` 用本次内容覆盖正文；``merge`` 列表字段合并（本次在前），
        单值字段本次不为空时替换。已有条目的状态都不变；内容没有变化时记为 unchanged。
        """
        gid = validate_game_id(game_id)
        mode = str(on_existing or "skip").strip().lower()
        if mode not in IMPORT_MODES:
            raise GameKnowledgeError("invalid_import_mode", f"已有条目的处理方式只能是 {', '.join(IMPORT_MODES)}")
        stamp = float(now if now is not None else time.time())
        summary = {kind: {"created": 0, "updated": 0, "unchanged": 0, "skipped": 0} for kind in ("cards", "terms")}
        created_ids: dict[str, list[int]] = {"cards": [], "terms": []}
        with self.cm.write_transaction() as tx:
            if not tx.execute("SELECT 1 FROM game_libraries WHERE game_id=?", (gid,)).fetchone():
                raise GameKnowledgeError("game_not_found", f"没有游戏库 {gid}")
            for kind, bucket, items in (("card", "cards", cards), ("term", "terms", terms)):
                key_column = self._spec(kind)[1]
                seen_keys: set[str] = set()
                for raw in items:
                    fields = dict(raw)
                    status = fields.pop("status", "draft")
                    key = fields.get(key_column) or auto_item_key("c" if kind == "card" else "t", fields["title" if kind == "card" else "term"])
                    fields[key_column] = key
                    if key in seen_keys:
                        summary[bucket]["skipped"] += 1
                        continue
                    seen_keys.add(key)
                    current = self._get_by_key(tx, kind, gid, key)
                    if current is None:
                        item = self._insert(tx, kind, gid, fields, status=status, now=stamp)
                        summary[bucket]["created"] += 1
                        created_ids[bucket].append(item["id"])
                        continue
                    if mode == "skip":
                        summary[bucket]["skipped"] += 1
                        continue
                    if mode == "merge":
                        fields = self._merged_fields(kind, current, fields)
                    _, changed = self._write_content(tx, kind, current, fields, now=stamp)
                    summary[bucket]["updated" if changed else "unchanged"] += 1
        return {"game_id": gid, "on_existing": mode, **summary, "created_ids": created_ids}

    def _merged_fields(self, kind: str, current: Mapping[str, Any], incoming: Mapping[str, Any]) -> dict[str, Any]:
        content = self._spec(kind)[5]
        merged: dict[str, Any] = {}
        for name in content:
            if name not in incoming:
                continue
            new, old = incoming[name], current.get(name)
            if name in _LIST_FIELDS:
                merged[name] = _merge_lists(new or [], old or [])
            elif name == "conditions":
                merged[name] = _merge_conditions(new or [], old or [])
            elif name == "meta":
                merged[name] = {**(old or {}), **(new or {})}
            else:
                merged[name] = new if new not in (None, "") else old
        return merged

    # ─────────────────────────── 旧数据 ───────────────────────────

    def legacy_memory_rows(self, source: str, *, limit: int = 5000) -> list[dict[str, Any]]:
        """读取记忆表里某个 ``source`` 的行（只读，供迁移到游戏库）；没有表或列时返回空列表。"""
        try:
            columns = {str(row[1]) for row in self._read("PRAGMA table_info(memories)").fetchall()}
        except sqlite3.Error:
            return []
        if not {"id", "content", "source"} <= columns:
            return []
        where = ["source=?"]
        if "memory_type" in columns:
            where.append("COALESCE(memory_type, 'message') NOT IN ('deleted', 'noise')")
        if "resolution_state" in columns:
            where.append("COALESCE(resolution_state, '') <> 'deleted'")
        timestamp = "timestamp" if "timestamp" in columns else "0"
        rows = self._read(
            f"SELECT id, content, {timestamp} FROM memories WHERE {' AND '.join(where)} ORDER BY id LIMIT ?",
            (str(source), max(1, int(limit))),
        ).fetchall()
        return [{"id": int(row[0]), "content": str(row[1] or ""), "timestamp": float(row[2] or 0.0)} for row in rows]

    def count_legacy_memory_rows(self, source: str) -> int:
        return len(self.legacy_memory_rows(source))

    def rebuild_fts(self, game_id: str | None = None) -> int:
        """重建检索索引（修复用）：按当前正文重写 fts_game_cards / fts_game_terms。"""
        rebuilt = 0
        with self.cm.write_transaction() as tx:
            for kind in ("card", "term"):
                table, _, columns, fts_table, to_item, _ = self._spec(kind)
                sql, params = f"SELECT {columns} FROM {table}", []
                if game_id is not None:
                    sql += " WHERE game_id=?"
                    params.append(validate_game_id(game_id))
                rows = tx.execute(sql, params).fetchall()
                if game_id is None:
                    tx.execute(f"DELETE FROM {fts_table}")
                for row in rows:
                    self._sync_fts(tx, kind, to_item(row))
                    rebuilt += 1
        return rebuilt


def _empty_counts() -> dict[str, dict[str, int]]:
    return {kind: {status: 0 for status in STATUSES} for kind in ("cards", "lessons", "terms")}


def _default(name: str) -> Any:
    if name in _LIST_FIELDS or name == "conditions":
        return []
    if name == "meta":
        return {}
    if name == "source_type":
        return "manual"
    return ""


def validated_conditions(raw: Any) -> list[dict[str, Any]]:
    """给调用方用的便捷函数：条件文字 / 对象 → 存储形式。"""
    return [condition.to_dict() for condition in parse_conditions(raw)]


__all__ = ["GameKnowledgeRepo", "IMPORT_MODES", "STATE_KEYS_MAX", "card_search_text", "term_search_text", "validated_conditions"]
