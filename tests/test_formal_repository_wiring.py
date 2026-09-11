from __future__ import annotations

import ast
import asyncio
import importlib
import sqlite3
import sys
import types
from pathlib import Path
from types import SimpleNamespace

from domain.evidence import EvidenceBinding, EvidenceRef
from domain.scope import RuntimeScope, SessionRef


ROOT = Path(__file__).resolve().parents[1]


def _runtime_scope(group_id: str) -> RuntimeScope:
    return RuntimeScope(
        bot_id="bot-alpha",
        visibility="group",
        session=SessionRef(
            id=f"test:group:{group_id}",
            platform_id="test",
            kind="group",
            conversation_id=group_id,
        ),
        subject_principal_id="test:user:user-1",
    )


def _import_database(monkeypatch):
    if "astrbot.api" not in sys.modules:
        api_module = types.ModuleType("astrbot.api")
        api_module.logger = SimpleNamespace(
            debug=lambda *_args, **_kwargs: None,
            info=lambda *_args, **_kwargs: None,
            warning=lambda *_args, **_kwargs: None,
            error=lambda *_args, **_kwargs: None,
        )
        astrbot_module = types.ModuleType("astrbot")
        astrbot_module.api = api_module
        monkeypatch.setitem(sys.modules, "astrbot", astrbot_module)
        monkeypatch.setitem(sys.modules, "astrbot.api", api_module)
    return importlib.import_module("engine.database")


def _method(tree: ast.Module, name: str) -> ast.FunctionDef | ast.AsyncFunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == name:
            return node
    raise AssertionError(f"method not found: {name}")


def _call_names(node: ast.AST) -> list[str]:
    names = []
    for item in ast.walk(node):
        if not isinstance(item, ast.Call):
            continue
        func = item.func
        if isinstance(func, ast.Name):
            names.append(func.id)
        elif isinstance(func, ast.Attribute):
            names.append(func.attr)
    return names


def test_wave_memory_db_exposes_formal_repositories_and_schema_is_idempotent(tmp_path, monkeypatch):
    database = _import_database(monkeypatch)
    from engine.db.scoped_learning_projection_repo import ScopedFewShotRepository
    from engine.db.scoped_soul_repo import ScopedSoulRepository

    path = tmp_path / "production.db"
    legacy = sqlite3.connect(path)
    legacy.execute("CREATE TABLE legacy_projection_marker(value TEXT NOT NULL)")
    legacy.execute("INSERT INTO legacy_projection_marker(value) VALUES ('keep-me')")
    legacy.commit()
    legacy.close()

    first = database.WaveMemoryDB(str(path))
    try:
        assert isinstance(first.soul_repository, ScopedSoulRepository)
        assert isinstance(first.fewshot_repository, ScopedFewShotRepository)
        assert not hasattr(first, "book_lore_repository")
    finally:
        first.close()

    second = database.WaveMemoryDB(str(path))
    try:
        tables = {
            row[0]
            for row in second.conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            ).fetchall()
        }
        assert {
            "scoped_soul_mood",
            "scoped_soul_concerns",
            "scoped_few_shot_examples",
        } <= tables
        assert "reviewed_book_lore_projections" not in tables
        assert second.conn.execute(
            "SELECT value FROM legacy_projection_marker"
        ).fetchone()[0] == "keep-me"
    finally:
        second.close()


def test_coordinator_writer_and_fewshot_service_share_formal_repository_with_scope_isolation(
    tmp_path, monkeypatch
):
    database = _import_database(monkeypatch)
    from engine.db.scoped_learning_projection_repo import CoordinatorScopedProjectionWriter
    from services.few_shot.service import FewShotService
    from services.system_convergence_runtime import ProductionWriteGateway

    path = tmp_path / "writer.db"
    db = database.WaveMemoryDB(str(path))
    gateway = ProductionWriteGateway(str(path))
    try:
        writer = CoordinatorScopedProjectionWriter(
            gateway.coordinator,
            fewshot_repository=db.fewshot_repository,
        )
        service = FewShotService(
            db=db,
            repository=db.fewshot_repository,
            writer=writer,
            enabled=True,
        )
        scope = _runtime_scope("group-1")
        other_scope = _runtime_scope("group-2")
        ref = EvidenceRef(
            kind="raw_message",
            id="message:formal-1",
            content_hash="sha256:formal-1",
            captured_at=100.0,
            source_scope=scope,
            available=True,
        )
        binding = EvidenceBinding(
            evidence_id=ref.id,
            target_scope=scope,
            derivation_chain=("raw_chat", "reviewed_candidate"),
            policy_version="formal-wiring/v1",
        )

        example_id = service.add_approved_example(
            scope=scope,
            candidate={"id": 41, "content": "先核实事实，再简短回应。"},
            evidence_refs=(ref,),
            evidence_bindings=(binding,),
            source_tags=({"tag_id": 1, "name": "事实"},),
            query_trace_id="trace:formal-1",
            content="先核实事实，再简短回应。",
            score=0.9,
            traits=("克制",),
            source_candidate_id=41,
        )

        stored_scope = db.fewshot_repository.get(example_id)["scope"]
        assert stored_scope.bot_id == scope.bot_id
        assert stored_scope.session == scope.session
        assert stored_scope.subject_principal_id is None
        assert "先核实事实" in service.get_injection(scope=scope)
        assert service.get_injection(scope=other_scope) == ""
    finally:
        asyncio.run(gateway.shutdown())
        db.close()


def test_service_container_explicitly_holds_formal_repositories():
    from webui.container import ServiceContainer

    soul = object()
    fewshot = object()
    db = SimpleNamespace(
        soul_repository=soul,
        fewshot_repository=fewshot,
    )
    ServiceContainer.reset()
    try:
        container = ServiceContainer()
        container.initialize(
            db=db,
            query_engine=None,
            embedding_service=None,
            memory_index=None,
            tag_index=None,
            cooccurrence=None,
        )
        assert container.soul_repository is soul
        assert container.fewshot_repository is fewshot
        assert not hasattr(container, "book_lore_repository") or container.book_lore_repository is None
    finally:
        ServiceContainer.reset()


def test_main_production_wiring_passes_formal_repositories_writer_and_runtime_scope():
    tree = ast.parse((ROOT / "main.py").read_text(encoding="utf-8"))
    constructor = _method(tree, "__init__")
    initializer = _method(tree, "_initialize_once")
    injection = _method(tree, "_setup_injection_shadow_pipeline")
    context_config = _method(tree, "_build_shadow_context_config")
    on_message = _method(tree, "on_message")

    constructor_source = ast.unparse(constructor)
    initializer_source = ast.unparse(initializer)
    injection_source = ast.unparse(injection)
    context_config_source = ast.unparse(context_config)
    on_message_source = ast.unparse(on_message)

    assert "CoordinatorScopedProjectionWriter(self.write_gateway.coordinator" in constructor_source
    assert "fewshot_repository=self.db.fewshot_repository" in constructor_source
    assert "book_lore_repository" not in constructor_source
    assert "repository=self.db.fewshot_repository" in initializer_source
    assert "writer=self.scoped_projection_writer" in initializer_source
    for service_name in ("ConcernTracker", "MoodTrajectory", "SubjectiveTime"):
        assert service_name in _call_names(initializer)
    assert "repository=soul_repository" in initializer_source
    assert "coordinator=soul_coordinator" in initializer_source
    assert {"FewShotChannel", "BookLoreChannel"} <= set(_call_names(injection))
    assert "book_lore_index=self.book_lore_index" in injection_source
    assert "from .webui.container import get_container" in injection_source
    assert "_parse_bool_config_value(cross_group_cfg.get('cross_group_enabled'), True)" in constructor_source
    assert constructor_source.count("'cross_group_enabled': self.cross_group_enabled") == 1
    assert injection_source.count("cross_group_enabled=self.cross_group_enabled") == 1
    assert injection_source.count(
        "shared_memory_grants_enabled=self.shared_memory_grants_enabled"
    ) == 1
    assert "config['cross_group_enabled'] = self.cross_group_enabled" in context_config_source
    assert "config['shared_memory_grants_enabled'] = self.shared_memory_grants_enabled" in context_config_source
    # 后台盲抽已整体废除：on_message 不得自动创建关切或时间锚点，
    # 两者都只能由模型现场基于证据经工具 / 命令链写入。
    for blind_write in ("concern_tracker.add(", "subjective_time.add_anchor("):
        assert blind_write not in on_message_source, f"on_message 不得后台盲抽: {blind_write}"
    assert "WaveMemoryNoteConcernTool(" in initializer_source, "关切必须由现场工具显式提审"
    assert "write_gateway=self.write_gateway" in initializer_source, "关切工具必须绑定正式写入网关"
    assert "scope=runtime_scope" in on_message_source
    # 注意：proactive 主动追问所需的 concern_score 目前无供数方，
    # ConcernTracker.match/summary 与 SubjectiveTime.add_anchor 均无生产调用者，
    # "关切驱动主动关心"尚未接线。此处刻意不断言其存在，
    # 以免留下"主动行为已守护"的错觉（接线为独立待办，需产品决定）。
