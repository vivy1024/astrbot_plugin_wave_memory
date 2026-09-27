"""Quart 应用工厂"""

from __future__ import annotations

from pathlib import Path

from quart import Quart, request

try:
    from astrbot.api import logger
except Exception:  # pragma: no cover - 本地单测未安装 AstrBot SDK 时的轻量兜底
    class _Logger:
        def debug(self, *args, **kwargs): pass
    logger = _Logger()


def _enable_cors(app: Quart) -> None:
    """手动 CORS（不依赖 quart-cors 包）。"""

    @app.after_request
    async def _add_cors_headers(response):
        origin = request.headers.get("Origin")
        if origin:
            response.headers["Access-Control-Allow-Origin"] = origin
            # 用 HeaderSet.add 而非直接赋值，避免覆盖下游（如静态资源）已设置的 Vary。
            response.vary.add("Origin")
            response.headers["Access-Control-Allow-Credentials"] = "true"
        response.headers.setdefault(
            "Access-Control-Allow-Headers",
            "Content-Type, Authorization, X-Requested-With",
        )
        response.headers.setdefault(
            "Access-Control-Allow-Methods",
            "GET, POST, PUT, PATCH, DELETE, OPTIONS",
        )
        return response


def create_app(
    *,
    scope_options_source=None,
    request_scope_provider=None,
) -> Quart:
    """创建并配置 Quart 应用，并显式组合请求 Scope 依赖。"""
    static_dir = Path(__file__).parent / "static"
    app = Quart(
        __name__,
        static_folder=str(static_dir) if static_dir.exists() else None,
        static_url_path="/static",
    )
    app.secret_key = "wavememory-webui"

    from .api_contract import ObjectRefRegistry

    app.extensions["wave_api_contract"] = {
        "scope_options_source": scope_options_source,
        "request_scope_provider": request_scope_provider,
        "object_refs": ObjectRefRegistry(),
    }

    _enable_cors(app)

    # 注册 Blueprint
    from .blueprints import get_blueprints

    for bp in get_blueprints():
        app.register_blueprint(bp)
        logger.debug(f"[WaveMemory WebUI] registered blueprint: {bp.name}")

    # 注意：根路径 "/" 由 pages 蓝图提供（pages.index），不要在这里重复注册 ——
    # 重复规则会按注册顺序静默取先到者，令后来者失效且无任何日志。

    @app.route("/assets/<path:filename>")
    @app.route("/static/app/assets/<path:filename>")
    async def serve_app_assets(filename):
        from .static_assets import send_hashed_asset
        assets_dir = app.config.get("WAVE_APP_ASSETS_DIR") or (static_dir / "app" / "assets")
        return await send_hashed_asset(Path(assets_dir), filename)

    @app.after_request
    async def _no_cache_spa_entry(response):
        # SPA 入口必须每次重新验证，才能及时拿到新哈希资源的引用。
        if request.path in ("/static/app/", "/static/app/index.html"):
            response.headers["Cache-Control"] = "no-cache"
            response.headers.pop("Expires", None)
        return response

    # Stage 3 diagnostics is independently registered so older blueprint registries
    # can load it without gaining any database/provider fallback behavior.
    from .blueprints.diagnostics import diagnostics_bp

    if diagnostics_bp.name not in app.blueprints:
        app.register_blueprint(diagnostics_bp)
        logger.debug(f"[WaveMemory WebUI] registered blueprint: {diagnostics_bp.name}")

    return app
