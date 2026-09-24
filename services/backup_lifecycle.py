"""Database backup lifecycle manager running safely and asynchronously."""

from __future__ import annotations

import re
import sqlite3
import threading
import time
from datetime import datetime
from pathlib import Path
from typing import Any

try:
    from astrbot.api import logger
except ImportError:  # 单测与独立运行时没有 AstrBot
    import logging

    logger = logging.getLogger("astrbot")

# 只有这种名字的文件是自动备份；手工留的 wave_memory_before_*.db 等不参与间隔判断和轮换。
AUTO_BACKUP_PATTERN = re.compile(r"wave_memory_\d{8}_\d{6}\.db")


class DatabaseBackupManager:
    """Manages periodic, non-blocking SQLite database backups and retention.

    v5 用 ``shutil.copy2`` 直接复制正在使用的数据库文件：还在 WAL 里、没合并进主文件的
    写入会漏掉，复制期间有写入还可能得到不一致的副本；判断"上一份备份"时又按文件名
    排序，会挑到很早的手工备份。v6 改用 SQLite 在线备份接口（读取已提交的 WAL 内容，
    得到一致快照），只按自动备份文件的修改时间判断间隔，并在后台线程里执行，不阻塞启动。
    """

    def __init__(self, data_dir: str, config: dict[str, Any] | None = None) -> None:
        self.data_dir = data_dir
        self.config = config or {}
        self.backup_dir = Path(self.data_dir) / "backups"
        self.db_file = Path(self.data_dir) / "wave_memory.db"
        self._thread: threading.Thread | None = None
        self.last_result: dict[str, Any] = {}

    def auto_backups(self) -> list[Path]:
        if not self.backup_dir.is_dir():
            return []
        return sorted(
            (f for f in self.backup_dir.glob("wave_memory_*.db") if AUTO_BACKUP_PATTERN.fullmatch(f.name)),
            key=lambda f: f.stat().st_mtime,
        )

    def _due(self, min_interval_seconds: int) -> bool:
        backups = self.auto_backups()
        if not backups:
            return True
        return (time.time() - backups[-1].stat().st_mtime) >= min_interval_seconds

    def run_backup_safe(self, *, min_interval_seconds: int = 3600, background: bool = True) -> bool:
        """到期时做一次备份；默认在后台线程执行。返回是否开始了备份。"""
        if not self.db_file.exists():
            return False
        try:
            self.backup_dir.mkdir(exist_ok=True)
            if not self._due(min_interval_seconds):
                logger.debug("[WaveMemory] Backup skipped (recent backup exists)")
                return False
        except Exception as exc:
            logger.warning(f"[WaveMemory] DB backup check failed (non-fatal): {exc}")
            return False
        if self._thread is not None and self._thread.is_alive():
            return False
        if not background:
            return self._backup_once()
        self._thread = threading.Thread(target=self._backup_once, name="wave-memory-backup", daemon=True)
        self._thread.start()
        return True

    def _backup_once(self) -> bool:
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        backup_file = self.backup_dir / f"wave_memory_{timestamp}.db"
        partial = backup_file.with_suffix(".db.partial")
        started = time.time()
        try:
            source = sqlite3.connect(f"{self.db_file.resolve().as_uri()}?mode=ro", uri=True, timeout=30.0)
            try:
                target = sqlite3.connect(str(partial))
                try:
                    # 一步拷完：整个过程在同一个读快照里完成。WAL 模式下读不挡写，
                    # 分页拷贝反而会在别的连接写入时从头重来，消息多时可能永远拷不完。
                    source.backup(target, pages=-1)
                    target.execute("PRAGMA schema_version").fetchone()
                finally:
                    target.close()
            finally:
                source.close()
            partial.replace(backup_file)
            elapsed = round(time.time() - started, 1)
            self.last_result = {"ok": True, "file": backup_file.name, "elapsed_s": elapsed, "at": time.time()}
            logger.info(f"[WaveMemory] DB backup created: {backup_file.name} ({elapsed}s)")
            self._prune_stale_backups()
            return True
        except Exception as exc:
            self.last_result = {"ok": False, "error": str(exc), "at": time.time()}
            logger.warning(f"[WaveMemory] DB backup failed (non-fatal): {exc}")
            try:
                partial.unlink(missing_ok=True)
            except OSError:
                pass
            return False

    def _prune_stale_backups(self) -> None:
        try:
            max_backups = max(1, int(self.config.get("backup_max_count", 1)))
        except (TypeError, ValueError):
            max_backups = 1

        for old_file in self.auto_backups()[:-max_backups]:
            try:
                old_file.unlink()
                for suffix in ("-wal", "-shm"):
                    Path(f"{old_file}{suffix}").unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(f"[WaveMemory] stale backup cleanup failed: {exc}")
