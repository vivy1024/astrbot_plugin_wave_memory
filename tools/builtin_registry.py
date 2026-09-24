"""内置工具登记：原来写在 ``main.py`` 里的 21 个工具实例化，按组搬到这里。

每个工厂接收插件对象（``deps``），只读取它需要的服务；依赖为 None 时由工具自己
按 fail-closed 规则处理，行为与 v5 一致。新增工具只需要在这里加一条 ToolSpec，
或放进 ``<plugin_data>/extensions/``，不用改 ``main.py``。
"""

from __future__ import annotations

from typing import Any

try:
    from ..services.tool_registry import ToolSpec
except ImportError:  # pragma: no cover - direct imports in isolated tests
    from services.tool_registry import ToolSpec


def _memory_tools() -> list[ToolSpec]:
    from .extra_tools import WaveMemoryFactsTool
    from .memory_search import WaveMemoryRememberTool, WaveMemorySearchTool
    from .person_search import WaveMemoryPersonSearchTool

    return [
        ToolSpec("wave_memory_search", lambda d: WaveMemorySearchTool(query_engine=d.query_engine, db=d.db)),
        ToolSpec("wave_memory_remember", lambda d: WaveMemoryRememberTool(writer=d.writer), writes=True),
        ToolSpec("wave_memory_facts", lambda d: WaveMemoryFactsTool(db=d.db)),
        ToolSpec("wave_memory_person_search", lambda d: WaveMemoryPersonSearchTool(db=d.db)),
    ]


def _agent_feedback_tools() -> list[ToolSpec]:
    from .config_suggestion import WaveMemorySuggestConfigTool
    from .injection_explain import WaveMemoryExplainInjectionTool
    from .memory_feedback import WaveMemoryFeedbackMemoryTool
    from .review_candidate import WaveMemorySubmitReviewCandidateTool

    common = {"group": "agent_feedback", "capability": "agent_feedback_tools", "capability_default": False}
    return [
        ToolSpec("wave_memory_explain_injection", lambda d: WaveMemoryExplainInjectionTool(db=d.db), **common),
        ToolSpec("wave_memory_feedback_memory", lambda d: WaveMemoryFeedbackMemoryTool(db=d.db), writes=True, **common),
        ToolSpec("wave_memory_suggest_config", lambda d: WaveMemorySuggestConfigTool(db=d.db), writes=True, **common),
        ToolSpec("wave_memory_submit_review_candidate", lambda d: WaveMemorySubmitReviewCandidateTool(db=d.db), writes=True, **common),
    ]


def _social_tools() -> list[ToolSpec]:
    from .belief_proposal import WaveMemoryProposeBeliefTool
    from .concern import WaveMemoryNoteConcernTool
    from .cultural_moment import WaveMemoryMarkCulturalMomentTool
    from .diary_episode import WaveMemoryRecordDiaryEpisodeTool
    from .episode import WaveMemoryNoteEpisodeTool
    # 注意：生效的是 affinity_update 里的版本；extra_tools 里同名类是已退役的占位。
    from .affinity_update import WaveMemoryAffinityTool
    from .fact_proposal import WaveMemoryProposeFactTool
    from .social_anchor import WaveMemoryNoteSocialAnchorTool
    from .social_impression import WaveMemoryRecordSocialImpressionTool

    def social_impression(d: Any) -> Any:
        return WaveMemoryRecordSocialImpressionTool(
            db=d.db,
            relationship_events=d.relationship_service,
            bot_db_ids={p.qq_id: p.db_id for p in d._bot_registry.values()},
        )

    common = {"group": "social", "capability": "affinity_tools", "capability_default": True}
    return [
        ToolSpec("wave_memory_affinity", lambda d: WaveMemoryAffinityTool(db=d.db), **common),
        ToolSpec("wave_memory_record_social_impression", social_impression, writes=True, **common),
        ToolSpec(
            "wave_memory_note_social_anchor",
            lambda d: WaveMemoryNoteSocialAnchorTool(
                db=d.db,
                concern_tracker=getattr(d, "concern_tracker", None),
                repository=getattr(d.db, "soul_repository", None),
                write_gateway=d.write_gateway,
            ),
            writes=True,
            **common,
        ),
        ToolSpec(
            "wave_memory_mark_cultural_moment",
            lambda d: WaveMemoryMarkCulturalMomentTool(db=d.db, jargon_service=getattr(d, "jargon_service", None)),
            writes=True,
            **common,
        ),
        ToolSpec(
            "wave_memory_propose_fact",
            lambda d: WaveMemoryProposeFactTool(db=d.db, jargon_service=getattr(d, "jargon_service", None)),
            writes=True,
            **common,
        ),
        ToolSpec("wave_memory_propose_belief", lambda d: WaveMemoryProposeBeliefTool(db=d.db), writes=True, **common),
        ToolSpec(
            "wave_memory_note_episode",
            lambda d: WaveMemoryNoteEpisodeTool(db=d.db, writer=d.writer, write_gateway=d.write_gateway),
            writes=True,
            **common,
        ),
        ToolSpec(
            "wave_memory_record_diary_episode",
            lambda d: WaveMemoryRecordDiaryEpisodeTool(db=d.db, write_gateway=d.write_gateway),
            writes=True,
            **common,
        ),
        ToolSpec(
            "wave_memory_note_concern",
            lambda d: WaveMemoryNoteConcernTool(
                db=d.db,
                concern_tracker=getattr(d, "concern_tracker", None),
                write_gateway=d.write_gateway,
            ),
            writes=True,
            **common,
        ),
    ]


def _book_lore_tools() -> list[ToolSpec]:
    def book_lore(d: Any) -> Any:
        from .book_lore_query import WaveMemoryBookLoreQueryTool

        return WaveMemoryBookLoreQueryTool(
            book_lore_index=d.book_lore_index,
            embedding_service=d.embedding_service,
            lore_db_path=d.lore_db_path,
            catalog_scope=d.book_lore_catalog_scope,
        )

    return [ToolSpec("wave_memory_book_lore_search", book_lore, group="book_lore", capability="book_lore_tools")]


def builtin_tool_specs() -> list[ToolSpec]:
    return [*_memory_tools(), *_agent_feedback_tools(), *_social_tools(), *_book_lore_tools()]


__all__ = ["builtin_tool_specs"]
