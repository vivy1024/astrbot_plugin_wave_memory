"""中文可检索的全文索引 ``fts_memories_cjk``：记忆正文按「相邻两字」切词后入 FTS5。

原来的 ``fts_memories`` 用 unicode61 分词，中文连续字串被当成一个词：「张羽师兄最近怎样」
整句一个 token，搜「张羽」只能命中「张羽」单独成词（前后有标点或空格）的行。
线上实测含「张羽」的 2710 条只能搜到 146 条，搜不到时 fts5 通道还会退回对全表做
``LIKE``（4 GB 库约 8 秒）。

这里不依赖词典（jieba 可有可无）：中日韩字符串切成相邻两字（「张羽师兄」→ 张羽 羽师 师兄），
字母数字按词小写。查询用同样的切法，一个词的两字片段组成 FTS5 短语（要求相邻出现），
所以任意两字以上的词都能命中，且不会把「张羽」匹配到「张某羽」。

索引是派生数据：outbox 消费者 ``fts_cjk`` 跟随记忆的新增/修改/删除增量维护，
历史数据由维护任务 ``maintenance.fts_cjk.rebuild`` 分批回填；回填完成前查询方继续用旧索引。
"""

from __future__ import annotations

import re
import sqlite3
import time
from collections.abc import Iterable, Sequence
from typing import Any

TABLE = "fts_memories_cjk"
STATE_TABLE = "fts_memories_cjk_state"

_CJK = r"㐀-䶿一-鿿豈-﫿぀-ヿ가-힯"
_TOKEN_RE = re.compile(rf"[{_CJK}]+|[A-Za-z0-9_]+")
_CJK_RE = re.compile(rf"^[{_CJK}]+$")

# 查询只取 rowid 再回 memories 取正文，索引里不必再存一份切好的词：SQLite 3.43+ 用
# contentless_delete 表（仍支持按 rowid 删除），线上省掉约 140 MB 的 _content 副本。
_CONTENTLESS = sqlite3.sqlite_version_info >= (3, 43, 0)
_OPTIONS = "tokens, content='', contentless_delete=1, tokenize='unicode61'" if _CONTENTLESS else "tokens, tokenize='unicode61'"

_SCHEMA = f"""
CREATE VIRTUAL TABLE IF NOT EXISTS {TABLE} USING fts5({_OPTIONS});
CREATE TABLE IF NOT EXISTS {STATE_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL);
"""

# 与 fts5 通道的活跃过滤一致：隔离、删除、噪声不进索引。
_INDEXABLE = """
    content IS NOT NULL AND content != ''
    AND COALESCE(quarantine, 0) = 0
    AND COALESCE(memory_type, 'message') NOT IN ('archived', 'evicted', 'deleted', 'noise')
"""


def tokenize(text: str | None) -> list[str]:
    """正文 → 索引词。中日韩字串切相邻两字（单字串保留单字），其余按词小写。"""
    tokens: list[str] = []
    for run in _TOKEN_RE.findall(str(text or "")):
        if _CJK_RE.match(run):
            if len(run) == 1:
                tokens.append(run)
            else:
                tokens.extend(run[i:i + 2] for i in range(len(run) - 1))
        else:
            tokens.append(run.lower())
    return tokens


def token_text(text: str | None) -> str:
    return " ".join(tokenize(text))


def _phrase(word: str) -> str:
    tokens = tokenize(word)
    if not tokens:
        return ""
    # 单个中日韩字符的词不参与（单字噪声太大）；字母数字词长度至少 2
    if len(tokens) == 1 and (len(tokens[0]) < 2):
        return ""
    return '"' + " ".join(token.replace('"', "") for token in tokens) + '"'


def match_expr(words: Sequence[str]) -> str:
    """关键词 → FTS5 表达式：每个词是一个相邻短语，词之间 OR。"""
    phrases: list[str] = []
    for word in words or ():
        phrase = _phrase(str(word or "").strip())
        if phrase and phrase not in phrases:
            phrases.append(phrase)
    return " OR ".join(phrases)


def ensure_schema(connection: Any) -> None:
    for statement in _SCHEMA.split(";"):
        if statement.strip():
            connection.execute(statement)


def ensure_schema_committed(connection: Any) -> None:
    ensure_schema(connection)
    connection.commit()


def _state(connection: Any, key: str) -> str | None:
    try:
        row = connection.execute(f"SELECT value FROM {STATE_TABLE} WHERE key=?", (key,)).fetchone()
    except sqlite3.OperationalError:
        return None
    return str(row[0]) if row else None


def _set_state(connection: Any, key: str, value: Any) -> None:
    connection.execute(
        f"INSERT INTO {STATE_TABLE}(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, str(value)),
    )


def is_ready(connection: Any) -> bool:
    """回填完成后才让查询方切到新索引。"""
    return _state(connection, "ready") == "1"


def status(connection: Any) -> dict[str, Any]:
    try:
        rows = int(connection.execute(f"SELECT COUNT(*) FROM {TABLE}").fetchone()[0])
    except sqlite3.OperationalError:
        return {"exists": False, "ready": False, "rows": 0}
    return {
        "exists": True,
        "ready": is_ready(connection),
        "rows": rows,
        "backfill_cursor": int(_state(connection, "backfill_cursor") or 0),
        "completed_at": _state(connection, "completed_at"),
    }


def sync_memory(connection: Any, memory_id: int) -> str:
    """按记忆当前状态更新一行索引：可索引就写入，否则删除。返回 indexed / removed。"""
    row = connection.execute(
        f"SELECT content FROM memories WHERE id=? AND {_INDEXABLE}",
        (int(memory_id),),
    ).fetchone()
    connection.execute(f"DELETE FROM {TABLE} WHERE rowid=?", (int(memory_id),))
    if row is None:
        return "removed"
    tokens = token_text(row[0])
    if tokens:
        connection.execute(f"INSERT INTO {TABLE}(rowid, tokens) VALUES (?, ?)", (int(memory_id), tokens))
    return "indexed"


def sync_memory_committed(connection: Any, memory_id: int) -> str:
    """``sync_memory`` 并提交（派生索引自己的小事务，不走领域写入口）。"""
    try:
        outcome = sync_memory(connection, memory_id)
        connection.commit()
        return outcome
    except BaseException:
        connection.rollback()
        raise


def backfill_batch(connection: Any, *, after_id: int, limit: int = 2000) -> tuple[int, int]:
    """回填 id 大于 ``after_id`` 的一批记忆，返回 (本批最大 id, 写入条数)；没有更多时最大 id 为 after_id。"""
    rows = connection.execute(
        f"SELECT id, content FROM memories WHERE id > ? AND {_INDEXABLE} ORDER BY id LIMIT ?",
        (int(after_id), int(limit)),
    ).fetchall()
    if not rows:
        return int(after_id), 0
    ids = [int(r[0]) for r in rows]
    placeholders = ",".join("?" for _ in ids)
    connection.execute(f"DELETE FROM {TABLE} WHERE rowid IN ({placeholders})", ids)
    payload = [(int(r[0]), token_text(r[1])) for r in rows]
    connection.executemany(f"INSERT INTO {TABLE}(rowid, tokens) VALUES (?, ?)", [p for p in payload if p[1]])
    last = ids[-1]
    _set_state(connection, "backfill_cursor", last)
    return last, len(payload)


def mark_ready(connection: Any) -> None:
    _set_state(connection, "ready", "1")
    _set_state(connection, "completed_at", time.strftime("%Y-%m-%dT%H:%M:%S"))


def reset(connection: Any) -> None:
    connection.execute(f"DELETE FROM {TABLE}")
    _set_state(connection, "ready", "0")
    _set_state(connection, "backfill_cursor", 0)


def backfill_cursor(connection: Any) -> int:
    return int(_state(connection, "backfill_cursor") or 0)


def query_ids(connection: Any, words: Iterable[str], *, limit: int = 50) -> list[int]:
    """只按关键词取候选 id（按 bm25 排序），作用域过滤由调用方在 memories 上做。"""
    expr = match_expr(list(words))
    if not expr:
        return []
    return [
        int(row[0])
        for row in connection.execute(
            f"SELECT rowid FROM {TABLE} WHERE {TABLE} MATCH ? ORDER BY rank LIMIT ?",
            (expr, int(limit)),
        ).fetchall()
    ]


__all__ = [
    "STATE_TABLE",
    "TABLE",
    "backfill_batch",
    "backfill_cursor",
    "ensure_schema",
    "ensure_schema_committed",
    "is_ready",
    "mark_ready",
    "match_expr",
    "query_ids",
    "reset",
    "status",
    "sync_memory",
    "sync_memory_committed",
    "token_text",
    "tokenize",
]
