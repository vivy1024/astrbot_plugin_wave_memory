"""Database backup lifecycle manager running safely and asynchronously."""

from __future__ import annotations

import os
import re
import shutil
import time
from datetime import datetime
from pathlib import Path
from typing import Any

from astrbot.api import logger


class DatabaseBackupManager:
    """Manages periodic, non-blocking SQLite database backups and retention."""

    def __init__(self, data_dir: str, config: dict[str, Any] | None = None) -> None:
        self.data_dir = data_dir
        self.config = config or {}
        self.backup_dir = Path(self.data_dir) / "backups"
        self.db_file = Path(self.data_dir) / "wave_memory.db"

    def run_backup_safe(self, *, min_interval_seconds: int = 3600) -> bool:
        """Execute one non-blocking backup pass with timestamp-based retention."""
        if not self.db_file.exists():
            return False

        try:
            self.backup_dir.mkdir(exist_ok=True)
            existing_backups = sorted(self.backup_dir.glob("wave_memory_*.db"))
            if existing_backups:
                last_mtime = existing_backups[-1].stat().st_mtime
                if (time.time() - last_mtime) < min_interval_seconds:
                    logger.debug("[WaveMemory] Backup skipped (recent backup exists)")
                    return False

            timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            backup_file = self.backup_dir / f"wave_memory_{timestamp}.db"
            shutil.copy2(str(self.db_file), str(backup_file))
            logger.info(f"[WaveMemory] DB backup created: {backup_file.name}")

            self._prune_stale_backups()
            return True
        except Exception as exc:
            logger.warning(f"[WaveMemory] DB backup failed (non-fatal): {exc}")
            return False

    def _prune_stale_backups(self) -> None:
        try:
            max_backups = max(1, int(self.config.get("backup_max_count", 1)))
        except (TypeError, ValueError):
            max_backups = 1

        auto_backups = sorted(
            (
                f for f in self.backup_dir.glob("wave_memory_*.db")
                if re.fullmatch(r"wave_memory_\d{8}_\d{6}\.db", f.name)
            ),
            key=lambda f: f.stat().st_mtime,
        )
        for old_file in auto_backups[:-max_backups]:
            try:
                old_file.unlink()
            except OSError as exc:
                logger.warning(f"[WaveMemory] stale backup cleanup failed: {exc}")
