"""v2.1.0 数据清理迁移 — bot 自我 facts 清理 + bot_id 统一 + 互动计数清零"""

import sqlite3
import logging
from pathlib import Path

logger = logging.getLogger(__name__)


def run_migration(db_path: str, bot_identifiers: dict = None):
    """执行 v2.1 数据清理。
    
    Args:
        db_path: wave_memory.db 路径
        bot_identifiers: 由 Bot 注册表生成，{
            "qq_ids": [...], "db_ids": [...], "names": [...],
            "qq_to_db": {QQ 号: db_id}   # 旧数据里以 QQ 号记的 bot_id 统一成 db_id
        }；不传则跳过与 Bot 身份有关的两步
    """
    bot_identifiers = bot_identifiers or {}

    conn = sqlite3.connect(db_path)
    cursor = conn.cursor()
    
    try:
        # --- 1. 删除 bot 自我 facts ---
        all_bot_subjects = [
            str(item)
            for key in ("qq_ids", "db_ids", "names")
            for item in bot_identifiers.get(key) or []
            if item
        ]
        placeholders = ",".join(["?"] * len(all_bot_subjects))
        count = 0
        if all_bot_subjects:
            cursor.execute(
                f"SELECT COUNT(*) FROM facts WHERE subject IN ({placeholders})",
                all_bot_subjects
            )
            count = cursor.fetchone()[0]

        if count > 0:
            cursor.execute(
                f"DELETE FROM facts WHERE subject IN ({placeholders})",
                all_bot_subjects
            )
            logger.info(f"[Migration v2.1] Deleted {count} bot self-facts")
        
        # --- 2. bot_id 统一为 db_id ---
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
        
        # --- 3. 互动计数清零 ---
        cursor.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='user_profiles'")
        if cursor.fetchone():
            cursor.execute("UPDATE user_profiles SET interaction_count = 0")
            updated = cursor.rowcount
            logger.info(f"[Migration v2.1] Reset interaction_count for {updated} profiles")
        
        # --- 4. jargon 表加 last_infer_freq 字段（为批次2准备）---
        cursor.execute("PRAGMA table_info(jargon)")
        columns = [row[1] for row in cursor.fetchall()]
        if "last_infer_freq" not in columns:
            cursor.execute("ALTER TABLE jargon ADD COLUMN last_infer_freq INTEGER DEFAULT 0")
            logger.info("[Migration v2.1] Added jargon.last_infer_freq column")
        
        # --- 5. facts 表加 last_reinforced 字段（为批次3准备）---
        cursor.execute("PRAGMA table_info(facts)")
        columns = [row[1] for row in cursor.fetchall()]
        if "last_reinforced" not in columns:
            cursor.execute("ALTER TABLE facts ADD COLUMN last_reinforced REAL")
            # 用 created_at 初始化
            cursor.execute("UPDATE facts SET last_reinforced = created_at WHERE last_reinforced IS NULL")
            logger.info("[Migration v2.1] Added facts.last_reinforced column")

        # --- 6. facts 表加 fact_type 字段（原子类型分类）---
        cursor.execute("PRAGMA table_info(facts)")
        columns = [row[1] for row in cursor.fetchall()]
        if "fact_type" not in columns:
            cursor.execute("ALTER TABLE facts ADD COLUMN fact_type TEXT DEFAULT 'FACTUAL'")
            logger.info("[Migration v2.1] Added facts.fact_type column")
        
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
