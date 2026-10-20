"""scoped fact 证据回挂 dry-run 脚本的行为测试。

覆盖：精确命中、改写后模糊命中、候选歧义、无匹配、跨 Scope 隔离、
时间窗外、缺 source_quote、缺 source_memory_id 列，以及 CSV/JSON 输出。
所有夹具都是临时 SQLite，脚本只读打开，测试同时校验数据库字节未变化。
"""

from __future__ import annotations

import csv
import hashlib
import importlib.util
import json
import sqlite3
from pathlib import Path

import pytest

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_fact_evidence.py"
BASE_TS = 1_700_000_000.0
DAY = 86400.0


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_fact_evidence", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def script():
    return _load_script()


def _create_db(path: Path, *, fact_source_column: bool = True) -> sqlite3.Connection:
    source_column = "source_memory_id INTEGER," if fact_source_column else ""
    conn = sqlite3.connect(path)
    conn.executescript(
        f"""
        CREATE TABLE scoped_facts (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT NOT NULL,
            session_id TEXT NOT NULL,
            visibility TEXT NOT NULL,
            subject TEXT NOT NULL DEFAULT '',
            predicate TEXT NOT NULL DEFAULT '',
            object TEXT NOT NULL DEFAULT '',
            confidence REAL NOT NULL DEFAULT 0.0,
            status TEXT NOT NULL DEFAULT 'pending',
            {source_column}
            provenance TEXT NOT NULL DEFAULT '{{}}',
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            revision INTEGER NOT NULL DEFAULT 1
        );

        CREATE TABLE memories (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            bot_id TEXT,
            session_id TEXT,
            visibility TEXT,
            content TEXT NOT NULL,
            timestamp REAL NOT NULL,
            sender_id TEXT,
            sender_name TEXT,
            resolution_state TEXT,
            quarantine INTEGER DEFAULT 0
        );
        """
    )
    return conn


def _add_fact(
    conn: sqlite3.Connection,
    *,
    quote: str = "",
    created_at: float = BASE_TS,
    bot_id: str = "yushu",
    session_id: str = "qq:group:g1",
    visibility: str = "group",
    source_memory_id=None,
    subject: str = "阿明",
    predicate: str = "正在",
    obj: str = "准备考研",
    provenance=None,
) -> int:
    if provenance is None:
        provenance = {"source_quote": quote}
    stored = provenance if isinstance(provenance, str) else json.dumps(provenance, ensure_ascii=False)
    cursor = conn.execute(
        """
        INSERT INTO scoped_facts(
            bot_id, session_id, visibility, subject, predicate, object,
            source_memory_id, provenance, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bot_id,
            session_id,
            visibility,
            subject,
            predicate,
            obj,
            source_memory_id,
            stored,
            created_at,
            created_at,
        ),
    )
    return int(cursor.lastrowid)


def _add_memory(
    conn: sqlite3.Connection,
    content: str,
    timestamp: float,
    *,
    bot_id: str = "yushu",
    session_id: str = "qq:group:g1",
    visibility: str = "group",
    resolution_state: str | None = "resolved",
    quarantine: int = 0,
    sender_id: str = "u1",
) -> int:
    cursor = conn.execute(
        """
        INSERT INTO memories(
            bot_id, session_id, visibility, content, timestamp,
            sender_id, sender_name, resolution_state, quarantine
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            bot_id,
            session_id,
            visibility,
            content,
            timestamp,
            sender_id,
            "阿明",
            resolution_state,
            quarantine,
        ),
    )
    return int(cursor.lastrowid)


def _digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_exact_match_after_nfkc_normalization_keeps_database_untouched(tmp_path, script):
    db = tmp_path / "wave memory.db"
    conn = _create_db(db)
    quote = "ＳＨＡＮＧＨＡＩ　出差"
    memory_id = _add_memory(conn, "那我下周要去shanghai 出差三天，顺便见个朋友", BASE_TS + 60)
    fact_id = _add_fact(conn, quote=quote)
    conn.commit()
    conn.close()

    before = _digest(db)
    report = script.run_backfill(db, days=7)
    after = _digest(db)

    assert report.ok, report.error
    assert before == after, "dry-run 不能修改数据库文件"
    assert report.summary["facts_seen"] == 1
    assert report.summary["facts_without_source_id"] == 1
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row["fact_id"] == fact_id
    assert row["source_quote"] == quote
    assert row["candidate_memory_id"] == memory_id
    assert row["match_method"] == "exact"
    assert row["score"] == 1.0
    assert row["ambiguous"] is False
    assert row["suggested"] is True
    assert row["status"] == "suggested"

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT source_memory_id FROM scoped_facts WHERE id=?", (fact_id,)
        ).fetchone()[0] is None, "脚本没有写库路径"
    finally:
        conn.close()


def test_paraphrased_quote_matches_with_sequence_matcher(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    quote = "我最近在准备考研，每天都去图书馆复习"
    memory_id = _add_memory(conn, "最近我在准备考研，每天都会去图书馆复习", BASE_TS + 120)
    fact_id = _add_fact(conn, quote=quote)
    conn.commit()
    conn.close()

    report = script.run_backfill(db)

    assert report.ok, report.error
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row["fact_id"] == fact_id
    assert row["candidate_memory_id"] == memory_id
    assert row["match_method"] == "fuzzy", "改写后的原话不应只靠精确子串"
    assert row["score"] >= script.DEFAULT_THRESHOLD
    assert row["ambiguous"] is False
    assert row["suggested"] is True


def test_multiple_close_candidates_are_ambiguous_without_suggestion(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    quote = "我觉得这家店的牛肉面特别好吃"
    first = _add_memory(conn, "我觉得这家店牛肉面特别好吃", BASE_TS + 30)
    second = _add_memory(conn, "我觉得这家店的牛肉面真的特别好吃", BASE_TS + 90)
    fact_id = _add_fact(conn, quote=quote)
    conn.commit()
    conn.close()

    report = script.run_backfill(db)

    assert report.ok, report.error
    assert report.summary["ambiguous"] == 1
    assert report.summary["suggested"] == 0
    assert len(report.rows) == 2
    assert {row["candidate_memory_id"] for row in report.rows} == {first, second}
    for row in report.rows:
        assert row["fact_id"] == fact_id
        assert row["ambiguous"] is True
        assert row["suggested"] is False, "歧义候选不能给出写入建议"
        assert row["status"] == "ambiguous"
        assert row["match_method"] == "fuzzy"
    scores = [row["score"] for row in report.rows]
    assert max(scores) - min(scores) < 0.05


def test_unrelated_memory_scores_below_threshold_as_no_match(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    memory_id = _add_memory(conn, "明天可能下雨记得带伞", BASE_TS + 10)
    fact_id = _add_fact(conn, quote="我今天买了两斤苹果")
    conn.commit()
    conn.close()

    report = script.run_backfill(db)

    assert report.ok, report.error
    assert report.summary["no_match"] == 1
    assert len(report.rows) == 1
    row = report.rows[0]
    assert row["fact_id"] == fact_id
    assert row["status"] == "no_match"
    assert row["suggested"] is False
    assert row["ambiguous"] is False
    assert row["score"] < script.DEFAULT_THRESHOLD
    assert row["candidate_memory_id"] == memory_id, "低于阈值的最佳候选仍可用于诊断"


def test_cross_scope_and_unhealthy_memories_are_ignored(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    quote = "我下周三要去杭州参加婚礼"
    _add_memory(conn, quote, BASE_TS, bot_id="baizz")
    _add_memory(conn, quote, BASE_TS, session_id="qq:group:g2")
    _add_memory(conn, quote, BASE_TS, visibility="private")
    _add_memory(conn, quote, BASE_TS, resolution_state="pending")
    _add_memory(conn, quote, BASE_TS, quarantine=1)
    fact_id = _add_fact(conn, quote=quote)
    conn.commit()
    conn.close()

    report = script.run_backfill(db)

    assert report.ok, report.error
    assert report.summary["no_match"] == 1
    assert report.rows[0]["fact_id"] == fact_id
    assert report.rows[0]["candidate_memory_id"] is None, "跨 Scope/未解析/隔离记忆不能成为候选"

    conn = sqlite3.connect(db)
    healthy_id = _add_memory(conn, quote, BASE_TS + 1)
    conn.commit()
    conn.close()

    report = script.run_backfill(db)
    assert report.ok, report.error
    assert report.summary["suggested"] == 1
    assert report.rows[0]["candidate_memory_id"] == healthy_id
    assert report.rows[0]["match_method"] == "exact"


def test_memories_outside_time_window_are_ignored(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    quote = "我上周把驾照考下来了"
    _add_memory(conn, quote, BASE_TS - 8 * DAY)
    _add_memory(conn, quote, BASE_TS + 8 * DAY)
    _add_fact(conn, quote=quote)
    conn.commit()
    conn.close()

    narrow = script.run_backfill(db, days=7)
    assert narrow.ok, narrow.error
    assert narrow.summary["no_match"] == 1
    assert narrow.rows[0]["candidate_memory_id"] is None

    wide = script.run_backfill(db, days=8)
    assert wide.ok, wide.error
    assert wide.summary["ambiguous"] == 1, "窗口边界包含两侧候选时应判歧义"
    assert len(wide.rows) == 2


def test_missing_source_quote_is_reported_without_matching(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    _add_memory(conn, "我最近在准备考研", BASE_TS)
    first = _add_fact(conn, quote="", provenance={})
    second = _add_fact(conn, quote="", provenance="not-json")
    conn.commit()
    conn.close()

    report = script.run_backfill(db)

    assert report.ok, report.error
    assert report.summary["missing_quote"] == 2
    assert report.summary["suggested"] == 0
    assert {row["fact_id"] for row in report.rows} == {first, second}
    for row in report.rows:
        assert row["status"] == "missing_quote"
        assert row["match_method"] == "none"
        assert row["candidate_memory_id"] is None
        assert row["suggested"] is False


def test_missing_source_memory_id_column_returns_clear_schema_error(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db, fact_source_column=False)
    conn.execute(
        """
        INSERT INTO scoped_facts(
            bot_id, session_id, visibility, subject, predicate, object,
            provenance, created_at, updated_at
        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        ("yushu", "qq:group:g1", "group", "阿明", "正在", "备考", "{}", BASE_TS, BASE_TS),
    )
    conn.commit()
    conn.close()

    report = script.run_backfill(db)

    assert report.ok is False
    assert "source_memory_id" in (report.error or "")
    assert report.rows == []
    assert script.main(["--db", str(db)]) == 2


def test_null_blank_zero_are_missing_but_existing_id_is_skipped(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    expected: dict[int, int] = {}
    for index, source_value in enumerate((None, "", 0, "   "), start=1):
        quote = f"这是第{index}条需要回挂的事实原话"
        expected[_add_fact(conn, quote=quote, source_memory_id=source_value)] = _add_memory(
            conn, quote, BASE_TS + index
        )
    already_linked_quote = "这条事实已经有来源记忆了"
    linked_fact = _add_fact(conn, quote=already_linked_quote, source_memory_id=42)
    _add_memory(conn, already_linked_quote, BASE_TS + 20)
    conn.commit()
    conn.close()

    report = script.run_backfill(db)

    assert report.ok, report.error
    assert report.summary["facts_seen"] == 5
    assert report.summary["facts_without_source_id"] == 4
    assert report.summary["suggested"] == 4
    assert len(report.rows) == 4
    assert linked_fact not in {row["fact_id"] for row in report.rows}
    for row in report.rows:
        assert row["candidate_memory_id"] == expected[row["fact_id"]]
        assert row["status"] == "suggested"


def test_apply_requires_yes_and_leaves_database_untouched(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    quote = "我下个月要搬去成都工作"
    _add_memory(conn, f"对了，{quote}，房子还没找好", BASE_TS + 45)
    fact_id = _add_fact(conn, quote=quote)
    conn.commit()
    conn.close()
    before = _digest(db)

    with pytest.raises(SystemExit) as exc:
        script.main(["--db", str(db), "--apply"])
    assert exc.value.code == 2
    assert _digest(db) == before

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT source_memory_id, revision FROM scoped_facts WHERE id=?", (fact_id,)
        ).fetchone() == (None, 1)
    finally:
        conn.close()


def test_apply_links_unique_evidence_and_writes_undo_log(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    quote = "我下个月要搬去成都工作"
    memory_id = _add_memory(conn, f"对了，{quote}，房子还没找好", BASE_TS + 45)
    fact_id = _add_fact(conn, quote=quote)
    conn.commit()
    conn.close()
    backup = tmp_path / "before.db"

    outcome = script.apply_backfill(db, backup_path=backup)

    assert outcome.ok, outcome.error
    assert outcome.planned == 1
    assert len(outcome.applied) == 1
    assert outcome.applied[0]["fact_id"] == fact_id
    assert outcome.applied[0]["memory_id"] == memory_id
    assert outcome.applied[0]["new_revision"] == 2
    assert Path(outcome.backup) == backup and backup.stat().st_size > 0
    assert outcome.undo_log and Path(outcome.undo_log).is_file()
    assert "UPDATE \"scoped_facts\"" in Path(outcome.undo_log).read_text(encoding="utf-8")

    conn = sqlite3.connect(db)
    try:
        source_id, revision, provenance, updated_at = conn.execute(
            "SELECT source_memory_id, revision, provenance, updated_at "
            "FROM scoped_facts WHERE id=?",
            (fact_id,),
        ).fetchone()
        assert source_id == memory_id
        assert revision == 2
        assert updated_at >= BASE_TS
        payload = json.loads(provenance)
        assert payload["source_quote"] == quote
        assert payload["evidence_backfill"]["memory_id"] == memory_id
        assert payload["evidence_backfill"]["match_method"] == "exact"
        assert payload["evidence_backfill"]["schema"] == "fact-evidence-backfill/v1"

        conn.executescript(Path(outcome.undo_log).read_text(encoding="utf-8"))
        restored = conn.execute(
            "SELECT source_memory_id, revision, provenance FROM scoped_facts WHERE id=?",
            (fact_id,),
        ).fetchone()
        assert restored[0] is None
        assert restored[1] == 1
        assert json.loads(restored[2]) == {"source_quote": quote}
    finally:
        conn.close()


def test_apply_skips_candidate_that_became_quarantined_after_planning(
    tmp_path, script, monkeypatch
):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    quote = "我下个月要搬去成都工作"
    memory_id = _add_memory(conn, f"对了，{quote}，房子还没找好", BASE_TS + 45)
    fact_id = _add_fact(conn, quote=quote)
    conn.commit()
    conn.close()

    original = script.run_backfill

    def plan_then_quarantine(*args, **kwargs):
        report = original(*args, **kwargs)
        changer = sqlite3.connect(db)
        try:
            changer.execute("UPDATE memories SET quarantine=1 WHERE id=?", (memory_id,))
            changer.commit()
        finally:
            changer.close()
        return report

    monkeypatch.setattr(script, "run_backfill", plan_then_quarantine)
    outcome = script.apply_backfill(db)

    assert outcome.ok, outcome.error
    assert outcome.planned == 1
    assert outcome.applied == []
    assert outcome.skipped == [{"fact_id": fact_id, "reason": "no_longer_no_match"}]

    conn = sqlite3.connect(db)
    try:
        assert conn.execute(
            "SELECT source_memory_id, revision FROM scoped_facts WHERE id=?", (fact_id,)
        ).fetchone() == (None, 1)
    finally:
        conn.close()


def test_apply_plans_nothing_for_ambiguous_evidence(tmp_path, script):
    db = tmp_path / "wave_memory.db"
    conn = _create_db(db)
    quote = "我觉得这家店的牛肉面特别好吃"
    _add_memory(conn, "我觉得这家店牛肉面特别好吃", BASE_TS + 30)
    _add_memory(conn, "我觉得这家店的牛肉面真的特别好吃", BASE_TS + 90)
    _add_fact(conn, quote=quote)
    conn.commit()
    conn.close()
    before = _digest(db)

    outcome = script.apply_backfill(db)

    assert outcome.ok, outcome.error
    assert outcome.planned == 0
    assert outcome.applied == []
    assert _digest(db) == before


def test_cli_writes_csv_and_json_reports(tmp_path, script):
    db = tmp_path / "wave memory.db"
    conn = _create_db(db)
    quote = "我下个月要搬去成都工作"
    memory_id = _add_memory(conn, f"对了，{quote}，房子还没找好", BASE_TS + 45)
    fact_id = _add_fact(conn, quote=quote)
    conn.commit()
    conn.close()
    before = _digest(db)

    csv_path = tmp_path / "candidates with space.csv"
    code = script.main(
        [
            "--db",
            str(db),
            "--bot-id",
            "yushu",
            "--session",
            "qq:group:g1",
            "--visibility",
            "group",
            "--days",
            "7",
            "--threshold",
            "0.72",
            "--out",
            str(csv_path),
        ]
    )
    assert code == 0
    with csv_path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        assert reader.fieldnames == list(script.CSV_FIELDS)
        csv_rows = list(reader)
    assert len(csv_rows) == 1
    assert csv_rows[0]["fact_id"] == str(fact_id)
    assert csv_rows[0]["candidate_memory_id"] == str(memory_id)
    assert csv_rows[0]["score"] == "1.000000"
    assert csv_rows[0]["match_method"] == "exact"
    assert csv_rows[0]["ambiguous"] == "false"
    assert csv_rows[0]["suggested"] == "true"

    json_path = tmp_path / "candidates.json"
    assert script.main(["--db", str(db), "--out", str(json_path)]) == 0
    payload = json.loads(json_path.read_text(encoding="utf-8"))
    assert payload["dry_run"] is True
    assert payload["read_only"] is True
    assert payload["summary"]["suggested"] == 1
    assert payload["rows"][0]["candidate_memory_id"] == memory_id
    assert payload["rows"][0]["source_quote"] == quote

    assert _digest(db) == before, "CSV/JSON 输出不应改动数据库"
