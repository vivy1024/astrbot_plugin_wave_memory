"""每个 Bot 的书设语料：Profile.persona.lore_corpus 决定读不读、读哪份。"""

from __future__ import annotations

import asyncio

from domain.bot_profile import BotProfile, bot_catalog_scope
from domain.scope import CatalogScope

from test_book_lore_channel import BookLoreChannelTest, FakeBookLoreIndex, FakeEmbedding

DEFAULT = CatalogScope(catalog_id="book-lore", corpus_id="unit-test", version="v1")


def _profile(corpus: str) -> BotProfile:
    return BotProfile.from_dict({"db_id": "bot-alpha", "name": "阿尔法", "qq_id": "1", "persona": {"lore_corpus": corpus}})


def test_scope_resolution_rules():
    assert bot_catalog_scope(None, DEFAULT) == (DEFAULT, "")
    assert bot_catalog_scope(_profile(""), DEFAULT) == (DEFAULT, "")
    assert bot_catalog_scope(_profile("unit-test"), DEFAULT) == (DEFAULT, "")
    assert bot_catalog_scope(_profile("None"), DEFAULT) == (None, "bot_book_lore_disabled")
    assert bot_catalog_scope(_profile("other-book"), DEFAULT) == (None, "corpus_not_loaded:other-book")
    assert bot_catalog_scope(_profile("other-book"), None) == (None, "catalog_scope_required")


def test_profile_round_trips_lore_corpus():
    profile = _profile("unit-test")
    assert BotProfile.from_dict(profile.to_dict()).persona.lore_corpus == "unit-test"
    assert BotProfile.from_dict({"db_id": "b", "name": "b", "qq_id": "2"}).persona.lore_corpus == ""


class BotLoreCorpusChannelTest(BookLoreChannelTest):
    def _channel(self, corpus):
        from services.injection.channels.book_lore import BookLoreChannel

        profiles = {"bot-alpha": _profile(corpus)}
        return BookLoreChannel(
            book_lore_index=FakeBookLoreIndex([(1, 0.91)]),
            embedding_service=FakeEmbedding(),
            lore_db_path=self._lore_db(),
            catalog_scope=self._catalog_scope(),
            profile_lookup=profiles.get,
        )

    def test_bot_using_loaded_corpus_reads_lore(self):
        result = asyncio.run(self._channel("unit-test").build(self._ctx()))
        self.assertEqual(result.status, "hit")
        self.assertIn("剑阵总纲", result.text)

    def test_bot_opted_out_or_other_corpus_reads_nothing(self):
        off = asyncio.run(self._channel("none").build(self._ctx()))
        other = asyncio.run(self._channel("另一本书").build(self._ctx()))
        self.assertEqual((off.status, off.warnings), ("disabled", ["bot_book_lore_disabled"]))
        self.assertEqual(other.status, "disabled")
        self.assertEqual(other.warnings, ["corpus_not_loaded:另一本书"])


def test_self_reflect_book_lore_follows_bot_corpus(tmp_path):
    import sqlite3

    import test_self_reflect  # noqa: F401  复用它的 astrbot.api 替身
    from services.self_reflect import SelfReflectService

    lore = tmp_path / "book_lore.db"
    conn = sqlite3.connect(lore)
    conn.execute("CREATE TABLE book_communities (id INTEGER PRIMARY KEY, title TEXT, summary TEXT)")
    conn.execute("INSERT INTO book_communities VALUES (1, '剑阵总纲', '先稳阵眼')")
    conn.commit()
    conn.close()
    profiles = {"bot-a": _profile("unit-test"), "bot-b": _profile("none")}
    service = SelfReflectService(
        None, None, FakeEmbedding(), None, FakeBookLoreIndex([(1, 0.9)]), str(lore),
        bot_name="bot", bot_id="bot-a", catalog_scope=DEFAULT, profile_lookup=profiles.get,
    )
    text_a, hits_a = asyncio.run(service._search_book_lore("bot-a", "回复", "纠正"))
    text_b, hits_b = asyncio.run(service._search_book_lore("bot-b", "回复", "纠正"))
    assert "剑阵总纲" in text_a and hits_a[0]["catalog_scope"]["corpus_id"] == "unit-test"
    assert (text_b, hits_b) == ("（无额外参考）", [])
