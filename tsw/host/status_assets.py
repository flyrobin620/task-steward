"""Style, script and colours of the STATUS page, kept apart from the renderer.

The page is a single offline file: ``status.css`` and ``status.js`` (next to this module) are
inlined, and the Content-Security-Policy admits only that script, by its SHA-256, so a task title
or an output reference can never run code in the page. Without the script every card is still
shown, grouped by status; the script adds filtering, search, sorting, regrouping, folding and the
theme switch, and keeps the choices in the URL fragment and local storage.
"""

from __future__ import annotations

import base64
import hashlib
from pathlib import Path

_HERE = Path(__file__).resolve().parent

# Status colours: the hue tells the state, the strongest hues mark what waits on the user.
STATUS_COLORS = {
    "waiting_user": "#e11d48",
    "verifying": "#7c3aed",
    "in_progress": "#2563eb",
    "dispatching": "#0891b2",
    "paused": "#d97706",
    "ready": "#0d9488",
    "waiting_dependency": "#64748b",
    "intake": "#db2777",
    "done": "#ea580c",
    "accepted": "#16a34a",
    "cancelled": "#6b7280",
    "archived": "#6b7280",
}
KIND_COLORS = {"research": "#8b5cf6", "design": "#ec4899", "code": "#0ea5e9"}
# Repository hues, picked by a stable hash of the name so a repository keeps its colour.
REPO_HUES = (212, 158, 276, 24, 332, 188, 96, 250, 4, 44, 300, 136)

CSS = (_HERE / "status.css").read_text(encoding="utf-8")
SCRIPT = (_HERE / "status.js").read_text(encoding="utf-8")
SCRIPT_HASH = "sha256-" + base64.b64encode(hashlib.sha256(SCRIPT.encode("utf-8")).digest()).decode()
CSP = (
    "default-src 'none'; "
    f"script-src '{SCRIPT_HASH}'; "
    "style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"
)
# The STATUS app (DN006 §8.10) adds its action script and may call its own server, nothing else.
APP_SCRIPT = (_HERE / "status_app.js").read_text(encoding="utf-8")
APP_SCRIPT_HASH = (
    "sha256-" + base64.b64encode(hashlib.sha256(APP_SCRIPT.encode("utf-8")).digest()).decode()
)
APP_CSP = (
    "default-src 'none'; "
    f"script-src '{SCRIPT_HASH}' '{APP_SCRIPT_HASH}'; connect-src 'self'; "
    "style-src 'unsafe-inline'; img-src data:; base-uri 'none'; form-action 'none'"
)
