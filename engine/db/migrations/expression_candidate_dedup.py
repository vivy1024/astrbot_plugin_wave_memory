"""表达候选：内容级去重所需的新列与来源表。

背景：`reply_expression_candidates` 的唯一键是 `(bot_id, session_id, visibility,
source_memory_id)` —— 按**来源消息**去重，不是按内容。于是同一句「难绷」在群里被说
三次就产生三行待审候选；线上 1190 条 pending 里只有 21 条内容出现过 ≥2 次，其余
都是一次性口头禅。人工逐条审批显然走不通（reviews 表至今 0 行），表达学习事实上
停在采集阶段。

这里只做**加列 + 加表 + 回填 + 触发器**，不改动既有行的语义、不重建表、不删除数据：
- `reply_expression_candidate_sources` 记录每个候选的**全部**来源消息（证据可追溯）；
- `occurrence_count` / `speaker_count` 是与 sources 表同步的读优化列；
- 计数由触发器维护，避免多进程并发下的竞态；回填保证老库计数与来源行一致。

去重粒度是**群内按内容**（`bot_id + session_id + visibility + content`），不跨群：
把 `yushu@群A` 和 `baizz@群B` 说过的同一句话合并成一条，会出现「审批一次但在两个群
都能召回，而其中一个群的证据来自另一个群」的失配。群内归并既能把待审量压到人工
可承受的范围，又保证每个群的召回证据都来自本群。
"""

from __future__ import annotations

import time

from .scoped_learning_projections import _transaction


# 内容级去重的读路径索引：按 scope + 内容精确定位已有候选。
_INDEXES = (
    "CREATE INDEX IF NOT EXISTS idx_reply_expression_content ON "
    "reply_expression_candidates(bot_id, session_id, visibility, content, id DESC)",
    "CREATE INDEX IF NOT EXISTS idx_reply_expression_source ON "
    "reply_expression_candidate_sources(example_id, source_memory_id)",
)

_SOURCES_TABLE = """CREATE TABLE IF NOT EXISTS reply_expression_candidate_sources (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    example_id INTEGER NOT NULL,
    bot_id TEXT NOT NULL,
    session_id TEXT NOT NULL,
    visibility TEXT NOT NULL CHECK(visibility='group'),
    source_memory_id INTEGER NOT NULL,
    speaker_id TEXT NOT NULL,
    situation TEXT NOT NULL,
    artifact_json TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(example_id, source_memory_id),
    FOREIGN KEY(example_id) REFERENCES reply_expression_candidates(id)
)"""

# 触发器维护计数列：`occurrence_count` 数来源条数，`speaker_count` 数不同说话人。
# 用触发器而不是应用层 UPDATE，是因为来源行可能由不同事务/进程写入，应用层读-改-写
# 会丢更新；SQLite 的触发器在同一写事务内原子执行。
_TRIGGERS = (
    """CREATE TRIGGER IF NOT EXISTS trg_reply_expression_source_insert
       AFTER INSERT ON reply_expression_candidate_sources
       BEGIN
         UPDATE reply_expression_candidates
            SET occurrence_count = (SELECT COUNT(*) FROM reply_expression_candidate_sources WHERE example_id=NEW.example_id),
                speaker_count = (SELECT COUNT(DISTINCT speaker_id) FROM reply_expression_candidate_sources WHERE example_id=NEW.example_id)
          WHERE id = NEW.example_id;
       END""",
    """CREATE TRIGGER IF NOT EXISTS trg_reply_expression_source_delete
       AFTER DELETE ON reply_expression_candidate_sources
       BEGIN
         UPDATE reply_expression_candidates
            SET occurrence_count = (SELECT COUNT(*) FROM reply_expression_candidate_sources WHERE example_id=OLD.example_id),
                speaker_count = (SELECT COUNT(DISTINCT speaker_id) FROM reply_expression_candidate_sources WHERE example_id=OLD.example_id)
          WHERE id = OLD.example_id;
       END""",
    # 合并重复候选时来源行会被改归属，两边的计数都要重算。
    """CREATE TRIGGER IF NOT EXISTS trg_reply_expression_source_move
       AFTER UPDATE OF example_id ON reply_expression_candidate_sources
       BEGIN
         UPDATE reply_expression_candidates
            SET occurrence_count = (SELECT COUNT(*) FROM reply_expression_candidate_sources WHERE example_id=OLD.example_id),
                speaker_count = (SELECT COUNT(DISTINCT speaker_id) FROM reply_expression_candidate_sources WHERE example_id=OLD.example_id)
          WHERE id = OLD.example_id;
         UPDATE reply_expression_candidates
            SET occurrence_count = (SELECT COUNT(*) FROM reply_expression_candidate_sources WHERE example_id=NEW.example_id),
                speaker_count = (SELECT COUNT(DISTINCT speaker_id) FROM reply_expression_candidate_sources WHERE example_id=NEW.example_id)
          WHERE id = NEW.example_id;
       END""",
)

# 合并重复候选时写进 review_note 的前缀：说明这行不是被人否决的，而是被同内容的新行吸收。
MERGED_NOTE_PREFIX = "content_duplicate_merged:superseded_by="

# 旧库缺列时补上（ALTER TABLE ... ADD COLUMN）；不改已有数据。
_COLUMNS: dict[str, dict[str, str]] = {
    "reply_expression_candidates": {
        "occurrence_count": "INTEGER NOT NULL DEFAULT 1",
        "speaker_count": "INTEGER NOT NULL DEFAULT 1",
    },
}


def _column_names(tx, table: str) -> set[str]:
    return {str(row[1]) for row in tx.execute(f"PRAGMA table_info({table})").fetchall()}


def _backfill_sources(tx) -> int:
    """把还没有来源行的候选补一条：来源就是它自己的 source_memory_id。

    这是「来源表」与「候选表」的第一次对齐；已存在来源行的候选不动，因此重复执行
    不会重复插入（`UNIQUE(example_id, source_memory_id)` + NOT EXISTS）。

    被归并吸收的旧行**排除在外**：它们的来源已经移交给了保留行，重新补一条会让
    这些已作废的行看起来又有了一次出现，也会让重复执行不再幂等。
    """
    cursor = tx.execute(
        """INSERT INTO reply_expression_candidate_sources
           (example_id, bot_id, session_id, visibility, source_memory_id, speaker_id,
            situation, artifact_json, created_at)
           SELECT c.id, c.bot_id, c.session_id, c.visibility, c.source_memory_id, c.speaker_id,
                  c.situation, c.artifact_json, c.created_at
             FROM reply_expression_candidates AS c
            WHERE c.review_note NOT LIKE ?
              AND NOT EXISTS (
                    SELECT 1 FROM reply_expression_candidate_sources AS s
                     WHERE s.example_id = c.id
                  )""",
        (f"{MERGED_NOTE_PREFIX}%",),
    )
    return max(0, int(cursor.rowcount or 0))


def _resync_counts(tx) -> int:
    """按来源表重算计数列；只更新不一致的行，重复执行无副作用。"""
    cursor = tx.execute(
        """UPDATE reply_expression_candidates
              SET occurrence_count = COALESCE((SELECT COUNT(*) FROM reply_expression_candidate_sources s WHERE s.example_id = reply_expression_candidates.id), 1),
                  speaker_count = COALESCE((SELECT COUNT(DISTINCT s.speaker_id) FROM reply_expression_candidate_sources s WHERE s.example_id = reply_expression_candidates.id), 1)
            WHERE occurrence_count != COALESCE((SELECT COUNT(*) FROM reply_expression_candidate_sources s WHERE s.example_id = reply_expression_candidates.id), 1)
               OR speaker_count != COALESCE((SELECT COUNT(DISTINCT s.speaker_id) FROM reply_expression_candidate_sources s WHERE s.example_id = reply_expression_candidates.id), 1)"""
    )
    return max(0, int(cursor.rowcount or 0))


def merge_duplicate_candidates(tx) -> dict[str, int]:
    """同 scope 同内容的重复待审候选：来源归并到保留行，旧行标为已被吸收。

    保留 **id 最大**（最新）的那一行：WebUI 按 id 倒序展示，reviewer 刚看到的行不会
    在点批准前被换成另一行；旧行 `revision+1` 并置为 `rejected`，因此拿着旧 revision
    提交的审批会得到 `stale_revision`，而不是静默批准一条已被取代的候选。

    被吸收的行**不写** `reply_expression_reviews`：那张表留给人工审核决策，
    去重不是审核，不该在审计记录里冒充一次否决。
    """
    groups = tx.execute(
        """SELECT bot_id, session_id, visibility, content, MAX(id) AS keeper
             FROM reply_expression_candidates
            WHERE status='pending'
            GROUP BY bot_id, session_id, visibility, content
           HAVING COUNT(*) > 1"""
    ).fetchall()
    moved = absorbed = 0
    for bot, session, visibility, content, keeper in groups:
        stale = [row[0] for row in tx.execute(
            """SELECT id FROM reply_expression_candidates
                WHERE status='pending' AND bot_id=? AND session_id=? AND visibility=?
                  AND content=? AND id<>?""",
            (bot, session, visibility, content, keeper),
        ).fetchall()]
        for old_id in stale:
            cursor = tx.execute(
                """INSERT OR IGNORE INTO reply_expression_candidate_sources
                   (example_id,bot_id,session_id,visibility,source_memory_id,speaker_id,
                    situation,artifact_json,created_at)
                   SELECT ?,bot_id,session_id,visibility,source_memory_id,speaker_id,
                          situation,artifact_json,created_at
                     FROM reply_expression_candidate_sources WHERE example_id=?""",
                (keeper, old_id),
            )
            moved += max(0, int(cursor.rowcount or 0))
            # 剩下的来源行都是保留行里已有的同一条消息，删掉避免重复计数。
            tx.execute("DELETE FROM reply_expression_candidate_sources WHERE example_id=?", (old_id,))
            cursor = tx.execute(
                """UPDATE reply_expression_candidates
                      SET status='rejected', revision=revision+1, reviewed_at=?, review_note=?
                    WHERE id=?""",
                (time.time(), f"{MERGED_NOTE_PREFIX}{keeper}", old_id),
            )
            absorbed += max(0, int(cursor.rowcount or 0))
    return {"moved_sources": moved, "absorbed": absorbed}


def ensure_expression_candidate_dedup_schema(connection) -> dict[str, int]:
    """幂等；若调用方已有写事务则在其中执行，否则自开一个。

    返回本次实际改动的行数（仅供启动日志观察，不是契约）。
    """
    result = {"sources_backfilled": 0, "moved_sources": 0, "absorbed": 0, "counts_resynced": 0}

    def apply(tx) -> None:
        for table, columns in _COLUMNS.items():
            existing = _column_names(tx, table)
            for name, ddl in columns.items():
                if name not in existing:
                    tx.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")
        tx.execute(_SOURCES_TABLE)
        for statement in _INDEXES:
            tx.execute(statement)
        # 触发器必须在回填之前建：这样回填写入来源行时计数列会跟着走，
        # 不会出现「来源行已写、计数还是默认 1」的中间态。
        for statement in _TRIGGERS:
            tx.execute(statement)
        result["sources_backfilled"] = _backfill_sources(tx)
        result.update(merge_duplicate_candidates(tx))
        result["counts_resynced"] = _resync_counts(tx)

    if bool(getattr(connection, "in_transaction", False)):
        apply(connection)
    else:
        with _transaction(connection) as tx:
            apply(tx)
    return result


def merge_source(connection, *, example_id: int, artifact_json: str, **fields) -> bool:
    """把一条来源并入已有候选；返回是否新增（False 表示之前已记过）。"""
    cursor = connection.execute(
        """INSERT INTO reply_expression_candidate_sources
           (example_id, bot_id, session_id, visibility, source_memory_id, speaker_id,
            situation, artifact_json, created_at)
           VALUES (:example_id, :bot_id, :session_id, :visibility, :source_memory_id,
                   :speaker_id, :situation, :artifact_json, :created_at)
           ON CONFLICT(example_id, source_memory_id) DO NOTHING""",
        {"example_id": int(example_id), "artifact_json": artifact_json, **fields},
    )
    return max(0, int(cursor.rowcount or 0)) > 0


def source_rows(connection, example_id: int) -> list[dict]:
    """读取候选的全部来源，按出现顺序返回（供审计与召回证据选择）。"""
    cursor = connection.execute(
        "SELECT * FROM reply_expression_candidate_sources WHERE example_id=? ORDER BY id", (int(example_id),),
    )
    names = [column[0] for column in cursor.description]
    return [dict(zip(names, row)) for row in cursor.fetchall()]


def primary_artifact(connection, example_id: int) -> str | None:
    """代表来源的 artifact_json，按来源顺序取第一条。

    召回时必须用**本群**来源做证据，所以正常路径由调用方按 scope 自己挑；
    这里只是给没有 scope 的审计读路径一个兜底。
    """
    row = connection.execute(
        "SELECT artifact_json FROM reply_expression_candidate_sources WHERE example_id=? ORDER BY id LIMIT 1",
        (int(example_id),),
    ).fetchone()
    return row[0] if row else None


__all__ = [
    "MERGED_NOTE_PREFIX",
    "ensure_expression_candidate_dedup_schema",
    "merge_duplicate_candidates",
    "merge_source",
    "primary_artifact",
    "source_rows",
]
