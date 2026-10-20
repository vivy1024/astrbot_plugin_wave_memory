"""Tests for the read-only expression corpus audit script."""

from __future__ import annotations

import csv
import json
import sqlite3
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = REPO_ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

import expr_corpus_audit as audit

BOT = "bot-db-1"
SESSION = "aiocqhttp:group:10001"


class DBFixture:
    """Small temp SQLite database with the columns the audit actually reads."""

    def __init__(self, path: Path, *, with_counts: bool = True) -> None:
        self.path = path
        self.with_counts = with_counts
        self.conn = sqlite3.connect(path)
        candidate_columns = [
            "id INTEGER PRIMARY KEY AUTOINCREMENT",
            "bot_id TEXT NOT NULL",
            "session_id TEXT NOT NULL",
            "visibility TEXT NOT NULL DEFAULT 'group'",
            "content TEXT NOT NULL",
            "status TEXT NOT NULL DEFAULT 'pending'",
        ]
        if with_counts:
            candidate_columns.extend(
                [
                    "occurrence_count INTEGER NOT NULL DEFAULT 1",
                    "speaker_count INTEGER NOT NULL DEFAULT 1",
                ]
            )
        self.conn.execute(
            f"CREATE TABLE reply_expression_candidates ({', '.join(candidate_columns)})"
        )
        self.conn.execute(
            """
            CREATE TABLE memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                group_id TEXT,
                bot_id TEXT,
                session_id TEXT,
                visibility TEXT,
                sender_id TEXT,
                content TEXT,
                timestamp REAL,
                memory_type TEXT DEFAULT 'message',
                quarantine INTEGER DEFAULT 0
            )
            """
        )
        self.conn.commit()

    def add_candidate(
        self,
        content: str,
        *,
        bot_id: str = BOT,
        session_id: str = SESSION,
        visibility: str = "group",
        occurrence_count: int = 1,
        speaker_count: int = 1,
    ) -> int:
        if self.with_counts:
            cursor = self.conn.execute(
                """INSERT INTO reply_expression_candidates
                   (bot_id, session_id, visibility, content, occurrence_count, speaker_count)
                   VALUES (?, ?, ?, ?, ?, ?)""",
                (bot_id, session_id, visibility, content, occurrence_count, speaker_count),
            )
        else:
            cursor = self.conn.execute(
                """INSERT INTO reply_expression_candidates
                   (bot_id, session_id, visibility, content)
                   VALUES (?, ?, ?, ?)""",
                (bot_id, session_id, visibility, content),
            )
        self.conn.commit()
        return int(cursor.lastrowid)

    def add_memory(
        self,
        content: str,
        *,
        sender_id: str = "user-1",
        timestamp: float = 1000.0,
        bot_id: str = BOT,
        session_id: str = SESSION,
        visibility: str = "group",
        memory_type: str | None = "message",
        quarantine: int = 0,
    ) -> int:
        cursor = self.conn.execute(
            """INSERT INTO memories
               (group_id, bot_id, session_id, visibility, sender_id, content,
                timestamp, memory_type, quarantine)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                "10001",
                bot_id,
                session_id,
                visibility,
                sender_id,
                content,
                timestamp,
                memory_type,
                quarantine,
            ),
        )
        self.conn.commit()
        return int(cursor.lastrowid)

    def close(self) -> None:
        self.conn.close()


@pytest.fixture()
def make_db(tmp_path):
    created: list[DBFixture] = []

    def factory(*, with_counts: bool = True) -> DBFixture:
        fixture = DBFixture(tmp_path / f"audit-{len(created)}.db", with_counts=with_counts)
        created.append(fixture)
        return fixture

    yield factory
    for fixture in created:
        fixture.close()


def audit_scope(db: DBFixture, *, strict: bool = False) -> dict:
    return audit.audit_corpus(
        db.path,
        bot_id=BOT,
        session_id=SESSION,
        visibility="group",
        strict=strict,
    )


def only_candidate(report: dict) -> dict:
    assert len(report["candidates"]) == 1
    return report["candidates"][0]


# ─── 归一化与去重 ──────────────────────────────────────────────────────────

def test_fullwidth_punctuation_is_normalized_on_both_sides(make_db):
    db = make_db()
    db.add_candidate("真的吗？")
    db.add_memory("真的吗?", sender_id="user-1", timestamp=1.0)
    db.add_memory("真的吗？", sender_id="user-2", timestamp=2.0)

    row = only_candidate(audit_scope(db))

    # 输出保留候选表原文；匹配用的是两侧 NFKC 归一化后的文本。
    assert row["content"] == "真的吗？"
    assert row["real_occurrence_count"] == 2
    assert row["real_speaker_count"] == 2
    assert row["real_occurrence_count_strict"] == 2
    assert row["real_speaker_count_strict"] == 2


def test_duplicate_rows_are_deduped_by_sender_content_and_rounded_timestamp(make_db):
    db = make_db()
    db.add_candidate("哈哈")
    db.add_memory("哈哈", sender_id="user-1", timestamp=100.01)
    db.add_memory("哈哈", sender_id="user-1", timestamp=100.04)  # 同一 0.1 秒窗口
    db.add_memory("哈哈", sender_id="user-1", timestamp=100.24)  # 不同窗口
    db.add_memory("哈哈", sender_id="user-2", timestamp=100.01)  # 不同说话人

    report = audit_scope(db)
    row = only_candidate(report)

    assert row["real_occurrence_count"] == 3
    assert row["real_speaker_count"] == 2
    assert report["corpus"]["rows_fetched"] == 4
    assert report["corpus"]["rows_skipped_duplicate"] == 1
    assert report["corpus"]["rows_used"] == 3


# ─── strict / loose 子串语义 ───────────────────────────────────────────────

def test_strict_boundaries_reject_embedded_chinese_substring(make_db):
    db = make_db()
    db.add_candidate("会的")
    db.add_memory("如果会的话", sender_id="user-1", timestamp=1.0)  # 子串误报
    db.add_memory("会的。", sender_id="user-2", timestamp=2.0)
    db.add_memory("我觉得，会的！", sender_id="user-3", timestamp=3.0)
    db.add_memory("不会的", sender_id="user-4", timestamp=4.0)  # 左侧不是边界

    report = audit_scope(db)
    row = only_candidate(report)

    assert row["real_occurrence_count"] == 4
    assert row["real_speaker_count"] == 4
    assert row["real_occurrence_count_strict"] == 2
    assert row["real_speaker_count_strict"] == 2

    assert row["occurrence_ge_3_loose"] is True
    assert row["speaker_ge_2_loose"] is True
    assert row["both_ge_loose"] is True
    assert row["qualified_loose"] is True

    assert row["occurrence_ge_3_strict"] is False
    assert row["speaker_ge_2_strict"] is True
    assert row["both_ge_strict"] is False
    # 生产门槛是 occurrence>=3 OR speaker>=2，严格口径下仍达标。
    assert row["qualified_strict"] is True

    assert report["summary"]["loose"]["occurrence_ge_3"] == 1
    assert report["summary"]["strict"]["occurrence_ge_3"] == 0
    assert report["summary"]["strict"]["speaker_ge_2"] == 1


def test_a_message_counts_once_per_candidate_even_if_substring_repeats(make_db):
    db = make_db()
    db.add_candidate("哈哈")
    db.add_memory("哈哈哈哈", sender_id="user-1", timestamp=1.0)

    row = only_candidate(audit_scope(db))

    assert row["real_occurrence_count"] == 1
    assert row["real_speaker_count"] == 1


def test_strict_flag_switches_unsuffixed_fields_to_strict_mode(make_db):
    db = make_db()
    db.add_candidate("会的")
    db.add_memory("如果会的话", sender_id="user-1", timestamp=1.0)
    db.add_memory("会的", sender_id="user-2", timestamp=2.0)

    loose_report = audit_scope(db)
    strict_report = audit_scope(db, strict=True)

    loose_row = only_candidate(loose_report)
    strict_row = only_candidate(strict_report)

    assert loose_row["real_occurrence_count"] == 2
    assert loose_row["real_occurrence_count_loose"] == 2
    assert loose_row["real_occurrence_count_strict"] == 1

    assert strict_row["real_occurrence_count"] == 1  # 主口径
    assert strict_row["real_occurrence_count_loose"] == 2  # 另一种口径仍保留
    assert strict_row["real_occurrence_count_strict"] == 1
    assert strict_row["occurrence_ge_3"] is False
    assert strict_row["speaker_ge_2"] is False
    assert strict_report["semantics"]["primary_mode"] == "strict"
    assert strict_report["summary"]["primary"] == strict_report["summary"]["strict"]
    assert loose_report["summary"]["primary"] == loose_report["summary"]["loose"]


# ─── 语料过滤 ──────────────────────────────────────────────────────────────

def test_bot_noise_quarantine_and_empty_content_are_excluded(make_db):
    db = make_db()
    db.add_candidate("哈哈")
    db.add_memory("哈哈", sender_id="user-1")  # 唯一有效行
    db.add_memory("哈哈", sender_id="bot")
    db.add_memory("哈哈", sender_id="user-2", memory_type="noise")
    db.add_memory("哈哈", sender_id="user-3", memory_type="archived")
    db.add_memory("哈哈", sender_id="user-4", memory_type="evicted")
    db.add_memory("哈哈", sender_id="user-5", memory_type="deleted")
    db.add_memory("哈哈", sender_id="user-6", quarantine=1)
    db.add_memory("", sender_id="user-7")
    db.add_memory("\u3000", sender_id="user-8")  # NFKC 后为空格，Python 侧跳过
    db.add_memory("哈哈", sender_id="user-9", memory_type=None)  # 旧行按 message 处理

    report = audit_scope(db)
    row = only_candidate(report)

    assert row["real_occurrence_count"] == 2
    assert row["real_speaker_count"] == 2
    assert report["corpus"]["rows_fetched"] == 3
    assert report["corpus"]["rows_skipped_empty"] == 1
    assert report["corpus"]["rows_used"] == 2


def test_scope_isolation_between_sessions_and_bots(make_db):
    db = make_db()
    session_b = "aiocqhttp:group:10002"
    bot_b = "bot-db-2"
    candidate_a = db.add_candidate("收到", bot_id=BOT, session_id=SESSION)
    candidate_b = db.add_candidate("收到", bot_id=BOT, session_id=session_b)
    candidate_c = db.add_candidate("收到", bot_id=bot_b, session_id=SESSION)
    db.add_memory("收到", sender_id="user-a", bot_id=BOT, session_id=SESSION)
    db.add_memory("收到", sender_id="user-b", bot_id=BOT, session_id=session_b)
    db.add_memory("收到", sender_id="user-c", bot_id=bot_b, session_id=SESSION)
    db.add_memory(
        "收到",
        sender_id="user-private",
        bot_id=BOT,
        session_id=SESSION,
        visibility="private",
    )

    report_a = audit_scope(db)
    report_b = audit.audit_corpus(
        db.path, bot_id=BOT, session_id=session_b, visibility="group"
    )

    assert [row["id"] for row in report_a["candidates"]] == [candidate_a]
    assert [row["id"] for row in report_b["candidates"]] == [candidate_b]
    assert candidate_c not in [row["id"] for row in report_a["candidates"]]

    row_a = only_candidate(report_a)
    assert row_a["real_occurrence_count"] == 1
    assert row_a["real_speaker_count"] == 1
    assert report_b["candidates"][0]["real_occurrence_count"] == 1


# ─── 缺列与空语料 ──────────────────────────────────────────────────────────

def test_missing_count_columns_report_null(make_db, tmp_path):
    db = make_db(with_counts=False)
    db.add_candidate("收到")
    db.add_memory("收到")

    report = audit_scope(db)
    row = only_candidate(report)

    assert row["id"] == 1
    assert row["candidate_occurrence_count"] is None
    assert row["candidate_speaker_count"] is None
    assert report["summary"]["candidates_with_candidate_counts"] == 0

    out = tmp_path / "report.json"
    audit.write_report(report, out)
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["candidates"][0]["candidate_occurrence_count"] is None


def test_empty_corpus_and_empty_candidate_table(make_db):
    db = make_db()
    db.add_candidate("收到")
    db.add_candidate("好的")

    report = audit_scope(db)
    assert report["corpus"]["rows_fetched"] == 0
    assert report["corpus"]["rows_used"] == 0
    assert report["summary"]["candidate_total"] == 2
    assert report["summary"]["loose"]["with_any_match"] == 0
    for row in report["candidates"]:
        assert row["real_occurrence_count"] == 0
        assert row["real_speaker_count"] == 0
        assert row["occurrence_ge_3_loose"] is False
        assert row["speaker_ge_2_loose"] is False
        assert row["both_ge_loose"] is False

    db2 = make_db()
    db2.add_memory("没有候选")
    report2 = audit_scope(db2)
    assert report2["candidates"] == []
    assert report2["summary"]["candidate_total"] == 0


# ─── 输出与 CLI ────────────────────────────────────────────────────────────

def test_csv_and_json_reports_are_written(make_db, tmp_path):
    db = make_db()
    db.add_candidate("收到", occurrence_count=1, speaker_count=1)
    db.add_memory("收到", sender_id="user-1", timestamp=1.0)

    report = audit_scope(db)
    json_path = tmp_path / "nested" / "audit.json"
    csv_path = tmp_path / "nested" / "audit.csv"
    audit.write_report(report, json_path)
    audit.write_report(report, csv_path)

    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["summary"]["candidate_total"] == 1
    assert payload["candidates"][0]["real_occurrence_count"] == 1

    with csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    assert len(rows) == 1
    assert rows[0]["content"] == "收到"
    assert rows[0]["real_occurrence_count"] == "1"
    assert rows[0]["occurrence_ge_3_loose"] == "false"


def test_cli_is_read_only_and_prints_summary(make_db, tmp_path, capsys):
    db = make_db()
    db.add_candidate("收到")
    db.add_memory("收到", sender_id="user-1", timestamp=1.0)
    db.close()
    before = db.path.read_bytes()

    out = tmp_path / "cli.json"
    exit_code = audit.main(
        [
            "--db",
            str(db.path),
            "--bot-id",
            BOT,
            "--session",
            SESSION,
            "--out",
            str(out),
        ]
    )

    assert exit_code == 0
    assert db.path.read_bytes() == before
    assert out.exists()
    printed = capsys.readouterr().out
    assert "candidates: total=1" in printed
    assert "primary (loose):" in printed
    payload = json.loads(out.read_text(encoding="utf-8"))
    assert payload["scope"]["bot_id"] == BOT
    assert payload["candidates"][0]["real_occurrence_count"] == 1
