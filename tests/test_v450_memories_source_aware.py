from pathlib import Path


class TestStage5ScopedMemories:
    def test_memories_page_requires_explicit_scope_and_server_options(self):
        page = Path("webui/frontend/src/pages/memories/MemoriesPage.tsx").read_text(encoding="utf-8")
        for marker in (
            "getScopeOptions",
            "scopeOptionsFor",
            "ScopeSelect",
            "bot_id",
            "session_id",
            "请选择 Bot 和群",
            "不会从裸编号猜当前群",
        ):
            assert marker in page

    def test_memories_page_uses_page_response_and_opaque_object_links(self):
        page = Path("webui/frontend/src/pages/memories/MemoriesPage.tsx").read_text(encoding="utf-8")
        api = Path("webui/frontend/src/api/memories.ts").read_text(encoding="utf-8")
        for marker in ("PaginationControls", "payload.page", "QueryState", "item.ref", "item.detail_url", "mutation_url", "ObjectRefDescriptor", "PageResponse"):
            assert marker in page + api
        assert "?id=" not in page

    def test_memories_mutations_reuse_server_issued_urls_and_revisions(self):
        page = Path("webui/frontend/src/pages/memories/MemoriesPage.tsx").read_text(encoding="utf-8")
        for marker in ("updateMemory(detail.mutation_url", "deleteMemory(detail.mutation_url", "result.item", "保存并重新读取", "删除当前记忆"):
            assert marker in page
