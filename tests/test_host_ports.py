"""宿主端口：Runtime 侧的替身事件与 boot_check 的替身上下文满足端口；缺成员能被指出。"""

from __future__ import annotations

from types import SimpleNamespace

from domain.host_ports import HostContext, HostMessageEvent, missing_members


class _Event:
    def get_self_id(self): return "1"
    def get_sender_id(self): return "u"
    def get_platform_id(self): return "qq"
    def get_message_type(self): return "GroupMessage"
    def get_group_id(self): return "g"
    def get_session_id(self): return "s"
    def get_message_str(self): return "hi"


class _Context:
    def get_provider_by_id(self, provider_id): return None
    def get_all_providers(self): return []
    def add_llm_tools(self, *tools): pass
    def get_llm_tool_manager(self): return SimpleNamespace(remove_func=lambda name: None)


def test_protocols_accept_conforming_objects():
    assert isinstance(_Event(), HostMessageEvent)
    assert isinstance(_Context(), HostContext)
    assert missing_members(_Event, "HostMessageEvent") == []


def test_missing_members_are_reported():
    class Partial:
        def get_self_id(self): return "1"

    assert not isinstance(Partial(), HostMessageEvent)
    assert "get_sender_id" in missing_members(Partial, "HostMessageEvent")

