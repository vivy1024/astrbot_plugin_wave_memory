"""静态渲染依赖必须完全本地化：CDN 不可达时神经云图与管理面板仍可加载。"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

STATIC = Path("webui/static")
VENDOR = STATIC / "vendor"

REQUIRED_VENDOR_FILES = {
    "three.min.js": "THREE",
    "OrbitControls.js": "OrbitControls",
    "EffectComposer.js": "EffectComposer",
    "RenderPass.js": "RenderPass",
    "ShaderPass.js": "ShaderPass",
    "CopyShader.js": "CopyShader",
    "LuminosityHighPassShader.js": "LuminosityHighPassShader",
    "UnrealBloomPass.js": "UnrealBloomPass",
    "gsap.min.js": "gsap",
    "tailwind.min.js": "tailwind",
}

_CDN_RE = re.compile(r"""https?://(?:cdn|cdnjs|unpkg)[^"'\s)]*""", re.IGNORECASE)


@pytest.mark.parametrize("filename,symbol", sorted(REQUIRED_VENDOR_FILES.items()))
def test_vendor_dependency_exists_and_is_real_javascript(filename: str, symbol: str):
    path = VENDOR / filename
    assert path.is_file(), f"缺少本地依赖 {filename}；CDN 不可达时页面会白屏"
    content = path.read_text(encoding="utf-8", errors="ignore")
    assert len(content) > 500, f"{filename} 体积异常，可能是错误页而非 JS"
    assert not content.lstrip().lower().startswith(("<!doctype", "<html")), f"{filename} 是 HTML 而非 JS"
    assert symbol in content, f"{filename} 未包含预期符号 {symbol}"


@pytest.mark.parametrize("page", ["explore.html"])
def test_static_pages_do_not_reference_external_cdn(page: str):
    content = (STATIC / page).read_text(encoding="utf-8")
    found = _CDN_RE.findall(content)
    assert not found, f"{page} 仍引用外部 CDN：{found}"


@pytest.mark.parametrize("page", ["explore.html"])
def test_static_pages_load_dependencies_from_local_vendor(page: str):
    content = (STATIC / page).read_text(encoding="utf-8")
    assert "/static/vendor/" in content, f"{page} 未使用本地 vendor 依赖"


def test_explore_page_guards_render_dependencies_before_init():
    content = (STATIC / "explore.html").read_text(encoding="utf-8")
    assert "verifyRenderDependencies" in content
    assert "render-dependency-error" in content
    # 自检必须先于 initGraph，否则缺失 THREE 会直接抛错并留下黑屏。
    guard_index = content.index("exploreRenderReady")
    init_index = content.index("initGraph()")
    assert guard_index < init_index, "依赖自检必须在 initGraph() 之前执行"
    assert "initial-load-error" in content, "依赖缺失需通过既有协议通知父窗口"
