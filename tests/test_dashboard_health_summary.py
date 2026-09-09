from pathlib import Path


def test_optional_off_and_data_warmup_do_not_make_system_critical():
    from webui.blueprints.system import classify_services_health

    services = [
        {"name": "向量索引", "status": "ok", "reason": ""},
        {"name": "Embedding", "status": "ok", "reason": ""},
        {"name": "Tag 提取", "status": "ok", "reason": ""},
        {"name": "EPA 基底", "status": "degraded", "reason": "需 ≥20 个 tag 向量"},
        {"name": "自主学习", "status": "off", "reason": "StudyService 未启用或 BookLore 不可用"},
        {"name": "自省系统", "status": "off", "reason": "SelfReflect 未启用"},
        {"name": "信念引擎", "status": "off", "reason": "belief_engine 未初始化(需 LLM)"},
    ]

    annotated, summary = classify_services_health(services)

    assert summary["overall"] == "degraded"
    assert summary["label"] == "可用但降级"
    assert summary["critical_count"] == 0
    assert summary["optional_off_count"] == 3
    assert summary["degraded_count"] == 1
    assert {item["name"]: item["severity"] for item in annotated} == {
        "向量索引": "ok",
        "Embedding": "ok",
        "Tag 提取": "ok",
        "EPA 基底": "degraded",
        "自主学习": "disabled",
        "自省系统": "disabled",
        "信念引擎": "disabled",
    }


def test_live_epa_state_overrides_stale_registry_status():
    from webui.blueprints.system import refresh_dynamic_services_health

    class _EPA:
        initialized = True
        min_tags = 20

    class _Container:
        epa = _EPA()

    services = [
        {"name": "EPA 基底", "status": "degraded", "reason": "需 ≥20 个 tag 向量", "dependency": "Tag 覆盖率 > 20%"},
        {"name": "Embedding", "status": "ok", "reason": ""},
    ]

    refreshed = refresh_dynamic_services_health(_Container(), services)
    epa = next(item for item in refreshed if item["name"] == "EPA 基底")

    assert epa["status"] == "ok"
    assert epa["reason"] == ""


def test_health_registry_reasons_distinguish_missing_bot_profile_from_llm_missing():
    source = Path("main.py").read_text(encoding="utf-8")

    assert "tag_llm_provider_id 未配置" in source
    assert "未配置 Bot Profile" in source
    assert "belief_engine 初始化失败或未启用" in source


def test_bot_profile_schema_is_explicit_and_runtime_has_no_fixed_identity_fallback():
    import json

    schema = json.loads(Path("_conf_schema.json").read_text(encoding="utf-8"))
    source = Path("main.py").read_text(encoding="utf-8")

    assert "MetaThinking_Bot1" in schema
    assert "MetaThinking_Bot2" in schema
    assert schema["MetaThinking_Bot1"]["items"]["db_id"]["default"] == "yushu"
    assert schema["MetaThinking_Bot2"]["items"]["db_id"]["default"] == "baizz"
    assert "_BUILTIN_BOT_PROFILE_CONFIGS" not in source
    assert "_bot_registry_compat_fallback" not in source
    assert "BotProfile requires explicit qq_id and stable db_id" in source
    assert "no implicit bot administrator granted" in source


def test_core_off_or_runtime_error_makes_system_critical():
    from webui.blueprints.system import classify_services_health

    services = [
        {"name": "Embedding", "status": "off", "reason": "embedding_provider_id 未配置"},
        {"name": "自主学习", "status": "off", "reason": "StudyService 未启用或 BookLore 不可用"},
        {"name": "Tag 提取", "status": "error", "reason": "LLM init failed"},
    ]

    annotated, summary = classify_services_health(services)
    severities = {item["name"]: item["severity"] for item in annotated}

    assert summary["overall"] == "critical"
    assert summary["label"] == "异常"
    assert summary["critical_count"] == 2
    assert summary["optional_off_count"] == 1
    assert severities["Embedding"] == "critical"
    assert severities["Tag 提取"] == "critical"
    assert severities["自主学习"] == "disabled"
