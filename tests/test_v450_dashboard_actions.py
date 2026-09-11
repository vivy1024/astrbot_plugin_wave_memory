from pathlib import Path


class TestStage5DashboardActions:
    def test_dashboard_links_only_to_canonical_management_pages(self):
        page = Path("webui/frontend/src/pages/dashboard/DashboardPage.tsx").read_text(encoding="utf-8")
        for marker in (
            "moduleRoute",
            "Link to={route}",
            "/knowledge/book-lore",
            "/knowledge/style-examples",
            "/knowledge/facts",
            "/people",
            "/maintenance",
            "/observatory",
        ):
            assert marker in page

    def test_dashboard_uses_backend_health_and_todo_payloads(self):
        page = Path("webui/frontend/src/pages/dashboard/DashboardPage.tsx").read_text(encoding="utf-8")
        card = Path("webui/frontend/src/pages/dashboard/SystemHealthCard.tsx").read_text(encoding="utf-8")
        api = Path("webui/frontend/src/api/system.ts").read_text(encoding="utf-8")
        for marker in ("todos?.untagged_count", "todos?.pending_fewshot", "todos?.has_errors", "services_summary"):
            assert marker in page + api
        for marker in ("summary?.overall", "service.severity", "severity === 'critical'", "降级", "未启用"):
            assert marker in card

    def test_dashboard_injection_summary_uses_window_metrics_and_real_config(self):
        page = Path("webui/frontend/src/pages/dashboard/DashboardPage.tsx").read_text(encoding="utf-8")
        trend = Path("webui/frontend/src/pages/dashboard/InjectionTrendCard.tsx").read_text(encoding="utf-8")
        surface = page + trend
        for marker in ("getChannelConfig", "affinityConfig", "token_budget", "max_items", "total_tokens_sum", "sample_count", "avg_tokens_per_day", "日均 token"):
            assert marker in surface
