"""WebUI 构建产物的静态资源服务：哈希资源长缓存 + 构建时预压缩文件协商。

- ``/static/app/assets/*``（vite 构建，文件名带内容哈希）：
  ``Cache-Control: public, max-age=31536000, immutable``。
- 客户端 ``Accept-Encoding`` 支持且存在构建时生成的 ``.br`` / ``.gz`` 时，
  直接返回预压缩文件并带 ``Content-Encoding``；否则回退到原文件。
  请求时不做动态压缩，避免大文件阻塞事件循环。
- Range 请求始终按原文件（identity）处理，保证字节偏移语义简单可靠。
"""

from __future__ import annotations

import mimetypes
from pathlib import Path

from quart import Response, request, send_file
from werkzeug.exceptions import NotFound
from werkzeug.security import safe_join

IMMUTABLE_MAX_AGE = 31536000
IMMUTABLE_CACHE_CONTROL = f"public, max-age={IMMUTABLE_MAX_AGE}, immutable"
NO_CACHE = "no-cache"

# 偏好顺序：br 压缩率更高，其次 gzip。
_PRECOMPRESSED = (("br", ".br"), ("gzip", ".gz"))

# 预压缩只针对文本类资源；图片/字体等已压缩格式直接返回原文件。
_COMPRESSIBLE_SUFFIXES = {".js", ".mjs", ".css", ".html", ".svg", ".json", ".map", ".txt", ".wasm"}


def _guess_mimetype(path: Path) -> str:
    if path.suffix in {".js", ".mjs"}:
        return "text/javascript"
    return mimetypes.guess_type(path.name)[0] or "application/octet-stream"


def _select_precompressed(path: Path) -> tuple[str, Path] | None:
    """按 Accept-Encoding 选择可用的预压缩文件；不可用时返回 None。"""
    if path.suffix.lower() not in _COMPRESSIBLE_SUFFIXES:
        return None
    if request.headers.get("Range"):
        return None
    accept = request.accept_encodings
    for encoding, suffix in _PRECOMPRESSED:
        if accept.quality(encoding) <= 0:
            continue
        candidate = path.with_name(path.name + suffix)
        if candidate.is_file():
            return encoding, candidate
    return None


async def send_hashed_asset(directory: Path, filename: str) -> Response:
    """发送带哈希的构建资源，支持预压缩协商、条件请求、Range 与 HEAD。"""
    raw = safe_join(str(directory), filename)
    if raw is None:
        raise NotFound()
    path = Path(raw)
    # 原文件必须存在才参与协商；只有 .gz/.br 而无原文件时按 404 处理。
    if not path.is_file():
        raise NotFound()

    mimetype = _guess_mimetype(path)
    selected = _select_precompressed(path)

    if selected is None:
        response = await send_file(path, mimetype=mimetype, cache_timeout=IMMUTABLE_MAX_AGE)
        complete_length = path.stat().st_size
    else:
        encoding, encoded_path = selected
        response = await send_file(encoded_path, mimetype=mimetype, cache_timeout=IMMUTABLE_MAX_AGE)
        response.headers["Content-Encoding"] = encoding
        # 不同编码是不同表示，ETag 必须区分。
        etag, weak = response.get_etag()
        if etag:
            response.set_etag(f"{etag}-{encoding}", weak=bool(weak))
        complete_length = encoded_path.stat().st_size

    if path.suffix.lower() in _COMPRESSIBLE_SUFFIXES:
        response.vary.add("Accept-Encoding")
    response.headers["Cache-Control"] = IMMUTABLE_CACHE_CONTROL
    await response.make_conditional(request, accept_ranges=True, complete_length=complete_length)
    return response
