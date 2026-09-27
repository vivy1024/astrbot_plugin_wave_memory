"""HTML 页面路由 Blueprint"""

from pathlib import Path

try:
    from quart import Blueprint, redirect
except Exception:  # pragma: no cover - 本地单测未安装 Quart 时的轻量兜底
    class Blueprint:  # type: ignore[no-redef]
        def __init__(self, *args, **kwargs):
            pass

        def route(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

        def app_errorhandler(self, *args, **kwargs):
            def deco(func):
                return func
            return deco

    def redirect(location, code=302):  # type: ignore[no-redef]
        return "", code, {"Location": location}


pages_bp = Blueprint("pages", __name__)

_STATIC_DIR = Path(__file__).parent.parent / "static"
# HTML 入口不缓存正文：每次向服务端重新验证，确保引用最新的哈希资源。
_HTML_HEADERS = {"Content-Type": "text/html", "Cache-Control": "no-cache"}
_INDEX_MISSING = "<h1>Wave Memory WebUI</h1><p>built WebUI is unavailable</p>"


def _html_response(path: Path, fallback: str) -> tuple[str, int, dict[str, str]]:
    if path.exists():
        return path.read_text(encoding="utf-8"), 200, _HTML_HEADERS
    return fallback, 503, _HTML_HEADERS


@pages_bp.route("/")
async def index():
    return _html_response(_STATIC_DIR / "app" / "index.html", _INDEX_MISSING)


def _request_query_string() -> str:
    try:
        from quart import request
        raw = request.query_string
    except Exception:  # 无请求上下文（单测）时按无参数处理
        return ""
    return raw.decode("utf-8", "ignore") if isinstance(raw, (bytes, bytearray)) else str(raw or "")


def graph_redirect_location(layer: str, query_string: str = "") -> str:
    """旧入口统一跳到 React 关系图谱；保留原查询参数，丢弃只属于旧 iframe 的 embed/layer。"""
    from urllib.parse import parse_qsl, urlencode

    pairs = [(key, value) for key, value in parse_qsl(query_string, keep_blank_values=True) if key not in {"embed", "layer"}]
    return "/#/graph?" + urlencode([("layer", layer), *pairs])


@pages_bp.route("/explore")
async def explore():
    """旧 3D 神经云图已下线，保留地址并 302 到 /#/graph?layer=kg。"""
    return redirect(graph_redirect_location("kg", _request_query_string()), code=302)


@pages_bp.route("/maintain")
async def maintain():
    return redirect("/#/maintenance", code=302)


@pages_bp.app_errorhandler(404)
async def handle_404(err):
    from quart import request
    path = request.path.strip("/")
    if path.startswith("api/") or path.startswith("static/"):
        return "Not Found", 404
    return _html_response(_STATIC_DIR / "app" / "index.html", _INDEX_MISSING)
