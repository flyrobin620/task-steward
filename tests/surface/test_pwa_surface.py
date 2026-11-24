"""The running Dashboard is installable to the home screen (UC-DIRECT-011, DN006 §8.10, T355):
``tsw dashboard`` in its own process serves the manifest and icons its pages link."""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from urllib.parse import urljoin

import pytest

from tests.surface.test_direct_surface import App, _serve
from tsw.home import Home
from tsw.host import pwa

mark = pytest.mark
UC = "UC-DIRECT-011"


@pytest.fixture
def board(home: Home) -> Iterator[App]:
    yield from _serve("dashboard")


@mark.use_case(UC)
def test_the_running_dashboard_serves_what_its_page_links(home: Home, board: App) -> None:
    # Before signing in: the manifest and the icons, nothing else.
    status, headers, body = board.request("GET", "/manifest.webmanifest")
    assert status == 200 and headers["Content-Type"].startswith("application/manifest+json")
    value = json.loads(body)
    assert value["start_url"] == "./" and value["display"] == "standalone"
    status, headers, body = board.request("GET", "/apple-touch-icon.png")
    assert status == 200 and headers["Content-Type"] == "image/png"
    assert body == (pwa.ICON_DIR / "apple-touch-icon.png").read_bytes()
    assert board.request("GET", "/")[0] == 403
    board.cookie = f"tsw_status_{board.port}={board.token}"
    page = board.request("GET", "/")[2].decode("utf-8")
    hrefs = re.findall(r'<link rel="(?:manifest|icon|apple-touch-icon)" href="([^"]+)"', page)
    assert hrefs and '<meta name="apple-mobile-web-app-title" content="TaskSteward">' in page
    board.cookie = ""
    for href in hrefs:
        path = urljoin("/", href)
        assert board.request("GET", path)[0] == 200, href
    for icon in value["icons"]:
        assert board.request("GET", urljoin("/manifest.webmanifest", icon["src"]))[0] == 200
