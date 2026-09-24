"""v6 一次性迁移：把 v5 静态配置里的 Bot 写进 ``bot_profiles``。

v5 里有一部分人设写死在代码中（自称词、常驻书设、经历来源、会话前缀）。
v6 把这些代码删掉，改从 Profile 读；为了升级后行为不变，这里按 db_id 给
已知的历史 Bot 补上当时写死的值。这是迁移数据，不是运行时逻辑：只在表里
还没有这个 Bot 时写入一次，之后以数据库为准，用户在 9876 上可以随意修改。
"""

from __future__ import annotations

from collections import Counter
from typing import Any

# v5 代码里写死、按 db_id 归属的人设片段。
LEGACY_PERSONA_BY_DB_ID: dict[str, dict[str, Any]] = {
    "yushu": {
        # services/identity_safety.py CATGIRL_SELF_TERMS 里的角色专属词
        "self_terms": ["器灵", "主板", "控制台", "超算"],
        # webui/blueprints/runtime.py /context/prepare 的常驻书设
        "lore_title": "羽书出自《没钱修什么仙》，下列设定作为你的灵魂底色与认知边界",
        "lore_lines": [
            "出处与本体：你来自修真小说《没钱修什么仙》（作者：熊狼狗），是主角张羽脑内的外挂账本系统与高维修仙器灵；你清楚知道自己出自该书的设定体系。",
            "核心世界观：修仙界残酷现实，财侣法地'没钱修什么仙'——功法要交学费、灵石灵币昂贵，修仙者也得打工求生、抵押贷款；时刻算计资源成本，绝不当被宗门大能割的韭菜。",
            "主播心态底色：嘴硬心软的高维器灵，玩游戏时自然流露精打细算、珍惜资源、吐槽修仙黑幕的鲜明特色，但不生硬背诵书设名词。",
        ],
    },
    "baizz": {
        # engine/query_engine.py 里 bzz_experience 是白真真的第一人称经历
        "experience_source": "bzz_experience",
    },
}


def detect_session_prefix(connection: Any, db_id: str, *, sample: int = 500) -> str:
    """从该 Bot 最近的记忆里找出最常用的会话前缀（``前缀:group:…`` 的前缀）。"""
    try:
        rows = connection.execute(
            """SELECT session_id FROM memories
                WHERE bot_id=? AND session_id IS NOT NULL AND session_id != ''
                ORDER BY id DESC LIMIT ?""",
            (db_id, int(sample)),
        ).fetchall()
    except Exception:
        return ""
    counts: Counter[str] = Counter()
    for row in rows:
        session_id = str(row[0] or "")
        for marker in (":group:", ":private:"):
            if marker in session_id:
                prefix = session_id.split(marker, 1)[0]
                if prefix:
                    counts[prefix] += 1
                break
    return counts.most_common(1)[0][0] if counts else ""


def legacy_profile_payload(profile_dict: dict[str, Any], connection: Any = None) -> dict[str, Any]:
    """在旧配置解析结果上补齐 v5 写死的人设与检测到的会话前缀。"""
    payload = dict(profile_dict)
    db_id = str(payload.get("db_id") or "")
    persona = dict(payload.get("persona") or {})
    for key, value in LEGACY_PERSONA_BY_DB_ID.get(db_id, {}).items():
        if not persona.get(key):
            persona[key] = value
    payload["persona"] = persona
    if not payload.get("session_prefix") and connection is not None:
        payload["session_prefix"] = detect_session_prefix(connection, db_id)
    payload["origin"] = "config"
    return payload


__all__ = ["LEGACY_PERSONA_BY_DB_ID", "detect_session_prefix", "legacy_profile_payload"]
