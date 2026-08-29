from pathlib import Path


class TestStage5SoulScopedReadonly:
    def test_soul_page_uses_explicit_scope_and_read_contract(self):
        page = Path("webui/frontend/src/pages/soul/SoulPage.tsx").read_text(encoding="utf-8")
        api = Path("webui/frontend/src/api/soul.ts").read_text(encoding="utf-8")
        for marker in ("getScopeOptions", "ScopeSelect", "当前群心智", "心情", "关切", "时间线", "ObjectDeepLink", "EvidenceList"):
            assert marker in page + api
        assert "不接受私聊" in page
        assert "PageResponse" in api

    def test_soul_page_exposes_unknown_and_empty_states_without_writes(self):
        page = Path("webui/frontend/src/pages/soul/SoulPage.tsx").read_text(encoding="utf-8")
        for marker in ("QueryState", "未知 / 未记录", "无可信分量"):
            assert marker in page
        # 自省入口存在，但写动作只允许经由 api 层的只读 refresh 通道，页面本身不得内联写请求
        assert "强制自省" in page
        assert "fetch(" not in page
        assert "POST" not in page
        assert "PUT" not in page
