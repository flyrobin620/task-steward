"""The Dashboard as a home-screen web app: its manifest and icons (DN006 §8.10, T355).

A browser that adds the Dashboard to the home screen (iOS Safari "Add to Home Screen", a desktop
or Android browser's "install") reads the page's ``<link rel="manifest">``, ``apple-touch-icon``
and ``apple-mobile-web-app-title``. The Dashboard app serves the manifest and the icons at its root
(:data:`ASSETS`), without the token: they are the product's own static files and name, no ledger
data and nothing about the installation (like ``/healthz`` and the workdeck manifest).

Every address here is relative: the page links ``manifest.webmanifest`` from the root (``./``) or a
task view (``../``), and the manifest's ``start_url``, ``scope`` and icons resolve against the
manifest's own address. The manifest has no ``id``: an ``id`` resolves against the origin alone, so
``"./"`` would give every device's Dashboard behind one gateway the same identity; without it the
identity is the resolved ``start_url``, the Dashboard's own address. So behind a proxy's path prefix (workdeck: ``/d/<device>/plugins/tsw/``)
the installed app starts at the Dashboard, not at the proxy's root, and the proxy rewrites nothing.

The icons are drawn once in ``icons/icon.svg`` (a rounded square, transparent corners: favicon and
the manifest's ``any`` icons); ``tools/icons.py render`` rasterizes the PNGs below from it, the
full-bleed ones (``apple-touch-icon``, ``maskable``) with the background rectangle squared, and
``icons/icons.json`` records which source they came from.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

ICON_DIR = Path(__file__).resolve().parent / "icons"
SOURCE = "icon.svg"
RECORD = "icons.json"
MANIFEST = "manifest.webmanifest"
NAME = "TaskSteward Dashboard"
SHORT_NAME = "TaskSteward"
DESCRIPTION = "TaskSteward 的任务看板：任务、attempt 与会话，验收、退回与取消"
# The middle of the icon's background gradient, for the splash screen; the page's backgrounds (status.css) for the
# browser's bar in the light and dark schemes.
BACKGROUND = "#7c3aed"
THEME_COLORS = (("light", "#f3f5fa"), ("dark", "#0e131d"))


@dataclass(frozen=True)
class Icon:
    """A PNG drawn from :data:`SOURCE`: ``any`` keeps the rounded corners, ``full`` fills the
    square (iOS rounds it itself; a maskable icon is cut by the launcher) and keeps the drawing
    inside the maskable safe zone, a centred circle of 40% radius."""

    name: str
    size: int
    variant: str  # "any" | "full"
    purpose: str | None  # the manifest's purpose; None: not in the manifest


ICONS = (
    Icon("apple-touch-icon.png", 180, "full", None),
    Icon("icon-192.png", 192, "any", "any"),
    Icon("icon-512.png", 512, "any", "any"),
    Icon("icon-maskable-192.png", 192, "full", "maskable"),
    Icon("icon-maskable-512.png", 512, "full", "maskable"),
)
APPLE_TOUCH_ICON = ICONS[0].name


def manifest() -> dict[str, Any]:
    """The Web App Manifest; every address in it is relative to the manifest's own."""
    icons: list[dict[str, str]] = [
        {"src": SOURCE, "sizes": "any", "type": "image/svg+xml", "purpose": "any"}
    ]
    icons += [
        {
            "src": icon.name,
            "sizes": f"{icon.size}x{icon.size}",
            "type": "image/png",
            "purpose": icon.purpose,
        }
        for icon in ICONS
        if icon.purpose
    ]
    return {
        "name": NAME,
        "short_name": SHORT_NAME,
        "description": DESCRIPTION,
        "lang": "zh-CN",
        "start_url": "./",
        "scope": "./",
        "display": "standalone",
        "background_color": BACKGROUND,
        "theme_color": THEME_COLORS[0][1],
        "icons": icons,
    }


def asset(path: str) -> tuple[bytes, str] | None:
    """The body and type of ``/manifest.webmanifest`` or an icon at the app's root, else None."""
    if path == "/" + MANIFEST:
        body = json.dumps(manifest(), ensure_ascii=False, indent=2) + "\n"
        return body.encode("utf-8"), "application/manifest+json; charset=utf-8"
    if path == "/" + SOURCE:
        return (ICON_DIR / SOURCE).read_bytes(), "image/svg+xml"
    for icon in ICONS:
        if path == "/" + icon.name:
            return (ICON_DIR / icon.name).read_bytes(), "image/png"
    return None


def head_links(base: str) -> str:
    """The ``<head>`` lines that make the page installable, relative to ``base`` (the app's
    root seen from the page: ``./`` or ``../``). The manifest is fetched with credentials: behind
    the workdeck gateway the sign-in is a cookie, and a manifest request without it is sent to
    the sign-in page (workdeck T354)."""
    themes = "".join(
        f'<meta name="theme-color" content="{color}" media="(prefers-color-scheme: {scheme})">'
        for scheme, color in THEME_COLORS
    )
    return (
        f'<link rel="manifest" href="{base}{MANIFEST}" crossorigin="use-credentials">\n'
        f'<link rel="icon" href="{base}{SOURCE}" type="image/svg+xml">'
        f'<link rel="icon" href="{base}icon-192.png" type="image/png" sizes="192x192">\n'
        f'<link rel="apple-touch-icon" href="{base}{APPLE_TOUCH_ICON}">'
        f'<meta name="apple-mobile-web-app-title" content="{SHORT_NAME}">'
        '<meta name="mobile-web-app-capable" content="yes">'
        '<meta name="apple-mobile-web-app-capable" content="yes">\n'
        f"{themes}"
    )
