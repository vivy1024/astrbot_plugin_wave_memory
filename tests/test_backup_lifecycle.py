"""自动备份：包含 WAL 里的写入、不被手工备份干扰、轮换只动自动备份。"""

from __future__ import annotations

import os
import sqlite3
import time

from services.backup_lifecycle import DatabaseBackupManager


def _live_db(data_dir):
    db = data_dir / "wave_memory.db"
    conn = sqlite3.connect(db)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA wal_autocheckpoint=0")
    conn.execute("CREATE TABLE memories (id INTEGER PRIMARY KEY, content TEXT)")
    conn.executemany("INSERT INTO memories(content) VALUES (?)", [(f"m{i}",) for i in range(50)])
    conn.commit()
    return conn


def test_backup_includes_uncheckpointed_wal_rows(tmp_path):
    conn = _live_db(tmp_path)  # 连接保持打开：数据还在 WAL 里
    assert (tmp_path / "wave_memory.db-wal").stat().st_size > 0
    manager = DatabaseBackupManager(str(tmp_path))
    assert manager.run_backup_safe(background=False) is True
    backup = manager.auto_backups()[-1]
    copy = sqlite3.connect(backup)
    assert copy.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 50
    copy.close()
    conn.close()


def test_manual_backups_do_not_affect_interval_or_rotation(tmp_path):
    conn = _live_db(tmp_path)
    backups = tmp_path / "backups"
    backups.mkdir()
    manual = backups / "wave_memory_before_cleanup_20260906_135411.db"
    manual.write_bytes(b"manual")
    old = time.time() - 10 * 86400
    os.utime(manual, (old, old))
    manager = DatabaseBackupManager(str(tmp_path), config={"backup_max_count": 1})
    assert manager.run_backup_safe(background=False) is True
    # 刚备份过：间隔内不再备份（v5 会按文件名挑到手工备份，每次启动都复制一遍）
    assert manager.run_backup_safe(background=False) is False
    first = manager.auto_backups()[-1]
    os.utime(first, (old, old))
    time.sleep(1.1)  # 时间戳精度到秒
    assert manager.run_backup_safe(background=False) is True
    autos = manager.auto_backups()
    assert len(autos) == 1 and autos[0].name != first.name
    assert manual.exists()
    conn.close()


def test_background_backup_does_not_block(tmp_path):
    conn = _live_db(tmp_path)
    manager = DatabaseBackupManager(str(tmp_path))
    assert manager.run_backup_safe() is True
    manager._thread.join(timeout=10)
    assert manager.last_result["ok"] is True
    conn.close()
