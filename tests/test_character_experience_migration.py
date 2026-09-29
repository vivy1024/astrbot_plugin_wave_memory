"""v5 旧经历的一次性迁移：幂等、缺数据安全跳过、目标 Bot 缺失时不写标记、对上书设笔记带上阶段与时间、
迁移后旧的无归属行不再被普通记忆召回（向量兜底过滤与原词检索）。

迁移映射本身（bzz_experience → baizz、白真真经历）是迁移数据；这里的角色名、正文、QQ 号全部虚构。
"""

from __future__ import annotations

import asyncio
import sqlite3
import time

import numpy as np
import pytest

from character_experience_support import DIM, bot, vector_for
from domain import bot_identity
from domain.bot_profile import BotProfile
from domain.scope import RuntimeScope, SessionRef
from engine.database import WaveMemoryDB
from engine.db.migrations.character_experience import (
    build_legacy_items,
    migrate_legacy_character_experiences,
    parse_legacy_note,
)
from engine.query_engine import QueryEngine
from services.bot_registry import BotRegistry
from services.character_experience import CharacterExperienceService
from services.few_shot.service import FewShotService
from services.injection.channels.fts5 import FTS5Channel

LEGACY = [
    # (正文, group_id=旧脚本写的 arc, 对应书设笔记 id, 笔记标题)
    ("那天我在山门口捡到一只受伤的小鹿，偷偷养在柴房里。", "arc01_山中岁月", "bzz_ch0002_s00_0", "ch2 山中岁月开端 [第1年春]"),
    ("小鹿好了以后跑回了林子，我在柴房门口坐了一下午。", "arc01_山中岁月", "bzz_ch0002_s00_1", "ch2 山中岁月开端 [第1年春]"),
    ("第一次坐马车进城读书，我紧张得一路没说话。", "arc02_书院求学", "bzz_ch0010_s03_2", "ch10 进城 [第2年秋]"),
    ("书院里没人认识我，这条对不上任何笔记。", "arc02_书院求学", None, None),
]


def _db_with_legacy(tmp_path, rows=LEGACY):
    db = WaveMemoryDB(str(tmp_path / "wave.sqlite3"), dimension=DIM)
    for content, arc, _note, _title in rows:
        db.conn.execute(
            """INSERT INTO memories (group_id, sender_id, sender_name, content, vector, timestamp, importance, memory_type, source)
               VALUES (?, '[测试角色]', '[测试角色经历]', ?, ?, 1700000000, 0.8, 'experience', 'bzz_experience')""",
            (arc, content, vector_for(content).tobytes()),
        )
    db.conn.commit()
    return db


def _lore_db(tmp_path, rows=LEGACY) -> str:
    path = tmp_path / "book_lore.db"
    conn = sqlite3.connect(path)
    conn.execute("""CREATE TABLE book_notes (id TEXT PRIMARY KEY, book_name TEXT NOT NULL, arc TEXT DEFAULT '', category TEXT DEFAULT '',
                    title TEXT NOT NULL, content TEXT, source_file TEXT DEFAULT '', vector BLOB)""")
    for content, arc, note_id, title in rows:
        if note_id:
            conn.execute("INSERT INTO book_notes VALUES (?, '虚构小说', ?, '白真真经历', ?, ?, '', NULL)", (note_id, arc, title, content))
    conn.execute("INSERT INTO book_notes VALUES ('note_ch1', '虚构小说', 'arc01', '章节事件', '第1章', '世界观内容', '', NULL)")
    conn.commit()
    conn.close()
    return str(path)


def _registry(db, *bots):
    registry = BotRegistry(None)
    registry.attach(db.bot_profiles, connection=db.conn)
    for data in bots:
        registry.save(BotProfile.from_dict(data), expected_version=0, changed_by="test")
    return registry


def _service(db, registry, lore=""):
    return CharacterExperienceService(repo=db.character_experiences, registry=registry, lore_db_path=lore, dimension=DIM)


@pytest.fixture(autouse=True)
def _reset_identity():
    yield
    bot_identity.publish()


def test_migration_imports_legacy_rows_once_and_enables_bot(tmp_path):
    db = _db_with_legacy(tmp_path)
    registry = _registry(db, bot("baizz", "测试角色", "10000001"), bot("oc_other", "路人甲", "10000002"))
    service = _service(db, registry, _lore_db(tmp_path))

    [report] = service.migrate_legacy()
    assert report["status"] == "applied" and report["imported"] == 4 and report["matched_notes"] == 3
    assert report["stage_keys"] == ["arc01_山中岁月", "arc02_书院求学"] and report["last_stage"] == "arc02_书院求学"

    repo = db.character_experiences
    items, total = repo.list("baizz", limit=10)
    assert total == 4 and {item["status"] for item in items} == {"active"}
    assert all(item["canon_level"] == "canon" and item["source_kind"] == "legacy_migration" for item in items)
    first = next(item for item in items if item["content"].startswith("那天我在山门口"))
    assert first["stage_key"] == "arc01_山中岁月" and first["story_time"] == "第1年春" and first["plot_stage"] == "山中岁月开端"
    assert first["source_ref"]["chapter"] == 2 and first["source_ref"]["book_note_id"] == "bzz_ch0002_s00_0"
    assert first["source_ref"]["legacy_source"] == "bzz_experience" and first["has_vector"], "同维度向量直接带过来"
    unmatched = next(item for item in items if "对不上" in item["content"])
    assert unmatched["stage_key"] == "arc02_书院求学" and unmatched["story_time"] == "", "对不上笔记的保留正文、时间留空"
    # 按章节 / 场景顺序排
    assert [item["content"][:4] for item in items[:2]] == ["那天我在", "小鹿好了"]
    stages = repo.list_stages("baizz")
    assert [(s["stage_key"], s["title"], s["ordinal"]) for s in stages] == [("arc01_山中岁月", "山中岁月", 1), ("arc02_书院求学", "书院求学", 2)]
    assert stages[0]["story_time"] == "第1年春"

    profile = registry.get("baizz")
    settings = profile.character_experience
    assert settings.enabled and settings.current_stage == "arc02_书院求学", "当前时间点 = 最后一个阶段（她能想起全部）"
    assert settings.legacy_sources == ["bzz_experience"] and settings.lore_categories == ["白真真经历"]
    assert "bzz_experience" in bot_identity.taken_over_sources()
    assert "白真真经历" in bot_identity.personal_lore_categories()
    assert not registry.get("oc_other").character_experience.enabled

    version = profile.version
    [again] = service.migrate_legacy()
    assert again["status"] == "already_applied"
    assert repo.counts("baizz")["total"] == 4 and registry.get("baizz").version == version
    # 旧行不删除、不修改
    rows = db.conn.execute("SELECT content, COALESCE(bot_id, '') FROM memories WHERE source='bzz_experience' ORDER BY id").fetchall()
    assert [r[0] for r in rows] == [r[0] for r in LEGACY] and {r[1] for r in rows} == {""}
    db.close()


def test_migration_skips_safely_without_legacy_rows(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "wave.sqlite3"), dimension=DIM)
    registry = _registry(db, bot("baizz", "测试角色", "10000001"))
    service = _service(db, registry)
    [report] = service.migrate_legacy()
    assert report["status"] == "nothing_to_migrate"
    assert db.character_experiences.counts("baizz")["total"] == 0
    assert not registry.get("baizz").character_experience.enabled
    assert service.migrate_legacy()[0]["status"] == "already_applied", "写了标记，之后不再扫描记忆表"
    db.close()


def test_lore_only_library_still_registers_personal_category(tmp_path):
    db = WaveMemoryDB(str(tmp_path / "wave.sqlite3"), dimension=DIM)
    registry = _registry(db, bot("baizz", "测试角色", "10000001"))
    service = _service(db, registry, _lore_db(tmp_path))
    [report] = service.migrate_legacy()
    assert report["status"] == "nothing_to_migrate" and report["lore_notes"] == 3
    settings = registry.get("baizz").character_experience
    assert settings.lore_categories == ["白真真经历"] and not settings.enabled
    db.close()


def test_missing_target_bot_waits_without_marker(tmp_path):
    db = _db_with_legacy(tmp_path)
    registry = _registry(db, bot("oc_other", "路人甲", "10000002"))
    service = _service(db, registry)
    assert service.migrate_legacy()[0]["status"] == "target_bot_missing"
    assert db.character_experiences.get_migration("legacy:bzz_experience->baizz") is None
    registry.save(BotProfile.from_dict(bot("baizz", "测试角色", "10000001")), expected_version=0)
    [report] = service.migrate_legacy()
    assert report["status"] == "applied" and report["imported"] == 4
    db.close()


def test_dry_run_previews_without_writing(tmp_path):
    db = _db_with_legacy(tmp_path)
    registry = _registry(db, bot("baizz", "测试角色", "10000001"))
    service = _service(db, registry, _lore_db(tmp_path))
    [report] = service.migrate_legacy(dry_run=True)
    assert report["status"] == "preview" and report["would_import"] == 4
    assert db.character_experiences.counts("baizz")["total"] == 0 and not registry.get("baizz").character_experience.enabled
    assert db.character_experiences.get_migration("legacy:bzz_experience->baizz") is None
    db.close()


def test_profile_update_retried_on_next_run_when_it_failed(tmp_path):
    db = _db_with_legacy(tmp_path)
    registry = _registry(db, bot("baizz", "测试角色", "10000001"))

    class Conflict(Exception):
        code = "version_conflict"

    def failing_save(profile, expected_version):
        raise Conflict()

    [first] = migrate_legacy_character_experiences(
        db.character_experiences, profile_get=registry.get_any, profile_save=failing_save, dimension=DIM,
    )
    assert first["status"] == "applied" and first["profile_updated"] is False
    assert not registry.get("baizz").character_experience.enabled
    [second] = migrate_legacy_character_experiences(
        db.character_experiences, profile_get=registry.get_any,
        profile_save=lambda profile, version: registry.save(profile, expected_version=version),
        dimension=DIM,
    )
    assert second["status"] == "applied" and second["profile_updated"] is True
    assert registry.get("baizz").character_experience.enabled
    assert db.character_experiences.counts("baizz")["total"] == 4, "只补配置，不重复导入"
    db.close()


def test_generic_mapping_and_note_parsing():
    assert parse_legacy_note({"id": "x_ch0012_s03_7", "title": "ch12 某个剧情 [第3年冬]", "arc": "arc02", "book_name": "虚构"}) == {
        "book_note_id": "x_ch0012_s03_7", "arc": "arc02", "chapter": 12, "scene_idx": 3, "line": 7,
        "plot_stage": "某个剧情", "story_time": "第3年冬", "corpus": "虚构",
    }
    items, stages, stats = build_legacy_items(
        [{"id": 5, "content": "我在灯会上走丢了。", "group_id": "old_exp", "vector": b"\x00" * 3}],
        [], source="old_exp", lore_category="", dimension=DIM,
    )
    assert items[0]["stage_key"] == "" and items[0]["vector"] is None and stages == []
    assert stats == {"rows": 1, "items": 1, "matched_notes": 0, "with_vector": 0, "stages": 0}


def _group_scope(bot_id="oc_other", group="g1"):
    return RuntimeScope(bot_id, "group", SessionRef(f"qq:group:{group}", "qq", "group", group), subject_principal_id="qq:user:10000009")


def test_legacy_rows_no_longer_recalled_after_migration(tmp_path):
    db = _db_with_legacy(tmp_path)
    registry = _registry(db, bot("baizz", "测试角色", "10000001"), bot("oc_other", "路人甲", "10000002"))
    fts = FTS5Channel(db=db, cross_group_enabled=True)
    before = fts.search_scoped(_group_scope(), "小鹿柴房", top_k=5)
    assert before and {row["source"] for row in before} == {"bzz_experience"}, "迁移前无归属的旧行对任何 Bot 都能被原词检索到"

    _service(db, registry, _lore_db(tmp_path)).migrate_legacy()
    assert fts.search_scoped(_group_scope(), "小鹿柴房", top_k=5) == []
    assert fts.search_scoped(_group_scope("baizz"), "小鹿柴房", top_k=5) == [], "她自己也改由角色经历通道想起，不重复"
    # 向量召回路径的兜底过滤（QueryEngine 对全部召回结果剔除已接管的来源）
    kept = QueryEngine._drop_taken_over_sources([
        {"id": 1, "source": "bzz_experience"}, {"id": 2, "source": "chat"},
    ])
    assert [m["id"] for m in kept] == [2]
    db.close()


def test_taken_over_sources_are_not_few_shot_style_candidates(tmp_path):
    """风格样例候选也不再拿已接管的旧经历（仍在 persona.experience_source 里的旧来源照常参与）。"""
    db = WaveMemoryDB(str(tmp_path / "wave.sqlite3"), dimension=DIM)
    now = time.time()
    rows = (
        ("今天群里有人问我喜欢什么花，我说喜欢山里的野菊。", "bot_reply"),
        ("那天我在山门口捡到一只受伤的小鹿，偷偷养在柴房里。", "old_exp"),
        ("我第一次学写字的时候把墨汁打翻在了书上。", "demo_exp"),
    )
    for content, source in rows:
        db.conn.execute(
            """INSERT INTO memories (group_id, sender_id, sender_name, content, timestamp, importance, memory_type, source)
               VALUES ('g1', '10000001', '测试角色', ?, ?, 0.5, 'chat', ?)""",
            (content, now, source),
        )
    db.conn.commit()
    bot_identity.publish(experience_sources=["demo_exp", "old_exp"], taken_over_sources=["old_exp"])
    service = FewShotService(db, llm_client=object())

    async def evaluate(_content):
        return {"score": 1.0, "traits": []}

    service._evaluate_style = evaluate
    seen = {item["content"] for item in asyncio.run(service.extract_candidates())}
    assert seen == {rows[0][0], rows[2][0]}
    db.close()
