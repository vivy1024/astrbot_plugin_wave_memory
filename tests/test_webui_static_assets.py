"""WebUI 静态资源缓存头与预压缩协商。"""

from __future__ import annotations

import gzip
from pathlib import Path

import pytest

from webui.app import create_app
from webui.static_assets import IMMUTABLE_CACHE_CONTROL

ASSET = "index-AbC123.js"
RAW = (b"console.log('wave memory');\n" * 200)
# 预压缩内容只需可区分即可，不依赖 brotli 库。
GZ = gzip.compress(RAW)
BR = b"fake-brotli-payload"


@pytest.fixture
def assets_dir(tmp_path: Path) -> Path:
    directory = tmp_path / "assets"
    directory.mkdir()
    (directory / ASSET).write_bytes(RAW)
    (directory / f"{ASSET}.gz").write_bytes(GZ)
    (directory / f"{ASSET}.br").write_bytes(BR)
    (directory / "style-Xy9.css").write_bytes(b"body{color:red}" * 100)
    return directory


@pytest.fixture
def client(assets_dir: Path):
    app = create_app()
    app.config["WAVE_APP_ASSETS_DIR"] = assets_dir
    return app.test_client()


def _assert_immutable(response) -> None:
    assert response.headers["Cache-Control"] == IMMUTABLE_CACHE_CONTROL
    assert "Accept-Encoding" in response.headers.get("Vary", "")


@pytest.mark.asyncio
async def test_brotli_preferred_when_accepted(client):
    response = await client.get(
        f"/static/app/assets/{ASSET}", headers={"Accept-Encoding": "gzip, deflate, br"}
    )
    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "br"
    assert response.headers["Content-Type"].startswith("text/javascript")
    assert int(response.headers["Content-Length"]) == len(BR)
    assert await response.get_data() == BR
    assert response.headers["ETag"].endswith('-br"')
    _assert_immutable(response)


@pytest.mark.asyncio
async def test_gzip_when_only_gzip_accepted(client):
    response = await client.get(f"/static/app/assets/{ASSET}", headers={"Accept-Encoding": "gzip"})
    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "gzip"
    assert int(response.headers["Content-Length"]) == len(GZ)
    assert gzip.decompress(await response.get_data()) == RAW
    _assert_immutable(response)


@pytest.mark.asyncio
async def test_identity_without_accept_encoding(client):
    response = await client.get(f"/static/app/assets/{ASSET}")
    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers
    assert int(response.headers["Content-Length"]) == len(RAW)
    assert await response.get_data() == RAW
    _assert_immutable(response)


@pytest.mark.asyncio
async def test_rejected_encoding_quality_zero_falls_back(client):
    response = await client.get(
        f"/static/app/assets/{ASSET}", headers={"Accept-Encoding": "br;q=0, gzip;q=0"}
    )
    assert "Content-Encoding" not in response.headers
    assert await response.get_data() == RAW


@pytest.mark.asyncio
async def test_missing_precompressed_falls_back_to_original(client, assets_dir: Path):
    response = await client.get(
        "/static/app/assets/style-Xy9.css", headers={"Accept-Encoding": "gzip, br"}
    )
    assert response.status_code == 200
    assert "Content-Encoding" not in response.headers
    assert response.headers["Content-Type"].startswith("text/css")
    assert await response.get_data() == (assets_dir / "style-Xy9.css").read_bytes()
    _assert_immutable(response)

    # 只缺 .br 时回退到 .gz。
    (assets_dir / f"{ASSET}.br").unlink()
    response = await client.get(f"/static/app/assets/{ASSET}", headers={"Accept-Encoding": "gzip, br"})
    assert response.headers["Content-Encoding"] == "gzip"


@pytest.mark.asyncio
async def test_head_request_keeps_headers_without_body(client):
    response = await client.head(f"/static/app/assets/{ASSET}", headers={"Accept-Encoding": "br"})
    assert response.status_code == 200
    assert response.headers["Content-Encoding"] == "br"
    assert int(response.headers["Content-Length"]) == len(BR)
    # Quart 测试客户端不剥离 HEAD 正文（线上由 Hypercorn/h11 丢弃 HEAD 正文），
    # 这里只锁定头部契约。
    assert await response.get_data() == BR


@pytest.mark.asyncio
async def test_range_request_served_from_identity(client):
    response = await client.get(
        f"/static/app/assets/{ASSET}", headers={"Accept-Encoding": "gzip, br", "Range": "bytes=0-9"}
    )
    assert response.status_code == 206
    assert "Content-Encoding" not in response.headers
    assert response.headers["Content-Range"] == f"bytes 0-9/{len(RAW)}"
    assert await response.get_data() == RAW[:10]


@pytest.mark.asyncio
async def test_conditional_request_returns_304_per_encoding(client):
    first = await client.get(f"/static/app/assets/{ASSET}", headers={"Accept-Encoding": "br"})
    etag = first.headers["ETag"]
    again = await client.get(
        f"/static/app/assets/{ASSET}", headers={"Accept-Encoding": "br", "If-None-Match": etag}
    )
    assert again.status_code == 304
    # br 表示的 ETag 不能命中 identity 表示。
    identity = await client.get(f"/static/app/assets/{ASSET}", headers={"If-None-Match": etag})
    assert identity.status_code == 200


@pytest.mark.asyncio
async def test_legacy_assets_alias_and_missing_file(client):
    response = await client.get(f"/assets/{ASSET}", headers={"Accept-Encoding": "br"})
    assert response.headers["Content-Encoding"] == "br"
    _assert_immutable(response)
    missing = await client.get("/static/app/assets/nope-123.js")
    assert missing.status_code == 404


@pytest.mark.asyncio
async def test_cors_does_not_clobber_vary(client):
    response = await client.get(
        f"/static/app/assets/{ASSET}",
        headers={"Accept-Encoding": "br", "Origin": "http://127.0.0.1:5173"},
    )
    vary = {item.strip() for item in response.headers["Vary"].split(",")}
    assert {"Accept-Encoding", "Origin"} <= vary


@pytest.mark.asyncio
async def test_spa_entries_are_no_cache():
    client = create_app().test_client()
    index_html = Path(__file__).resolve().parents[1] / "webui" / "static" / "app" / "index.html"
    for path in ("/", "/some/spa/route"):
        response = await client.get(path)
        assert response.headers["Cache-Control"] == "no-cache", path
    if index_html.exists():
        response = await client.get("/static/app/index.html")
        assert response.status_code == 200
        assert response.headers["Cache-Control"] == "no-cache"
        assert "Expires" not in response.headers
