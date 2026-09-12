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
            response.headers["Vary"] = "Origin"
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
        from quart import send_from_directory
        assets_dir = static_dir / "app" / "assets"
        return await send_from_directory(assets_dir, filename)

    # Stage 3 diagnostics is independently registered so older blueprint registries
    # can load it without gaining any database/provider fallback behavior.
    from .blueprints.diagnostics import diagnostics_bp

    if diagnostics_bp.name not in app.blueprints:
        app.register_blueprint(diagnostics_bp)
        logger.debug(f"[WaveMemory WebUI] registered blueprint: {diagnostics_bp.name}")

    return app
