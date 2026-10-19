"""v2.1.0 数据清理迁移 — bot_id 统一 + 互动计数清零 + jargon 字段补齐"""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def run_migration(db_path: str, bot_identifiers: dict = None):
    """执行 v2.1 数据清理。
    
    Args:
        db_path: wave_memory.db 路径
        bot_identifiers: 由 Bot 注册表生成，含 qq_ids / db_ids / names 与
            qq_to_db（QQ 号 → db_id，旧数据里以 QQ 号记的 bot_id 统一成 db_id）；
            不传则跳过 bot_id 统一这一步
    """
    bot_identifiers = bot_identifiers or {}

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # --- 1. bot_id 统一为 db_id ---
        # 检查 beliefs 表是否存在
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='beliefs'")
        if cursor.fetchone():
            # 以 QQ 号记的 bot_id 统一成该 Bot 的 db_id
            for qq_id, db_id in (bot_identifiers.get("qq_to_db") or {}).items():
                if not qq_id or not db_id:
                    continue
                cursor.execute("UPDATE beliefs SET bot_id = ? WHERE bot_id = ?", (str(db_id), str(qq_id)))
                if cursor.rowcount:
                    logger.info(f"[Migration v2.1] Unified {cursor.rowcount} beliefs bot_id {qq_id} -> {db_id}")
        
        # --- 2. 互动计数清零 ---
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_profiles'")
        if cursor.fetchone():
            cursor.execute("UPDATE user_profiles SET interaction_count = 0")
            updated = cursor.rowcount
            logger.info(f"[Migration v2.1] Reset interaction_count for {updated} profiles")
        
        # --- 3. jargon 表加 last_infer_freq 字段（为批次2准备）---
        cursor.execute("PRAGMA table_info(jargon)")
        columns = [row[1] for row in cursor.fetchall()]
        if "last_infer_freq" not in columns:
            cursor.execute("ALTER TABLE jargon ADD COLUMN last_infer_freq INTEGER DEFAULT 0")
            logger.info("[Migration v2.1] Added jargon.last_infer_freq column")
        
        conn.commit()
        logger.info("[Migration v2.1] All migrations completed successfully")
        return True
        
    except Exception as e:
        conn.rollback()
        logger.error(f"[Migration v2.1] Failed: {e}")
        return False
    finally:
        conn.close()


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        run_migration(sys.argv[1])
    else:
        print("Usage: python v2_1_cleanup.py <path_to_wave_memory.db>")
