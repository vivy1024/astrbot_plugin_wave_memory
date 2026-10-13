import asyncio
import sqlite3
from types import SimpleNamespace

import pytest

from domain.evidence import (
    EvidenceBinding,
    EvidenceDerivation,
    EvidenceRef,
    FULL_EVIDENCE_DERIVATION_CHAIN,
)
from domain.scope import CatalogScope, RuntimeScope, SessionRef


def _runtime_scope(group_id: str = "group-1", *, bot_id: str = "bot-a") -> RuntimeScope:
    return RuntimeScope(
        bot_id=bot_id,
        visibility="group",
        session=SessionRef(
            id=f"test:group:{group_id}",
            platform_id="test",
            kind="group",
            conversation_id=group_id,
        ),
        subject_principal_id="test:user:user-1",
    )


def _runtime_evidence(scope: RuntimeScope):
    ref = EvidenceRef(
        kind="raw_message",
        id="message:fewshot-1",
        content_hash="sha256:fewshot-1",
        captured_at=100.0,
        source_scope=scope,
        available=True,
    )
    binding = EvidenceBinding(
        evidence_id=ref.id,
        target_scope=scope,
        derivation_chain=("raw_chat", "reviewed_candidate"),
        policy_version="contract-v1",
    )
    return ref, binding


def _catalog_projection_evidence(target: RuntimeScope):
    source = CatalogScope(catalog_id="book-lore", corpus_id="corpus-a", version="v1")
    ref = EvidenceRef(
        kind="reviewed_book_lore_projection",
        id="community:7",
        content_hash="sha256:community-7",
        captured_at=100.0,
        source_scope=source,
        available=True,
    )
    binding = EvidenceBinding(
        evidence_id=ref.id,
        target_scope=target,
        derivation_chain=FULL_EVIDENCE_DERIVATION_CHAIN,
        policy_version="scope-derivation/v1",
    )
    derivation = EvidenceDerivation(
        kind="EvidenceDerivation",
        reviewed=True,
        review_status="reviewed",
        derivation_version="book-lore/v1",
        policy_version="scope-derivation/v1",
        source=source,
        target=target,
        derivation_chain=FULL_EVIDENCE_DERIVATION_CHAIN,
    )
    return source, ref, binding, derivation


def test_book_lore_channel_requires_raw_catalog_dependencies():
    """书设注入不再接受 reviewed projection 作为数据源。"""
    from domain.scope import CatalogScope
    from services.injection.channels.book_lore import BookLoreChannel
    from services.injection.context import InjectionContext

    current = _runtime_scope()
    ctx = InjectionContext(
        event="event",
        req=object(),
        message="旧港",
        group_id="group-1",
        sender_id="user-1",
        sender_name="用户",
        bot_id="10001",
        bot_profile_id="bot-a",
        scope=current,
        config={"channels": {"book_lore": {"top_k": 5}}},
        trace_id="trace-book-lore",
    )
    projection_only = BookLoreChannel(
        projection_repository=object(),
        catalog_scope=CatalogScope(catalog_id="book-lore", corpus_id="corpus-a", version="v1"),
    )
    result = asyncio.run(projection_only.build(ctx))
    assert result.status in {"empty", "disabled"}
    assert "当前群设定" not in (result.text or "")


@pytest.mark.asyncio
async def test_webui_lists_only_formal_fewshot_for_exact_runtime_scope():
    quart = pytest.importorskip("quart")
    if not hasattr(getattr(quart, "Quart", None), "test_client"):
        pytest.skip("Quart runtime client is unavailable")

    from engine.db.scoped_learning_projection_repo import ScopedFewShotRepository
    from webui.app import create_app
    from webui.container import ServiceContainer, get_container
    from webui.scope_options import ExplicitRequestScopeProvider

    conn = sqlite3.connect(":memory:")
    fewshot = ScopedFewShotRepository(conn, now=lambda: 100.0)
    current = _runtime_scope()
    other = _runtime_scope("group-2")

    for scope, candidate_id, content in (
        (current, 51, "当前群的克制风格"),
        (other, 52, "其他群的隐藏风格"),
    ):
        ref, binding = _runtime_evidence(scope)
        fewshot.write_approved(
            scope=scope,
            candidate={"id": candidate_id},
            evidence_refs=(ref,),
            evidence_bindings=(binding,),
            source_tags=[{
                "tag_id": candidate_id,
                "name": "测试风格",
                "tag_type": "style",
                "position": 1,
                "relevance": 1.0,
            }],
            query_trace_id=f"trace:{candidate_id}",
            content=content,
            score=0.9,
            traits=("克制",),
            source_candidate_id=candidate_id,
            idempotency_key=f"candidate:{candidate_id}",
        )

    ServiceContainer.reset()
    container = get_container()
    container.db = SimpleNamespace(conn=conn, closed=False)
    container.password = ""
    container.fewshot_repository = fewshot
    provider = ExplicitRequestScopeProvider(
        bot_registry={"bot-a": SimpleNamespace(db_id="bot-a")}
    )
    app = create_app(request_scope_provider=provider)
    client = app.test_client()
    query = "bot_id=bot-a&session_id=test:group:group-1&visibility=group"

    try:
        response = await client.get(f"/api/knowledge/few-shot?{query}")
        assert response.status_code == 200
        payload = await response.get_json()
        assert payload["source"] == "scoped_few_shot_examples"
        assert payload["page"]["total"] == 1
        assert [item["content"] for item in payload["items"]] == ["当前群的克制风格"]
        assert payload["items"][0]["session_id"] == "test:group:group-1"

        lore_response = await client.get(f"/api/knowledge/book-lore/projections?{query}")
        assert lore_response.status_code == 404

        missing_scope = await client.get("/api/knowledge/few-shot")
        assert missing_scope.status_code == 400
        assert (await missing_scope.get_json())["error"]["code"] == "scope_required"

        qq_bot = await client.get(
            "/api/knowledge/few-shot?bot_id=10001&session_id=test:group:group-1&visibility=group"
        )
        assert qq_bot.status_code == 400
        assert (await qq_bot.get_json())["error"]["code"] == "scope_required"
    finally:
        ServiceContainer.reset()
        conn.close()
