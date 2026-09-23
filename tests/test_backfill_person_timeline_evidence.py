"""原话证据回填脚本：同 Bot 同群反查、推测显式标记、先备份再单事务写入。"""

from __future__ import annotations

import importlib.util
import json
import sqlite3
from pathlib import Path

from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB

_SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "backfill_person_timeline_evidence.py"


def _load_script():
    spec = importlib.util.spec_from_file_location("backfill_person_timeline_evidence", _SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _group(group: str, bot: str = "yushu") -> RuntimeScope:
    return RuntimeScope(bot, "group", SessionRef(f"qq:group:{group}", "qq", "group", group))


def _provenance(conn: sqlite3.Connection, event_id: int) -> dict:
    return json.loads(conn.execute("SELECT provenance FROM person_timeline_events WHERE id=?", (event_id,)).fetchone()[0])


def test_backfill_marks_window_quotes_as_inferred_and_backs_up_first(tmp_path):
    path = tmp_path / "wave_memory.db"
    db = WaveMemoryDB(str(path), dimension=3)
    try:
        db.add_memory(group_id="g1", content="另一个 Bot 看到的更近发言", sender_id="u1", timestamp=99.0,
                      scope=_group("g1", bot="baizz"), provenance={}, origin_metadata={})
        own_mid = db.add_memory(group_id="g1", content="我今天终于把论文交了", sender_id="u1", timestamp=90.0,
                                scope=_group("g1"), provenance={}, origin_metadata={})
        timeline = db.person_timeline
        inferred_id = timeline.add_event(bot_id="yushu", user_id="u1", group_id="g1", kind="impression",
                                         summary="很拼", occurred_at=100.0)
        confirmed_id = timeline.add_event(bot_id="yushu", user_id="u1", group_id="g1", kind="impression",
                                          summary="靠谱", occurred_at=100.0, provenance={"source_memory_id": own_mid})
    finally:
        db.close()

    script = _load_script()
    assert script.backfill_evidence(path, dry_run=True) == 2
    conn = sqlite3.connect(path)
    try:
        assert "source_quote" not in _provenance(conn, inferred_id), "dry-run 不写库"
    finally:
        conn.close()
    assert not list(tmp_path.glob("*.bak-before-evidence-*")), "dry-run 不备份"

    assert script.backfill_evidence(path) == 2
    backups = list(tmp_path.glob("wave_memory.db.bak-before-evidence-*"))
    assert len(backups) == 1
    conn = sqlite3.connect(path)
    try:
        inferred = _provenance(conn, inferred_id)
        assert inferred["source_quote"] == "我今天终于把论文交了", "只取当前 Bot 在同群的发言"
        assert inferred["source_memory_id"] == own_mid
        assert inferred["source_quote_inferred"] is True
        detail = conn.execute("SELECT detail FROM person_timeline_events WHERE id=?", (inferred_id,)).fetchone()[0]
        assert "原话证据" not in detail, "推测原话不写进 detail 冒充确证"

        confirmed = _provenance(conn, confirmed_id)
        assert confirmed["source_quote"] == "我今天终于把论文交了"
        assert "source_quote_inferred" not in confirmed
        detail = conn.execute("SELECT detail FROM person_timeline_events WHERE id=?", (confirmed_id,)).fetchone()[0]
        assert "原话证据" in detail
    finally:
        conn.close()
    backup = sqlite3.connect(backups[0])
    try:
        assert "source_quote" not in _provenance(backup, inferred_id), "备份保存的是回填前的状态"
    finally:
        backup.close()
