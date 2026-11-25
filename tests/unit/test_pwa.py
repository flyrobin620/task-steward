"""The Dashboard as a home-screen web app: manifest, icons and the page's links (UC-DIRECT-012,
DN006 §8.10, T355)."""

from __future__ import annotations

import json
import re
import shutil
import sqlite3
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urljoin

import pytest

from tools import icons as icon_tool
from tsw.host import pwa
from tsw.host.status import AppView, page_head, render
from tsw.host.status_assets import APP_CSP, CSP
from tsw.host.task_view import not_found
from tsw.projection import SCHEMA_SQL

mark = pytest.mark
UC = "UC-DIRECT-012"
# The Dashboard behind the workdeck gateway (T341, T354) and on its own.
MOUNT = "https://gateway.example/d/DEVICE/plugins/tsw/"
LOCAL = "http://127.0.0.1:47800/"


@pytest.fixture
def conn() -> Iterator[sqlite3.Connection]:
    db = sqlite3.connect(":memory:")
    db.row_factory = sqlite3.Row
    db.executescript(SCHEMA_SQL)
    yield db
    db.close()


def links(page: str) -> dict[str, str]:
    """``rel`` → ``href`` of the page's ``<link>`` elements (the first of each rel)."""
    out: dict[str, str] = {}
    for tag in re.findall(r"<link\b[^>]*>", page):
        rel = re.search(r'rel="([^"]+)"', tag)
        href = re.search(r'href="([^"]+)"', tag)
        if rel and href:
            out.setdefault(rel.group(1), href.group(1))
    return out


@mark.use_case(UC)
def test_the_manifest_names_the_app_and_keeps_every_address_relative() -> None:
    value = pwa.manifest()
    assert value["name"] == "TaskSteward Dashboard" and value["short_name"] == "TaskSteward"
    assert value["display"] == "standalone"
    assert value["start_url"] == value["scope"] == "./"
    # No id: it would resolve against the origin alone, giving every device behind one gateway
    # the same app; the default is the resolved start_url, which differs per device.
    assert "id" not in value
    other = MOUNT.replace("DEVICE", "OTHER")
    assert urljoin(urljoin(MOUNT, pwa.MANIFEST), value["start_url"]) != urljoin(
        urljoin(other, pwa.MANIFEST), value["start_url"]
    )
    for color in (value["background_color"], value["theme_color"]):
        assert re.fullmatch(r"#[0-9a-f]{6}", color)
    for root in (MOUNT, LOCAL):
        address = urljoin(root, pwa.MANIFEST)
        # start_url and scope resolve against the manifest: the Dashboard, not the proxy's root.
        assert urljoin(address, value["start_url"]) == root
        assert urljoin(address, value["scope"]) == root
        for icon in value["icons"]:
            assert not icon["src"].startswith(("/", "http"))
            assert urljoin(address, icon["src"]).startswith(root)
    purposes = {(i["src"], i["purpose"]) for i in value["icons"]}
    assert ("icon.svg", "any") in purposes
    assert ("icon-512.png", "any") in purposes and ("icon-192.png", "any") in purposes
    assert ("icon-maskable-512.png", "maskable") in purposes
    # The apple-touch-icon is the page's link, not a manifest icon.
    assert pwa.APPLE_TOUCH_ICON not in {i["src"] for i in value["icons"]}
    for icon in value["icons"]:
        if icon["type"] == "image/png":
            size = next(i.size for i in pwa.ICONS if i.name == icon["src"])
            assert icon["sizes"] == f"{size}x{size}"


@mark.use_case(UC)
def test_the_assets_are_the_manifest_and_the_icon_files() -> None:
    found = pwa.asset("/manifest.webmanifest")
    assert found is not None
    body, kind = found
    assert kind == "application/manifest+json; charset=utf-8"
    assert json.loads(body.decode("utf-8")) == pwa.manifest()
    svg = pwa.asset("/icon.svg")
    assert svg is not None and svg[1] == "image/svg+xml" and svg[0].startswith(b"<svg")
    for icon in pwa.ICONS:
        png = pwa.asset("/" + icon.name)
        assert png is not None and png[1] == "image/png"
        assert icon_tool.png_size(png[0]) == (icon.size, icon.size)
    assert pwa.APPLE_TOUCH_ICON == "apple-touch-icon.png"
    assert next(i for i in pwa.ICONS if i.name == pwa.APPLE_TOUCH_ICON).size == 180
    for path in ("/", "/icons/icon.svg", "/manifest.json", "/../icon.svg", "/icon-192.PNG"):
        assert pwa.asset(path) is None


@mark.use_case(UC)
def test_the_app_pages_link_the_manifest_and_icons_from_the_apps_root(
    conn: sqlite3.Connection,
) -> None:
    app = AppView(token="tok-1", actions={})
    board = render(conn, app)
    view = not_found("T404", app)  # a task view: one level down, tasks/<id>
    for page, address in ((board, ""), (view, "tasks/T404")):
        found = links(page)
        assert set(found) >= {"manifest", "icon", "apple-touch-icon"}
        for root in (MOUNT, LOCAL):
            here = urljoin(root, address)
            assert urljoin(here, found["manifest"]) == root + "manifest.webmanifest"
            assert urljoin(here, found["apple-touch-icon"]) == root + "apple-touch-icon.png"
            assert urljoin(here, found["icon"]) == root + "icon.svg"
        assert '<meta name="apple-mobile-web-app-title" content="TaskSteward">' in page
    manifest_tag = re.search(r'<link rel="manifest"[^>]*>', board)
    assert manifest_tag and 'crossorigin="use-credentials"' in manifest_tag.group(0)
    # The page may load them: its CSP admits its own origin's manifest and images.
    assert "manifest-src 'self'" in APP_CSP and "img-src 'self' data:" in APP_CSP


@mark.use_case(UC)
def test_the_offline_page_links_no_manifest(conn: sqlite3.Connection) -> None:
    page = render(conn)
    assert links(page) == {}
    assert "apple-mobile-web-app-title" not in page and "manifest-src" not in CSP
    assert "apple-touch-icon" not in page_head("x", None)


@mark.use_case(UC)
def test_the_committed_icons_are_drawn_from_the_source() -> None:
    assert icon_tool.check() == []


@mark.use_case(UC)
def test_the_check_finds_a_changed_source_or_png(tmp_path: Path) -> None:
    folder = tmp_path / "icons"
    shutil.copytree(pwa.ICON_DIR, folder)
    assert icon_tool.check(folder) == []
    source = folder / pwa.SOURCE
    source.write_text(source.read_text(encoding="utf-8") + "<!-- -->", encoding="utf-8")
    assert any("没有重新生成" in p for p in icon_tool.check(folder))
    shutil.copy2(pwa.ICON_DIR / pwa.SOURCE, source)
    (folder / "icon-192.png").write_bytes((pwa.ICON_DIR / "icon-512.png").read_bytes())
    problems = icon_tool.check(folder)
    assert any("icon-192.png 的尺寸" in p for p in problems)
    assert any("icon-192.png 不是" in p for p in problems)
    (folder / "apple-touch-icon.png").unlink()
    assert "缺少 apple-touch-icon.png" in icon_tool.check(folder)


@mark.use_case(UC)
def test_the_full_bleed_variant_squares_only_the_background() -> None:
    svg = '<svg><rect id="bg" width="512" height="512" rx="112"/><rect rx="9"/></svg>'
    assert (
        icon_tool.full_bleed(svg)
        == '<svg><rect id="bg" width="512" height="512"/><rect rx="9"/></svg>'
    )
    with pytest.raises(SystemExit):
        icon_tool.full_bleed('<svg><rect width="512" rx="112"/></svg>')
