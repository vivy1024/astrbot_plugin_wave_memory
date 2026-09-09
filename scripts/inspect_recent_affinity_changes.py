import sqlite3, datetime, json
from pathlib import Path

db_curr = '/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory.db'
db_pre_noise = '/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory_before_noise_purge_1788741704.db'
db_pre_wipe = '/AstrBot/data/plugin_data/astrbot_plugin_wave_memory/wave_memory_before_affinity_wipe_1788679982.db'

cst_tz = datetime.timezone(datetime.timedelta(hours=8))
# 过去两天：2026-09-05 00:00:00 起
t_start = datetime.datetime(2026, 9, 5, 0, 0, 0, tzinfo=cst_tz).timestamp()

for label, db_path in [("CURRENT_DB", db_curr), ("PRE_NOISE_DB", db_pre_noise), ("PRE_WIPE_DB", db_pre_wipe)]:
    p = Path(db_path)
    if not p.exists():
        continue
    con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    print(f"\n==================== {label} ({p.name}) ====================")
    
    # 1. 关系事件
    try:
        events = con.execute(
            """SELECT id, bot_id, session_id, subject_principal_id, event_type, dimension, delta, reason, created_at 
               FROM scoped_soul_relationship_events 
               WHERE created_at >= ? 
               ORDER BY created_at DESC LIMIT 50""", 
            (t_start,)
        ).fetchall()
        print(f"--- scoped_soul_relationship_events (>= 09-05, count: {len(events)}) ---")
        for e in events[:25]:
            t_str = datetime.datetime.fromtimestamp(float(e['created_at']), tz=cst_tz).strftime('%m-%d %H:%M:%S')
            print(f"[{t_str}] {e['subject_principal_id']} | {e['event_type']} | {e['dimension']} {e['delta']:+g} | {e['reason']}")
    except Exception as exc:
        print("events err:", exc)

    # 2. 印象记录
    try:
        rows = con.execute(
            """SELECT user_id, group_id, bot_id, nickname, affection, metadata 
               FROM user_profiles 
               WHERE metadata LIKE '%impression%'"""
        ).fetchall()
        
        impressions = []
        histories = []
        for r in rows:
            meta = {}
            try:
                meta = json.loads(r['metadata'])
            except:
                continue
            if not isinstance(meta, dict):
                continue
            name = r['nickname'] or r['user_id']
            imp = meta.get('impression')
            imp_at = meta.get('impression_updated_at')
            if imp and imp_at and imp_at >= t_start:
                impressions.append((imp_at, name, r['group_id'], imp, r['affection'], meta.get('dimensions')))
            for h in meta.get('impression_history') or []:
                if isinstance(h, dict):
                    at = h.get('at') or h.get('updated_at') or h.get('cleared_at') or h.get('superseded_at')
                    if at and at >= t_start:
                        histories.append((at, name, r['group_id'], h))

        impressions.sort(key=lambda x: x[0], reverse=True)
        histories.sort(key=lambda x: x[0], reverse=True)
        print(f"\n--- user_profiles impressions updated >= 09-05 (count: {len(impressions)}) ---")
        for at, name, gid, text, aff, dims in impressions[:25]:
            t_str = datetime.datetime.fromtimestamp(float(at), tz=cst_tz).strftime('%m-%d %H:%M:%S')
            print(f"[{t_str}] [{name}] (群 {gid}, aff={aff}) -> 印象: {text}")

        print(f"\n--- user_profiles impression_history entries >= 09-05 (count: {len(histories)}) ---")
        for at, name, gid, h in histories[:25]:
            t_str = datetime.datetime.fromtimestamp(float(at), tz=cst_tz).strftime('%m-%d %H:%M:%S')
            txt = h.get('text') or h.get('phrase') or ''
            actor = h.get('actor') or ''
            print(f"[{t_str}] [{name}] | {actor} | {txt}")
    except Exception as exc:
        print("profiles err:", exc)
    con.close()
