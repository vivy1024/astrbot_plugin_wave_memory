"""敏感配置（WebUI 密码）：接口不回传明文，只报告是否已设置；保存时空值表示不修改。"""

from __future__ import annotations

from services.config.settings_state import build_field_state, build_settings_schema, is_secret_field

SCHEMA = {
    "WebUI_Settings": {
        "type": "object",
        "items": {
            "webui_password": {"type": "string", "default": ""},
            "webui_port": {"type": "int", "default": 9876},
        },
    }
}


def test_password_is_masked_but_reports_has_value():
    saved = {"WebUI_Settings": {"webui_password": "hunter2", "webui_port": 9876}}
    state = build_field_state(
        group_key="WebUI_Settings", item_key="webui_password", meta=SCHEMA["WebUI_Settings"]["items"]["webui_password"],
        saved_config=saved, effective_config=saved,
    )
    assert state["secret"] is True and state["has_value"] is True
    assert "hunter2" not in repr(state)

    payload = build_settings_schema(SCHEMA, saved, saved)
    assert "hunter2" not in repr(payload)
    port = build_field_state(
        group_key="WebUI_Settings", item_key="webui_port", meta=SCHEMA["WebUI_Settings"]["items"]["webui_port"],
        saved_config=saved, effective_config=saved,
    )
    assert port["effective"] == 9876 and "secret" not in port


def test_unset_password_reports_no_value():
    state = build_field_state(
        group_key="WebUI_Settings", item_key="webui_password", meta=SCHEMA["WebUI_Settings"]["items"]["webui_password"],
        saved_config={"WebUI_Settings": {}}, effective_config={"WebUI_Settings": {}},
    )
    assert state["has_value"] is False


def test_secret_field_registry():
    assert is_secret_field("WebUI_Settings", "webui_password")
    assert not is_secret_field("WebUI_Settings", "webui_port")
