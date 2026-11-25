"""The Dashboard serves its home-screen manifest and icons on a real socket (UC-DIRECT-012,
DN006 §8.10, T355): without the token, under the ``Host`` check, and the pages link them."""

from __future__ import annotations

import http.client
import json
import re
import threading
from collections.abc import Iterator
from urllib.parse import urljoin

import pytest

from tools.icons import png_size
from tsw.home import Home
from tsw.host import pwa
from tsw.host.status_app import COOKIE, StatusApp, make_server

mark = pytest.mark
UC = "UC-DIRECT-012"


@pytest.fixture
def served(home: Home) -> Iterator[StatusApp]:
    app = StatusApp(home, token="tok-p")
    server = make_server(app)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield app
    finally:
        server.shutdown()
        server.server_close()


def get(
    app: StatusApp, path: str, *, host: str | None = None, cookie: bool = False
) -> tuple[int, dict[str, str], bytes]:
    conn = http.client.HTTPConnection("127.0.0.1", app.port, timeout=30)
    headers = {"Host": host or f"127.0.0.1:{app.port}"}
    if cookie:
        headers["Cookie"] = f"{COOKIE}_{app.port}={app.token}"
    conn.request("GET", path, headers=headers)
    response = conn.getresponse()
    out = (response.status, dict(response.getheaders()), response.read())
    conn.close()
    return out


@mark.use_case(UC)
def test_the_manifest_and_icons_are_served_without_a_token(served: StatusApp) -> None:
    status, headers, body = get(served, "/manifest.webmanifest")
    assert status == 200
    assert headers["Content-Type"] == "application/manifest+json; charset=utf-8"
    assert json.loads(body) == pwa.manifest()
    assert "Set-Cookie" not in headers and "Access-Control-Allow-Origin" not in headers
    assert headers["X-Content-Type-Options"] == "nosniff"
    for icon in pwa.ICONS:
        status, headers, body = get(served, "/" + icon.name)
        assert status == 200 and headers["Content-Type"] == "image/png"
        assert body == (pwa.ICON_DIR / icon.name).read_bytes()
        assert png_size(body) == (icon.size, icon.size)
    status, headers, body = get(served, "/icon.svg")
    assert status == 200 and headers["Content-Type"] == "image/svg+xml"
    assert body == (pwa.ICON_DIR / pwa.SOURCE).read_bytes()
    # Nothing carries the token or ledger data; the Host check still applies; the rest of the
    # app still needs the token.
    assert get(served, "/manifest.webmanifest", host=f"evil.example:{served.port}")[0] == 403
    assert get(served, "/apple-touch-icon.png", host=f"evil.example:{served.port}")[0] == 403
    assert get(served, "/")[0] == 403
    assert get(served, "/icons/icon.svg")[0] == 403
    assert get(served, "/icons/icon.svg", cookie=True)[0] == 404


@mark.use_case(UC)
def test_every_link_on_the_served_pages_reaches_an_asset(home: Home, served: StatusApp) -> None:
    for address in ("/", "/preferences", "/tasks/T404"):
        status, _, body = get(served, address, cookie=True)
        assert status in (200, 404), address
        page = body.decode("utf-8")
        hrefs = re.findall(r'<link rel="(?:manifest|icon|apple-touch-icon)" href="([^"]+)"', page)
        assert len(hrefs) == 4, address
        for href in hrefs:
            target = urljoin(f"http://127.0.0.1:{served.port}{address}", href)
            path = target.split(str(served.port), 1)[1]
            assert get(served, path)[0] == 200, (address, href)
