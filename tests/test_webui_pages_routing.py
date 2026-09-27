import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path
from urllib.parse import parse_qs


def _load_pages_module():
    class Blueprint:
        def __init__(self, *args, **kwargs):
            pass

        def route(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

        def app_errorhandler(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

    previous_quart = sys.modules.get("quart")
    sys.modules["quart"] = types.SimpleNamespace(Blueprint=Blueprint)
    try:
        import webui.blueprints.pages as pages
        return importlib.reload(pages)
    finally:
        if previous_quart is None:
            sys.modules.pop("quart", None)
        else:
            sys.modules["quart"] = previous_quart


class WebUIPagesRoutingTest(unittest.IsolatedAsyncioTestCase):
    async def test_index_serves_built_react_app_only(self):
        pages = _load_pages_module()
        original_static_dir = pages._STATIC_DIR
        with tempfile.TemporaryDirectory() as tmp:
            static_dir = Path(tmp)
            (static_dir / "app").mkdir()
            (static_dir / "app" / "index.html").write_text("<html>React WebUI</html>", encoding="utf-8")
            (static_dir / "index.html").write_text("<html>obsolete page</html>", encoding="utf-8")
            pages._STATIC_DIR = static_dir
            try:
                body, status, headers = await pages.index()
            finally:
                pages._STATIC_DIR = original_static_dir
        self.assertEqual(status, 200)
        self.assertEqual(headers["Content-Type"], "text/html")
        self.assertIn("React WebUI", body)
        self.assertNotIn("obsolete page", body)

    async def test_index_fails_closed_when_built_app_is_missing(self):
        pages = _load_pages_module()
        original_static_dir = pages._STATIC_DIR
        with tempfile.TemporaryDirectory() as tmp:
            pages._STATIC_DIR = Path(tmp)
            try:
                body, status, _ = await pages.index()
            finally:
                pages._STATIC_DIR = original_static_dir
        self.assertEqual(status, 503)
        self.assertIn("built WebUI is unavailable", body)

    async def test_legacy_explore_redirects_to_react_graph_and_maintain_redirects(self):
        pages = _load_pages_module()
        body, explore_status, explore_headers = await pages.explore()
        _, maintain_status, maintain_headers = await pages.maintain()
        self.assertEqual(explore_status, 302)
        self.assertEqual(body, "")
        self.assertEqual(explore_headers["Location"], "/#/graph?layer=kg")
        self.assertEqual(maintain_status, 302)
        self.assertEqual(maintain_headers["Location"], "/#/maintenance")

    def test_graph_redirect_keeps_scope_query_and_drops_iframe_only_params(self):
        pages = _load_pages_module()
        location = pages.graph_redirect_location(
            "kg", "bot_id=yushu&session_id=%E7%BE%BD%E4%B9%A6%3Agroup%3A42&visibility=group&embed=1&layer=facts"
        )
        self.assertTrue(location.startswith("/#/graph?"))
        query = parse_qs(location.split("?", 1)[1])
        self.assertEqual(query["layer"], ["kg"])
        self.assertEqual(query["bot_id"], ["yushu"])
        self.assertEqual(query["session_id"], ["羽书:group:42"])
        self.assertEqual(query["visibility"], ["group"])
        self.assertNotIn("embed", query)
        self.assertEqual(pages.graph_redirect_location("kg"), "/#/graph?layer=kg")

    def test_legacy_3d_static_assets_are_removed(self):
        static = Path("webui/static")
        for name in ("explore.html", "kg.js", "kg-config.js", "vendor"):
            self.assertFalse((static / name).exists(), f"旧神经云图资源 {name} 应已下线")


if __name__ == "__main__":
    unittest.main()
